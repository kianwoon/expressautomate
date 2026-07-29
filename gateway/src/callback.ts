/**
 * Push a status/QR change to FastAPI's `POST /api/wa/internal/status`
 * (plan §5, §6) — the only way `wa_sessions.status` gets written, and the
 * trigger for the `wa_session` SSE nudge the settings page refetches on.
 *
 * No `qr` field is sent: the QR is never persisted (plan §5), so the only
 * thing this callback durably records is that *something* changed —
 * `GET /api/wa/session` asks this gateway directly for the live QR.
 */

import type { SessionRef } from './store.js';
import type { SessionSnapshot } from './sessions.js';

export function makeStatusPusher(
  apiCallbackUrl: string | undefined,
  sharedSecret: string,
  log: { warn: (obj: unknown, msg: string) => void },
) {
  return async function push(
    ref: SessionRef,
    snapshot: SessionSnapshot,
    statusDetail: string | null,
  ): Promise<void> {
    if (!apiCallbackUrl) return;
    try {
      const response = await fetch(`${apiCallbackUrl}/api/wa/internal/status`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${sharedSecret}`,
        },
        body: JSON.stringify({
          tenant_id: ref.tenantId,
          user_id: ref.sessionId,
          status: snapshot.status,
          status_detail: statusDetail,
          phone_e164: snapshot.phoneNumber,
          connected_at: snapshot.connectedAt,
          qr_expires_at: snapshot.expiresAt,
        }),
        signal: AbortSignal.timeout(5_000),
      });
      if (!response.ok) {
        log.warn({ status: response.status }, 'wa_internal_status_push_rejected');
      }
    } catch (error) {
      // Never throw out of here — see this file's docstring and
      // `SessionManager.#push`'s own try/catch around the callback. A lost
      // nudge costs the settings page a manual refresh, nothing durable.
      log.warn({ error: String(error) }, 'wa_internal_status_push_failed');
    }
  };
}
