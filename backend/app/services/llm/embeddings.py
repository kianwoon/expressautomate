"""Embeddings for semantic candidate matching.

The companion to `complete_json`: where that module asks a model for structured
text, this one asks for vectors. The two are kept separate because they talk to
different endpoints (`/chat/completions` vs `/embeddings`), return different
shapes, and fail for different reasons — and a sourcing run that falls back to
the six-component scorer when embeddings are absent is correct behaviour, not
an error to disguise.

Everything here is the OpenAI-compatible wire format, so the provider is a
configuration string rather than a code change. The default is OpenAI's
`text-embedding-3-small` (1536-dim); any compatible endpoint works.

Vectors are L2-normalised at the point they leave this module, because the
sourcing scorer compares them by cosine similarity and pgvector's cosine
operator (`<=>`) assumes nothing about magnitude. Normalising once at write
time means a query embeds, normalises, and compares — no per-comparison math.
"""

import math
import time
from dataclasses import dataclass, field

import httpx

from app.core.config import settings


class EmbeddingsError(Exception):
    """The embeddings endpoint returned an unusable response.

    Separate from a transport error for the same reason `LLMInvalidJSON` is:
    a timeout is worth retrying, a 400 or a malformed body is worth surfacing
    with what came back. The worker layer decides which is which.
    """


@dataclass
class EmbeddingResult:
    """One batch's worth of vectors plus the bookkeeping to log it."""

    vectors: list[list[float]]
    model: str
    prompt_tokens: int | None = None
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)


def _normalise(vec: list[float]) -> list[float]:
    """L2-normalise a vector in place, returning it.

    Cosine similarity is the metric for CV↔JD matching, and pgvector's cosine
    operator divides by the product of magnitudes — so a vector of magnitude
    other than 1 is compared correctly but at the cost of that division every
    query. Normalising once at write time makes the stored vector and the query
    vector both unit length, and the division a no-op.

    A zero vector is left untouched: it has no direction, and normalising it
    would divide by zero. A candidate whose CV embedded to all zeros has a
    bigger problem than its magnitude, and the cosine comparison will report
    it as unrelated to everything — which is the honest answer.
    """
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


async def embed_texts(
    texts: list[str],
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EmbeddingResult:
    """Embed a batch of texts, returning L2-normalised vectors in order.

    The provider is asked for everything in one call up to its own limit; the
    caller is responsible for chunking at `EMBEDDING_BATCH_SIZE` if the input
    may exceed it. Batching at the call site keeps this function a thin wrapper
    over a single endpoint, and a test that exercises one text exercises the
    same path as production.

    `transport` is the test seam, identical in purpose to `complete_json`'s:
    nothing in production passes it, and it is what keeps the suite from ever
    spending money on a real embedding.
    """
    model = model or settings.EMBEDDING_MODEL
    if not texts:
        # An empty batch is a no-op rather than an error: a backfill that found
        # nothing to embed has done its job, and raising here would make the
        # caller special-case the empty path it already handles by doing nothing.
        return EmbeddingResult(vectors=[], model=model)

    started = time.monotonic()
    payload = {"model": model, "input": texts}
    async with httpx.AsyncClient(
        base_url=base_url or settings.EMBEDDING_BASE_URL,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        transport=transport,
        headers={"Authorization": f"Bearer {api_key or settings.embedding_api_key()}"},
    ) as client:
        response = await client.post("/embeddings", json=payload)
        response.raise_for_status()
        body = response.json()

    data = body.get("data") or []
    if len(data) != len(texts):
        # The provider returned a different number of vectors than it was sent.
        # Reassembling them into the candidate rows would attach the wrong
        # vector to the wrong CV, which is worse than failing the batch.
        raise EmbeddingsError(
            f"expected {len(texts)} embeddings, got {len(data)}"
        )

    # The OpenAI spec does not guarantee order, so sort by the explicit index
    # the provider returns rather than trusting the array sequence. A provider
    # that omits `index` falls back to array position, which is the documented
    # invariant and correct for conformant responses.
    ordered = sorted(
        data, key=lambda item: item.get("index", 0) if isinstance(item, dict) else 0
    )
    vectors = []
    for item in ordered:
        embedding = (item or {}).get("embedding")
        if not embedding:
            raise EmbeddingsError("an embedding was empty or missing")
        vectors.append(_normalise([float(v) for v in embedding]))

    usage = body.get("usage") or {}
    return EmbeddingResult(
        vectors=vectors,
        model=body.get("model", model),
        prompt_tokens=usage.get("prompt_tokens"),
        latency_ms=int((time.monotonic() - started) * 1000),
        raw=body,
    )


async def embed_one(
    text: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[float]:
    """Embed a single text, returning one L2-normalised vector.

    A convenience over `embed_texts` for the per-run job-order embedding: the
    JD is embedded once per sourcing run, and a one-text call reads more
    clearly at the call site than `[result.vectors[0]]` would.
    """
    if not text or not text.strip():
        # An empty JD has nothing to embed. Returning an empty vector rather
        # than raising lets the caller treat "no semantic signal" uniformly —
        # the scorer abstains on a missing embedding, and an empty one is the
        # same absence.
        return []
    result = await embed_texts(
        [text],
        model=model,
        base_url=base_url,
        api_key=api_key,
        transport=transport,
    )
    return result.vectors[0]


class FakeEmbeddings:
    """Test double. Queue vectors; assert on the texts that were embedded.

    Substitutable for `embed_texts` / `embed_one` by callable shape, mirroring
    `FakeLLM`. A test wires it in where production passes a real transport.

    `embed_one` pops the next queued vector; `embed_texts` pops one vector per
    text, so a test that queues N vectors and embeds N texts gets them back in
    order. Vectors are returned as-is (not normalised) so a test can assert on
    the exact bytes it queued.
    """

    def __init__(self, *vectors: list[float]) -> None:
        self.vectors: list[list[float]] = [list(v) for v in vectors]
        self.texts: list[str] = []

    async def __call__(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> EmbeddingResult:
        self.texts.extend(texts)
        out: list[list[float]] = []
        for _ in texts:
            if not self.vectors:
                raise AssertionError("FakeEmbeddings ran out of queued vectors")
            out.append(self.vectors.pop(0))
        return EmbeddingResult(vectors=out, model=model or settings.EMBEDDING_MODEL)
