"""Source-email body storage (plan §2.3 as amended, §10).

Keys are *derived*, never generated. That is the property the fetch job leans
on: a retry writes to the same key as the attempt that failed, so it overwrites
its own half-finished work instead of leaving an object nothing points at. Get
that wrong and every Graph throttle leaks storage that no row references and no
purge will ever find.

`InMemoryBodyStore` is the double the rest of the pipeline's tests run against,
so its behaviour has to match the real store where the pipeline depends on it —
notably that deleting an absent key is not an error.
"""

import uuid

import pytest
from botocore.exceptions import ClientError

from app.core.config import settings
from app.services.storage.r2 import (
    BodyDeletionFailed,
    InMemoryBodyStore,
    R2BodyStore,
    body_key,
)


def test_the_same_message_always_maps_to_the_same_key():
    """A retry must overwrite, never orphan."""
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()

    first = body_key(tenant, mailbox, "AAA-immutable", "txt")
    second = body_key(tenant, mailbox, "AAA-immutable", "txt")

    assert first == second


def test_keys_are_scoped_by_tenant_then_mailbox():
    """Tenant deletion purges by prefix, so the tenant has to come first."""
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()

    key = body_key(tenant, mailbox, "AAA", "txt")

    assert key.startswith(f"{tenant}/{mailbox}/")
    assert key.endswith(".txt")


def test_different_messages_never_collide():
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()

    keys = {body_key(tenant, mailbox, f"MSG-{n}", "txt") for n in range(50)}

    assert len(keys) == 50


def test_the_two_body_formats_get_separate_keys():
    """The text body feeds the model; the HTML is kept as the source of truth.
    One key for both would mean the second write destroyed the first."""
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()

    assert body_key(tenant, mailbox, "AAA", "txt") != body_key(tenant, mailbox, "AAA", "html")


def test_a_message_id_cannot_invent_a_key_prefix():
    """Graph immutable ids are base64-ish and contain `/`, `+` and `=`.

    Used raw, a `/` would silently push one message's body into a different
    logical folder — breaking the tenant-prefix purge and making two ids that
    differ only around a slash collide.
    """
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()

    key = body_key(tenant, mailbox, "AAkAL/g+w==", "html")
    filename = key.rsplit("/", 1)[-1]

    assert key.count("/") == 2, "exactly tenant/mailbox/filename"
    assert "/" not in filename
    assert filename.endswith(".html")


def test_ids_differing_only_by_a_slash_get_different_keys():
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()

    assert body_key(tenant, mailbox, "AA/BB", "txt") != body_key(tenant, mailbox, "AABB", "txt")


async def test_the_fake_store_round_trips():
    store = InMemoryBodyStore()
    await store.put("k", "hello")

    assert await store.get("k") == "hello"


async def test_reading_an_absent_key_returns_none_rather_than_raising():
    """A purged body is an expected state, not an error: retention deletes the
    object and leaves the row, so anything reading a body must cope."""
    assert await InMemoryBodyStore().get("never-written") is None


async def test_deleting_removes_the_object():
    store = InMemoryBodyStore()
    await store.put("k", "hello")

    await store.delete("k")

    assert await store.get("k") is None


async def test_deleting_a_missing_key_is_not_an_error():
    """`purge_expired` reruns after partial failure; it must be idempotent."""
    await InMemoryBodyStore().delete("never-existed")


async def test_deleting_nothing_at_all_is_a_no_op():
    """The real S3 API rejects an empty delete batch, so the guard has to live
    in the store rather than in every caller."""
    await InMemoryBodyStore().delete()


async def test_deleting_several_keys_at_once():
    store = InMemoryBodyStore()
    await store.put("a", "1")
    await store.put("b", "2")
    await store.put("c", "3")

    await store.delete("a", "b")

    assert await store.get("a") is None
    assert await store.get("b") is None
    assert await store.get("c") == "3"


async def test_a_body_survives_unicode_and_is_returned_unchanged():
    """Recruitment mail carries currency symbols and names in many scripts;
    a lossy round trip would corrupt evidence offsets."""
    store = InMemoryBodyStore()
    body = "Salary: S$6,000–7,000 · 曾先生 · café"

    await store.put("k", body)

    assert await store.get("k") == body


@pytest.mark.parametrize("kind", ["txt", "html"])
def test_key_kinds_are_reflected_in_the_extension(kind):
    key = body_key(uuid.uuid4(), uuid.uuid4(), "AAA", kind)

    assert key.endswith(f".{kind}")


# --- the real store's error handling ----------------------------------------
#
# The fake cannot prove any of this: absence and failure arrive from boto3 as
# the same exception type, distinguished only by a string buried in the
# response. Getting that branch wrong means a purged body raises instead of
# returning None, and every extraction replay after a retention sweep dies.


class _FakeS3:
    """Minimal async stand-in for the aioboto3 S3 client."""

    def __init__(
        self,
        *,
        get_error: ClientError | None = None,
        delete_errors: list[dict] | None = None,
    ) -> None:
        self.get_error = get_error
        self.delete_errors = delete_errors
        self.deleted: list[dict] = []

    async def __aenter__(self) -> "_FakeS3":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get_object(self, **kwargs):
        if self.get_error is not None:
            raise self.get_error
        raise AssertionError("this fake only models the failure paths")

    async def delete_objects(self, **kwargs):
        self.deleted.append(kwargs)
        # The real API answers 200 for the request and reports per-key failures
        # in `Errors`, so the fake has to model the response shape, not just
        # record the call.
        if self.delete_errors:
            return {"Errors": self.delete_errors}
        return {"Deleted": [{"Key": o["Key"]} for o in kwargs["Delete"]["Objects"]]}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


