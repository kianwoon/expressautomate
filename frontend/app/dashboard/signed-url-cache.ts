/**
 * A short-lived memo for presigned image URLs.
 *
 * Avatars and logos are not fetched as bytes from our API — the API answers
 * with a URL signed for a few minutes and the browser loads the object from R2
 * directly. That made every panel open cost two serial round trips (sign, then
 * download) and, worse, made the download unavoidable: a presigned URL carries
 * a fresh signature and timestamp every time it is minted, so the `src` was a
 * different string on every open and the browser's own image cache could never
 * hit. The bytes came down again for a picture already on screen a moment ago.
 *
 * Two things fix that together, and neither works alone:
 *
 * 1. The API signs `response-cache-control` into the URL, so R2 tells the
 *    browser it may keep the bytes (see `_cache_control` in
 *    `candidates_avatar.py`).
 * 2. This module hands back the *same* URL string for the same image within
 *    its lifetime, which is what lets that permission be used.
 *
 * Deliberately module-scoped and in memory only. Not `localStorage`: a signed
 * URL is a capability, and one written to disk outlives both the tab and the
 * permission it was granted under. Everything here dies with the page.
 *
 * The key carries the image's version (`avatar_updated_at` / `logo_updated_at`),
 * so replacing a photo misses the cache by construction rather than by
 * remembering to invalidate. `forget` exists for deletion, which has no new
 * version to move to.
 */

type Entry = { url: string; expiresAt: number; inFlight?: Promise<unknown> };

const entries = new Map<string, Entry>();

/** Re-sign this far before the URL actually dies. A cached URL handed out at
 *  the last moment can expire between the render and the image request, which
 *  shows as a broken photo rather than a slow one. */
const EXPIRY_MARGIN_MS = 30_000;

export function cacheKey(kind: string, id: string, version: string | null): string {
  return `${kind}:${id}:${version ?? ""}`;
}

/**
 * The cached URL for `key`, or `fetcher()`'s result — stored and shared.
 *
 * Concurrent callers for the same key await one request rather than each
 * making their own: the sourcing list renders many cards at once and several
 * of them are routinely the same client.
 *
 * `null` (the image does not exist) is deliberately NOT cached. It is the
 * cheap answer already — the caller skips the request entirely when the record
 * says there is no key — and caching it would keep a just-uploaded photo
 * hidden.
 */
export async function cachedSignedUrl<T extends { url: string; expires_in: number }>(
  key: string,
  fetcher: () => Promise<T | null>,
): Promise<T | null> {
  const now = Date.now();
  const hit = entries.get(key);
  if (hit) {
    if (hit.inFlight) return hit.inFlight as Promise<T | null>;
    // `expires_in` is what is LEFT, not what was originally granted — a caller
    // that scheduled a refresh off the full TTL after a cache hit would be
    // scheduling it from the wrong moment.
    if (hit.expiresAt > now) {
      return { url: hit.url, expires_in: Math.round((hit.expiresAt - now) / 1000) } as T;
    }
    entries.delete(key);
  }

  const inFlight = (async () => {
    try {
      const result = await fetcher();
      if (result) {
        entries.set(key, {
          url: result.url,
          expiresAt: Date.now() + Math.max(0, result.expires_in * 1000 - EXPIRY_MARGIN_MS),
        });
      } else {
        entries.delete(key);
      }
      return result;
    } catch (err) {
      // A failed request must not be remembered as one in flight forever.
      entries.delete(key);
      throw err;
    }
  })();

  entries.set(key, { url: "", expiresAt: 0, inFlight });
  return inFlight;
}

/** Drop every entry for one image, whatever version it was cached under.
 *  Deletion is the case the version-keyed key cannot cover: there is no newer
 *  version to miss on, only an absence. */
export function forget(kind: string, id: string): void {
  const prefix = `${kind}:${id}:`;
  for (const key of entries.keys()) {
    if (key.startsWith(prefix)) entries.delete(key);
  }
}

/** Test seam. Module state outlives a test file's individual cases, and a URL
 *  cached by one would otherwise satisfy the next one's fetch assertion. */
export function clearSignedUrlCache(): void {
  entries.clear();
}
