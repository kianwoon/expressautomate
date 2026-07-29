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