@pytest.mark.parametrize("code", ["NoSuchKey", "404"])
async def test_the_real_store_reports_a_purged_body_as_absent(monkeypatch, code):
    """Retention deletes the object and keeps the row, so a missing body is an
    expected state — and boto3 signals it with a ClientError, not a None."""
    store = R2BodyStore()
    monkeypatch.setattr(store, "_client", lambda: _FakeS3(get_error=_client_error(code)))

    assert await store.get("gone") is None


@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket", "InvalidAccessKeyId"])
async def test_the_real_store_still_raises_on_a_genuine_failure(monkeypatch, code):
    """A misconfiguration must not look identical to a purged body.

    `NoSuchBucket` is the dangerous one: read as absence it would make every
    read return None, so extraction would run on empty text and record
    confident nothing — for every tenant at once, without a single error.
    """
    store = R2BodyStore()
    monkeypatch.setattr(store, "_client", lambda: _FakeS3(get_error=_client_error(code)))

    with pytest.raises(ClientError):
        await store.get("k")


async def test_deletes_are_split_into_batches_the_api_accepts(monkeypatch):
    """S3 caps a batch delete at 1000 keys and rejects the whole request past
    that — which tenant deletion and a backlogged retention sweep both hit."""
    store = R2BodyStore()
    fake = _FakeS3()
    monkeypatch.setattr(store, "_client", lambda: fake)

    keys = [f"k{n}" for n in range(2500)]
    await store.delete(*keys)

    sizes = [len(call["Delete"]["Objects"]) for call in fake.deleted]
    assert sizes == [1000, 1000, 500]

    sent = [obj["Key"] for call in fake.deleted for obj in call["Delete"]["Objects"]]
    assert sent == keys, "every key must be deleted exactly once, in order"


async def test_a_partially_failed_delete_is_not_reported_as_success(monkeypatch):
    """`delete_objects` answers 200 and lists per-key failures in `Errors`.

    Ignoring that is a silent retention leak: `purge_expired` would null the
    row's keys while the objects survive in R2, and since the row no longer
    names them, no rerun could ever find them. Raising leaves the keys in
    place so the next sweep retries.
    """
    store = R2BodyStore()
    monkeypatch.setattr(
        store,
        "_client",
        lambda: _FakeS3(delete_errors=[{"Key": "b", "Code": "AccessDenied"}]),
    )

    with pytest.raises(BodyDeletionFailed) as excinfo:
        await store.delete("a", "b")

    assert "b" in str(excinfo.value)


async def test_a_fully_successful_delete_does_not_raise(monkeypatch):
    store = R2BodyStore()
    monkeypatch.setattr(store, "_client", lambda: _FakeS3())

    await store.delete("a", "b")


async def test_the_region_ignores_ambient_aws_configuration(monkeypatch):
    """The region is part of the SigV4 signature.

    botocore does not fail without one — it quietly defaults S3 to us-east-1,
    and picks up `AWS_DEFAULT_REGION` if the host exports it. Either way the
    signature changes and R2 rejects the request, with nothing in the failure
    naming the region as the cause. Asserted against a hostile value, because
    asserting the setting is non-empty would pass on its own default.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    # A placeholder endpoint, so the test exercises region resolution without
    # requiring R2 credentials to exist — CI deliberately has none, and no
    # request is ever issued here.
    monkeypatch.setattr(settings, "R2_ENDPOINT_URL", "https://example.r2.cloudflarestorage.com")

    async with R2BodyStore()._client() as client:
        assert client.meta.region_name == settings.R2_REGION
        assert client.meta.region_name != "eu-west-1"


async def test_the_real_store_skips_the_api_call_for_an_empty_delete(monkeypatch):
    """S3 rejects a delete with no objects, so the guard has to short-circuit
    before the client is ever opened."""
    store = R2BodyStore()
    fake = _FakeS3()

    def _never() -> _FakeS3:
        raise AssertionError("no client should be opened for an empty delete")

    monkeypatch.setattr(store, "_client", _never)

    await store.delete()

    assert fake.deleted == []


async def test_the_real_store_batches_deletes_into_one_call(monkeypatch):
    store = R2BodyStore()
    fake = _FakeS3()
    monkeypatch.setattr(store, "_client", lambda: fake)

    await store.delete("a", "b")

    assert len(fake.deleted) == 1, "one round trip, not one per key"
    assert fake.deleted[0]["Delete"]["Objects"] == [{"Key": "a"}, {"Key": "b"}]


async def test_a_missing_bucket_is_named_not_retried_blindly():
    """Found in production: the bucket had never been created, so every email
    failed at the body store with a raw NoSuchBucket traceback that read as a
    transient storage error being retried.

    A bucket that does not exist answers the same way forever. Saying so is the
    only thing that shortens the outage.
    """
    from botocore.exceptions import ClientError

    from app.services.storage.r2 import BodyStoreMisconfigured, R2BodyStore

    store = R2BodyStore()

    class _Missing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put_object(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "NoSuchBucket", "Message": "no"}}, "PutObject"
            )

    store._client = lambda: _Missing()

    with pytest.raises(BodyStoreMisconfigured) as exc:
        await store.put("k", "body")

    assert settings.R2_BUCKET_NAME in str(exc.value)
