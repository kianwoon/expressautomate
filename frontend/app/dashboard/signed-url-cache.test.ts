import { beforeEach, describe, expect, it, vi } from "vitest";

import { cacheKey, cachedSignedUrl, clearSignedUrlCache, forget } from "./signed-url-cache";

/**
 * What this pins is the property the whole optimisation rests on: the SAME
 * URL string comes back for the same image. A cache that merely avoided the
 * API round trip but handed out a freshly signed URL each time would look
 * like a win in the network panel and change nothing at all — the browser
 * keys its image cache on the URL, so a different string is a fresh download
 * of bytes it already has.
 *
 * allow-hardcode: the strings below are test fixtures.
 */

const KIND = "candidate-avatar";

beforeEach(() => {
  clearSignedUrlCache();
});

describe("cachedSignedUrl", () => {
  it("returns the identical URL on a repeat read, without asking again", async () => {
    let minted = 0;
    const sign = vi.fn(async () => ({ url: `https://r2.test/a?sig=${(minted += 1)}`, expires_in: 300 }));
    const key = cacheKey(KIND, "cand-1", "2026-08-03T00:00:00Z");

    const first = await cachedSignedUrl(key, sign);
    const second = await cachedSignedUrl(key, sign);

    expect(sign).toHaveBeenCalledTimes(1);
    expect(second?.url).toBe(first?.url);
  });

  it("shares one request between callers that arrive together", async () => {
    // The sourcing list mounts many cards at once, several of them the same
    // client. Without this they would each sign the same logo for themselves.
    const sign = vi.fn(async () => ({ url: "https://r2.test/logo", expires_in: 300 }));
    const key = cacheKey("client-logo", "cl-1", "2026-08-03T00:00:00Z");

    const [a, b, c] = await Promise.all([
      cachedSignedUrl(key, sign),
      cachedSignedUrl(key, sign),
      cachedSignedUrl(key, sign),
    ]);

    expect(sign).toHaveBeenCalledTimes(1);
    expect([a?.url, b?.url, c?.url]).toEqual([
      "https://r2.test/logo",
      "https://r2.test/logo",
      "https://r2.test/logo",
    ]);
  });

  it("misses when the image version changes, so a replacement is never stale", async () => {
    const sign = vi
      .fn()
      .mockResolvedValueOnce({ url: "https://r2.test/old", expires_in: 300 })
      .mockResolvedValueOnce({ url: "https://r2.test/new", expires_in: 300 });

    const before = await cachedSignedUrl(cacheKey(KIND, "cand-1", "2026-08-03T00:00:00Z"), sign);
    const after = await cachedSignedUrl(cacheKey(KIND, "cand-1", "2026-08-03T09:00:00Z"), sign);

    expect(before?.url).toBe("https://r2.test/old");
    expect(after?.url).toBe("https://r2.test/new");
  });

  it("re-signs once the URL is close enough to expiry to be risky", async () => {
    // A URL handed out at the last moment can die between the render and the
    // image request, which shows as a broken photo rather than a slow one.
    const sign = vi
      .fn()
      .mockResolvedValueOnce({ url: "https://r2.test/first", expires_in: 20 })
      .mockResolvedValueOnce({ url: "https://r2.test/second", expires_in: 300 });
    const key = cacheKey(KIND, "cand-2", "2026-08-03T00:00:00Z");

    const first = await cachedSignedUrl(key, sign);
    const second = await cachedSignedUrl(key, sign);

    expect(first?.url).toBe("https://r2.test/first");
    expect(second?.url).toBe("https://r2.test/second");
  });

  it("does not remember a failure as a request that never finishes", async () => {
    const key = cacheKey(KIND, "cand-3", "2026-08-03T00:00:00Z");
    const failing = vi.fn(async () => {
      throw new Error("nope");
    });

    await expect(cachedSignedUrl(key, failing)).rejects.toThrow("nope");

    const recovered = await cachedSignedUrl(key, async () => ({
      url: "https://r2.test/ok",
      expires_in: 300,
    }));
    expect(recovered?.url).toBe("https://r2.test/ok");
  });

  it("forgets every version of one image, which is what deletion needs", async () => {
    const sign = vi
      .fn()
      .mockResolvedValueOnce({ url: "https://r2.test/one", expires_in: 300 })
      .mockResolvedValueOnce({ url: "https://r2.test/two", expires_in: 300 });
    const key = cacheKey(KIND, "cand-4", "2026-08-03T00:00:00Z");

    await cachedSignedUrl(key, sign);
    forget(KIND, "cand-4");
    const again = await cachedSignedUrl(key, sign);

    expect(sign).toHaveBeenCalledTimes(2);
    expect(again?.url).toBe("https://r2.test/two");
  });
});
