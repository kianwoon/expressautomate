import { createHash, timingSafeEqual } from 'node:crypto';

import type { FastifyReply, FastifyRequest } from 'fastify';

/**
 * Constant-time comparison of a presented secret against the expected one.
 *
 * Why not `presented === expected`: JavaScript string equality (like memcmp)
 * returns at the first differing byte, so the time it takes leaks how long a
 * correct prefix the caller guessed. An attacker who can time the endpoint
 * recovers the secret one byte at a time instead of brute-forcing it whole.
 * `timingSafeEqual` compares every byte regardless.
 *
 * Both sides are hashed first for two reasons: `timingSafeEqual` *throws* on
 * length mismatch (which would itself be a timing/behaviour oracle for the
 * secret's length), and SHA-256 gives two fixed 32-byte buffers whatever the
 * inputs were.
 */
export function secretMatches(presented: string, expected: string): boolean {
  const a = createHash('sha256').update(presented, 'utf8').digest();
  const b = createHash('sha256').update(expected, 'utf8').digest();
  return timingSafeEqual(a, b);
}

/** Extract the token from `Authorization: Bearer <token>`; '' when absent. */
function bearerToken(header: string | undefined): string {
  if (!header) return '';
  const match = /^Bearer (.+)$/.exec(header);
  return match?.[1] ?? '';
}

/**
 * Shared-secret guard for every route except `/health` (plan §4).
 *
 * Belt and braces alongside "no public route": private networking is a Koyeb
 * config detail that can drift exactly like the Route and health-check
 * settings CLAUDE.md records.
 */
export function requireSharedSecret(expected: string) {
  return async function guard(request: FastifyRequest, reply: FastifyReply): Promise<void> {
    const presented = bearerToken(request.headers.authorization);
    if (secretMatches(presented, expected)) return;

    request.log.warn({ path: request.url }, 'rejected: bad or missing shared secret');
    // No detail in the body: telling the caller whether the header was missing,
    // malformed or merely wrong is free reconnaissance.
    await reply.code(401).send({ error: 'unauthorized' });
  };
}
