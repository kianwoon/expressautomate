import Fastify, { type FastifyInstance } from 'fastify';

import { requireSharedSecret } from './auth.js';
import type { GatewayConfig } from './config.js';
import type { SessionManager, SessionSnapshot } from './sessions.js';

/**
 * The WA gateway's HTTP surface (plan §1, §4-§6). P1's skeleton — health
 * check open, everything else behind the shared secret — is unchanged; P3
 * adds the three routes FastAPI's `app/services/wa_gateway.py` calls.
 *
 * `sessions` is optional so `app.test.ts`'s P1/P2 assertions about the
 * skeleton itself (no `SessionManager`, no database) keep working unmodified.
 */
export function buildApp(config: GatewayConfig, sessions?: SessionManager): FastifyInstance {
  const app = Fastify({
    logger: { level: process.env.LOG_LEVEL ?? 'info' },
  });

  /**
   * Koyeb's health check hits this. It must be 200 and must NOT require auth:
   * CLAUDE.md records a deploy that stayed PENDING until CI timed out because
   * the configured health-check path 404ed. An authenticated health check
   * fails the same way, with a 401 instead.
   */
  app.get('/health', async () => ({ status: 'ok' }));

  // Everything below is guarded. Registered inside an encapsulated child
  // context so the hook cannot leak onto /health above, and so a route added
  // later is guarded by default rather than by remembering to add a hook.
  app.register(async (guarded) => {
    guarded.addHook('onRequest', requireSharedSecret(config.sharedSecret));

    guarded.get('/status', async () => ({ status: 'ok', baileys: sessions ? 'wired' : 'not-wired' }));

    if (!sessions) return;

    // `GET` with a query string, not a body: some proxies drop a body on a
    // GET, and `app/services/wa_gateway.py`'s client sends `params` here for
    // exactly that reason.
    guarded.get('/sessions/status', async (request, reply) => {
      const ref = refFrom(request.query);
      if (!ref) return reply.code(400).send({ error: 'tenantId and userId are required' });
      return toWire(await sessions.status(ref));
    });

    guarded.post('/sessions/pair', async (request, reply) => {
      const ref = refFrom(request.body);
      if (!ref) return reply.code(400).send({ error: 'tenantId and userId are required' });
      return toWire(await sessions.pair(ref));
    });

    /**
     * `POST /send` — plan §7.
     *
     * **Singular by design and it stays singular.** There is no `/send/bulk`,
     * no array of recipients, and no broadcast: plan §9 makes the API shape
     * itself the day-one ban-risk mitigation, so the missing endpoint is the
     * feature. Text only, one recipient, one message.
     *
     * A refused send is a 409 rather than a 500 or a silent success, and the
     * body names the status the session is actually in — the recruiter's
     * modal renders "still reconnecting" differently from "you were logged
     * out", and cannot if the gateway only says "not connected".
     */
    guarded.post('/send', async (request, reply) => {
      const ref = refFrom(request.body);
      if (!ref) return reply.code(400).send({ error: 'tenantId and userId are required' });
      const body = request.body as Record<string, unknown>;
      const to = typeof body['to'] === 'string' ? body['to'] : '';
      const text = typeof body['text'] === 'string' ? body['text'] : '';
      if (to === '' || text === '') {
        return reply.code(400).send({ error: 'to and text are required' });
      }

      // `sessions.send` goes through the same public `status()` path
      // `/sessions/status` uses, so a session whose socket died with the last
      // deploy is resumed from stored credentials rather than reported gone.
      const outcome = await sessions.send(ref, to, text);
      if (!outcome.ok) {
        // Two different failures, two different codes, because the API turns
        // them into two different records. 409: never attempted, the session
        // was not connected — no activity row at all. 422: attempted on a
        // live socket and WhatsApp refused it — a `failed` row carrying this
        // exact `error` string. A 5xx is reserved for "we do not know what
        // happened", which is neither of these. 429: the spacing floor
        // (plan §9) has not elapsed — also never attempted.
        if ('retryAfterSeconds' in outcome) {
          reply.header('Retry-After', String(outcome.retryAfterSeconds));
          return reply
            .code(429)
            .send({ error: 'send spacing floor not yet elapsed', retryAfterSeconds: outcome.retryAfterSeconds });
        }
        if ('indeterminate' in outcome) {
          // The 5xx the comment above reserves for "we do not know what
          // happened", and the API turns it into an `unknown` row rather than
          // a failure. 502 rather than 503: the gateway is fine, the thing it
          // was talking to stopped answering mid-sentence.
          return reply.code(502).send({ error: outcome.indeterminate, status: 'connected' });
        }
        if ('refusal' in outcome) {
          return reply.code(422).send({ error: outcome.refusal, status: 'connected' });
        }
        return reply.code(409).send({ error: 'session is not connected', status: outcome.status });
      }
      return { status: 'sent', providerMessageId: outcome.providerMessageId };
    });

    guarded.post('/sessions/disconnect', async (request, reply) => {
      const ref = refFrom(request.body);
      if (!ref) return reply.code(400).send({ error: 'tenantId and userId are required' });
      return toWire(await sessions.disconnect(ref));
    });
  });

  return app;
}

/** Both callers (`refBody` on POST, query string on GET) land here as plain objects. */
function refFrom(body: unknown): { tenantId: string; sessionId: string } | null {
  if (typeof body !== 'object' || body === null) return null;
  const record = body as Record<string, unknown>;
  const tenantId = record['tenantId'];
  const userId = record['userId'];
  if (typeof tenantId !== 'string' || tenantId === '') return null;
  if (typeof userId !== 'string' || userId === '') return null;
  // `sessionId` here is the recruiter's `user_id` — see sessions.ts's
  // "Session identity" note for why the two are the same value.
  return { tenantId, sessionId: userId };
}

function toWire(snapshot: SessionSnapshot) {
  return {
    status: snapshot.status,
    qr: snapshot.qr,
    expiresAt: snapshot.expiresAt,
    phoneNumber: snapshot.phoneNumber,
    connectedAt: snapshot.connectedAt,
  };
}
