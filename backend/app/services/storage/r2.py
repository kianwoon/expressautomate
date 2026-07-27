"""Source-email body storage on Cloudflare R2 (plan §2.3 as amended, §10).

Bodies live here rather than in Postgres because they are large, read by one
job, and deleted on their own schedule — retention removes the object and keeps
the row, since the row is the deduplication entry.

Keys are derived from the message, never generated. The fetch job writes the
body *before* it flips the row to `fetched`, so a crash between the two must
cost nothing worse than a repeated write. That only holds if the retry lands on
the same key; a random key would leave an orphan that no row references and no
purge can find.
"""

import base64
import uuid
from typing import Protocol

import aioboto3
from botocore.exceptions import ClientError

from app.core.config import settings

# What R2 answers when the key is not there. boto3 surfaces it as a ClientError
# rather than a typed exception, and the code differs between get_object
# ("NoSuchKey") and head_object ("404").
#
# NoSuchBucket is deliberately absent. A missing bucket is a misconfiguration,
# not a purged body, and treating it as absence would turn every read into
# "already gone" — extraction would then run on empty text and record confident
# nothing, across every tenant at once, without a single error.
_ABSENT_CODES = frozenset({"NoSuchKey", "404"})

# The same distinction, given a name so `put` can act on it too.
_NO_SUCH_BUCKET = "NoSuchBucket"


class BodyStoreMisconfigured(Exception):
    """The body store cannot work until an operator changes something.

    Distinct from a transient storage failure because it answers the same way
    forever: retrying spends attempts on a state no amount of patience fixes,
    and buries the one line that says what to do.
    """

# S3 caps a batch delete at 1000 keys. Tenant deletion and a backlogged
# retention sweep both exceed that, and the API rejects the whole request rather
# than trimming it.
_MAX_DELETE_BATCH = 1000


def body_key(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, message_id: str, kind: str
) -> str:
    """Deterministic object key for one message body.

    The Graph immutable id is base64url-encoded rather than used verbatim: it
    may contain `/`, `+` and `=`, and a `/` would silently invent a key prefix.
    That breaks two things at once — the `{tenant_id}/` prefix that tenant
    deletion purges by, and uniqueness, since two ids differing only around a
    slash would land in the same place.

    Padding is stripped because it is recoverable from the length and `=` reads
    badly in a key; nothing ever decodes these back.
    """
    encoded = base64.urlsafe_b64encode(message_id.encode()).decode().rstrip("=")
    return f"{tenant_id}/{mailbox_id}/{encoded}.{kind}"


class BodyDeletionFailed(Exception):
    """Some keys in a batch were not deleted."""


def _raise_on_partial_failure(response: dict | None) -> None:
    """Turn a partially failed batch into an exception.

    `delete_objects` does not raise when individual keys fail — it returns them
    in `Errors` and answers 200 for the request as a whole. Ignoring that is a
    silent retention leak: `purge_expired` would null the row's keys while the
    objects survive in R2, and because the row no longer names them, no rerun
    could ever find them again. Failing loudly leaves the keys in place, so the
    next sweep retries.
    """
    errors = (response or {}).get("Errors") or []
    if errors:
        failed = ", ".join(str(e.get("Key")) for e in errors[:5])
        raise BodyDeletionFailed(f"{len(errors)} object(s) not deleted: {failed}")


def _batched(items: tuple[str, ...], size: int):
    """Split into chunks the API will accept. `itertools.batched` is 3.12+ but
    returns tuples of a generic type; this keeps the intent obvious at the one
    call site."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


class BodyStore(Protocol):
    """What the pipeline needs from storage. `InMemoryBodyStore` is the double."""

    async def put(self, key: str, content: str) -> None: ...
    async def get(self, key: str) -> str | None: ...
    async def delete(self, *keys: str) -> None: ...


class R2BodyStore:
    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client(self):
        """Open a client for one operation.

        The region is passed explicitly. botocore does not fail without one —
        it quietly defaults S3 to `us-east-1` — and that is the problem: the
        region is baked into the credential scope of the SigV4 signature, so a
        deploy host that happens to export `AWS_DEFAULT_REGION` would silently
        change how requests are signed. R2 tolerates some values, but which
        ones is not a property worth depending on. Cloudflare documents `auto`;
        pinning it makes signing independent of ambient AWS configuration.
        """
        return self._session.client(
            "s3",
            endpoint_url=settings.R2_ENDPOINT_URL,
            region_name=settings.R2_REGION,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        )

    async def put(self, key: str, content: str) -> None:
        """Store a body, naming a missing bucket for what it is.

        A bucket that does not exist is a deployment gap, not a bad moment:
        every write will fail identically until somebody creates it. Left as a
        raw `NoSuchBucket`, it arrived as an arq traceback per email and looked
        like a transient storage error being retried — which is exactly what
        happened here, with the bucket having never been created at all.
        """
        async with self._client() as s3:
            try:
                await s3.put_object(
                    Bucket=settings.R2_BUCKET_NAME, Key=key, Body=content.encode()
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == _NO_SUCH_BUCKET:
                    raise BodyStoreMisconfigured(
                        f"R2 bucket {settings.R2_BUCKET_NAME!r} does not exist. "
                        "No email body can be stored until it is created."
                    ) from exc
                raise

    async def get(self, key: str) -> str | None:
        """Fetch a body, or None if it is no longer stored.

        Absence is an expected state rather than an error: retention deletes
        the object and keeps the row, so anything reading a body has to cope
        with the body having been purged out from under it.

        A missing *bucket* is emphatically not that, and gets the same named
        exception the write path raises. It must never become `None` here:
        extraction would read empty text and record a confident nothing for
        every email in every tenant, without one error to show for it.
        """
        async with self._client() as s3:
            try:
                obj = await s3.get_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == _NO_SUCH_BUCKET:
                    raise BodyStoreMisconfigured(
                        f"R2 bucket {settings.R2_BUCKET_NAME!r} does not exist. "
                        "No email body can be read until it is created."
                    ) from exc
                if code in _ABSENT_CODES:
                    return None
                raise
            return (await obj["Body"].read()).decode()

    async def delete(self, *keys: str) -> None:
        """Delete objects, tolerating any that are already gone.

        The empty-batch guard is here rather than in callers because the S3 API
        rejects a delete with no objects, and `purge_expired` legitimately
        reaches rows whose keys were already cleared.
        """
        if not keys:
            return
        async with self._client() as s3:
            for batch in _batched(keys, _MAX_DELETE_BATCH):
                # delete_objects reports absent keys as deleted, which is
                # exactly the idempotence a rerun of the purge needs.
                response = await s3.delete_objects(
                    Bucket=settings.R2_BUCKET_NAME,
                    Delete={"Objects": [{"Key": key} for key in batch]},
                )
                _raise_on_partial_failure(response)


class InMemoryBodyStore:
    """Test double. The pipeline's tests must never reach the network."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    async def put(self, key: str, content: str) -> None:
        self.objects[key] = content

    async def get(self, key: str) -> str | None:
        return self.objects.get(key)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.objects.pop(key, None)
