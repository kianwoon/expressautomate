import Fastify, { type FastifyInstance } from 'fastify';

import { requireSharedSecret } from './auth.js';
import type { GatewayConfig } from './config.js';

/**
 * P1 skeleton of the WA gateway (plan §1, §4). No Baileys, no sessions, no
 * database yet — those are P2/P3. What has to be right here is the shape:
 * an unauthenticated health check, and a shared secret on everything else.
 */
export function buildApp(config: GatewayConfig): FastifyInstance {
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

    /**
     * Deliberately trivial: it exists so the shared-secret guard is exercised
     * end to end and testable before any real route depends on it. The real
     * routes (`/sessions/:user_id/start|logout`, `/send`) arrive in P3/P4.
     */
    guarded.get('/status', async () => ({ status: 'ok', baileys: 'not-wired' }));
  });

  return app;
}
