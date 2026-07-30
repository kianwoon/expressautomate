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
import sys
import uuid
from contextlib import asynccontextmanager
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


def avatar_key(tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> str:
    """Deterministic object key for a candidate's avatar photo.

    The leading `{tenant_id}/` segment is load-bearing, same as `body_key`:
    it is the prefix a tenant erasure would purge by, so anything stored
    without it would survive that request. No prefix purge is written yet —
    the convention is kept here so the sweep, when it lands, finds every
    object it needs to and not a photo it was never told about.
    """
    return f"{tenant_id}/candidates/{candidate_id}/avatar"


def client_logo_key(tenant_id: uuid.UUID, client_id: uuid.UUID) -> str:
    """Deterministic object key for a client's logo.

    Same shape and the same reason as `avatar_key`: the leading `{tenant_id}/`
    segment is what a tenant erasure purges by, so an object stored without it
    would survive the request that was supposed to remove it.

    Deterministic, so re-uploading replaces the bytes in place rather than
    accumulating one object per upload with nothing pointing at the old ones.
    """
    return f"{tenant_id}/clients/{client_id}/logo"


def document_key(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, document_id: uuid.UUID, kind: str
) -> str:
    """Where one uploaded CV is kept, exactly as it arrived.

    Computed from the authenticated tenant and a freshly minted document id,
    never from anything the client sent — the filename is attacker-controlled
    and a `../` or a leading `/` in it would escape the `{tenant_id}/` prefix
    that tenant erasure purges by. `kind` is what `sniff` decided the bytes
    are, not what the upload claimed to be.
    """
    return f"{tenant_id}/candidates/{candidate_id}/documents/{document_id}.{kind}"


def document_text_key(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, document_id: uuid.UUID
) -> str:
    """Where the plain text read out of one CV is kept.

    Beside the uploaded file rather than in place of it, because an evidence
    span is an offset into *this* string: re-deriving text from the PDF later
    is not guaranteed to reproduce the same offsets, so a stored extraction
    would start quoting the wrong characters.

    The leading `{tenant_id}/` segment is load-bearing, same as `body_key` and
    `avatar_key`: it is the prefix a tenant erasure purges by.
    """
    return f"{tenant_id}/candidates/{candidate_id}/documents/{document_id}.txt"


def import_key(tenant_id: uuid.UUID, import_id: uuid.UUID, stem: str, kind: str) -> str:
    """Where one uploaded spreadsheet is kept, exactly as it arrived.

    Computed from the authenticated tenant and a freshly minted import id,
    never from the filename the browser sent — same rule as `document_key`,
    and same `{tenant_id}/` prefix a tenant erasure purges by.

    `stem` is the only part carrying information rather than identity: a CSV
    has no internal sheet name, so the upload route records here which of the
    two sheets the file is standing in for, and the job reads it back. It is
    one of a fixed set of our own words — never anything the client typed.
    """
    return f"{tenant_id}/imports/{import_id}/{stem}.{kind}"


def import_error_report_key(tenant_id: uuid.UUID, import_id: uuid.UUID) -> str:
    """Where the per-row problems of one import are kept.

    A file beside the upload rather than a column, because a five-hundred-row
    migration can produce hundreds of problems and Postgres is the wrong
    place for a document a person reads once.
    """
    return f"{tenant_id}/imports/{import_id}/errors.txt"


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
    async def get_bytes(self, key: str) -> bytes | None: ...
    async def delete(self, *keys: str) -> None: ...
    async def put_bytes(self, key: str, content: bytes, content_type: str) -> None: ...
    async def presigned_get(self, key: str, ttl_seconds: int) -> str: ...


class R2BodyStore:
    def __init__(self) -> None:
        self._session = aioboto3.Session()

    @asynccontextmanager
    async def _client(self):
        """Open a client for one operation.

        The region is passed explicitly. botocore does not fail without one —
        it quietly defaults S3 to `us-east-1` — and that is the problem: the
        region is baked into the credential scope of the SigV4 signature, so a
        deploy host that happens to export `AWS_DEFAULT_REGION` would silently
        change how requests are signed. R2 tolerates some values, but which
        ones is not a property worth depending on. Cloudflare documents `auto`;
        pinning it makes signing independent of ambient AWS configuration.

        Construction itself is the other thing that can fail, and it failed in
        production: the service was deployed with no `R2_*` variables at all,
        so botocore got an empty endpoint and raised a bare
        `ValueError: Invalid endpoint:` from deep inside its endpoint
        resolver. That is a misconfiguration exactly like a missing bucket —
        it answers the same way forever — but it arrived as an unexplained
        500. Only the *construction* is wrapped: the body of the `async with`
        runs outside the guard, so a genuine `ValueError` raised by an
        operation is not relabelled as an operator error.
        """
        creator = self._session.client(
            "s3",
            endpoint_url=settings.R2_ENDPOINT_URL,
            region_name=settings.R2_REGION,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        )
        try:
            client = await creator.__aenter__()
        except ValueError as exc:
            raise BodyStoreMisconfigured(
                "R2 client cannot be built: endpoint "
                f"{settings.R2_ENDPOINT_URL!r}, bucket "
                f"{settings.R2_BUCKET_NAME!r}. Check the R2_* environment "
                "variables on this service."
            ) from exc
        try:
            yield client
        finally:
            await creator.__aexit__(*sys.exc_info())

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

    async def put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        """Store binary content (the avatar path — everything else here is text).

        Kept as a sibling to `put` rather than a replacement so every existing
        caller storing `str` bodies is untouched.
        """
        async with self._client() as s3:
            try:
                await s3.put_object(
                    Bucket=settings.R2_BUCKET_NAME,
                    Key=key,
                    Body=content,
                    ContentType=content_type,
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == _NO_SUCH_BUCKET:
                    raise BodyStoreMisconfigured(
                        f"R2 bucket {settings.R2_BUCKET_NAME!r} does not exist. "
                        "No object can be stored until it is created."
                    ) from exc
                raise

    async def presigned_get(self, key: str, ttl_seconds: int) -> str:
        """A time-limited URL for reading `key` directly from R2.

        Used for the avatar path so the browser fetches the image straight
        from R2 rather than proxying bytes through the API.
        """
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.R2_BUCKET_NAME, "Key": key},
                ExpiresIn=ttl_seconds,
            )

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

    async def get_bytes(self, key: str) -> bytes | None:
        """`get`, without the decode. Same absence rules, same bucket rule.

        A CV is a PDF or a DOCX, and decoding either as UTF-8 raises rather
        than returning something wrong — which would turn a readable document
        into a job crash. The bytes are what the parser wants.
        """
        async with self._client() as s3:
            try:
                obj = await s3.get_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == _NO_SUCH_BUCKET:
                    raise BodyStoreMisconfigured(
                        f"R2 bucket {settings.R2_BUCKET_NAME!r} does not exist. "
                        "No document can be read until it is created."
                    ) from exc
                if code in _ABSENT_CODES:
                    return None
                raise
            return await obj["Body"].read()

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
        self.binary_objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, content: str) -> None:
        self.objects[key] = content

    async def get(self, key: str) -> str | None:
        return self.objects.get(key)

    async def get_bytes(self, key: str) -> bytes | None:
        stored = self.binary_objects.get(key)
        return stored[0] if stored else None

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.objects.pop(key, None)
            self.binary_objects.pop(key, None)

    async def put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        self.binary_objects[key] = (content, content_type)

    async def presigned_get(self, key: str, ttl_seconds: int) -> str:
        return f"https://fake-r2.test/{key}?ttl={ttl_seconds}"
