import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { after, before, describe, test } from 'node:test';

import type { FastifyInstance } from 'fastify';

import { buildApp } from './app.js';
import { secretMatches } from './auth.js';
import { ConfigError, loadConfig } from './config.js';
import type { SessionManager } from './sessions.js';

const SECRET = 'test-shared-secret-not-a-real-one';

describe('gateway HTTP surface', () => {
  let app: FastifyInstance;

  before(() => {
    app = buildApp({
      host: '127.0.0.1',
      port: 0,
      sharedSecret: SECRET,
      // P2 widened the config; the HTTP surface does not use these yet.
      databaseUrl: 'postgresql://unused:unused@127.0.0.1:1/unused',
      encryptionKey: randomBytes(32),
    });
  });

  after(async () => {
    await app.close();
  });

  test('health returns 200 with no auth at all', async () => {
    const res = await app.inject({ method: 'GET', url: '/health' });
    assert.equal(res.statusCode, 200);
    assert.deepEqual(res.json(), { status: 'ok' });
  });

  test('a guarded route with no secret is 401', async () => {
    const res = await app.inject({ method: 'GET', url: '/status' });
    assert.equal(res.statusCode, 401);
    // No detail: the body must not say *why* it failed.
    assert.deepEqual(res.json(), { error: 'unauthorized' });
  });

  test('a guarded route with the wrong secret is 401', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/status',
      headers: { authorization: 'Bearer definitely-not-the-secret' },
    });
    assert.equal(res.statusCode, 401);
  });

  test('a near-miss secret (correct prefix) is still 401', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/status',
      headers: { authorization: `Bearer ${SECRET.slice(0, -1)}` },
    });
    assert.equal(res.statusCode, 401);
  });

  test('a malformed Authorization header is 401, not a crash', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/status',
      headers: { authorization: SECRET },
    });
    assert.equal(res.statusCode, 401);
  });

  test('the right secret reaches the stub route', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/status',
      headers: { authorization: `Bearer ${SECRET}` },
    });
    assert.equal(res.statusCode, 200);
    assert.equal(res.json().baileys, 'not-wired');
  });
});

describe('/sessions/* routes (P3)', () => {
  let app: FastifyInstance;
  const calls: { method: string; ref: unknown }[] = [];
  const fakeSessions = {
    status: async (ref: unknown) => {
      calls.push({ method: 'status', ref });
      return { status: 'disconnected', qr: null, expiresAt: null, phoneNumber: null, connectedAt: null };
    },
    pair: async (ref: unknown) => {
      calls.push({ method: 'pair', ref });
      return { status: 'pairing', qr: 'abc123', expiresAt: '2026-07-29T00:00:20Z', phoneNumber: null, connectedAt: null };
    },
    disconnect: async (ref: unknown) => {
      calls.push({ method: 'disconnect', ref });
      return { status: 'disconnected', qr: null, expiresAt: null, phoneNumber: null, connectedAt: null };
    },
  } as unknown as SessionManager;

  before(() => {
    app = buildApp(
      {
        host: '127.0.0.1',
        port: 0,
        sharedSecret: SECRET,
        databaseUrl: 'postgresql://unused:unused@127.0.0.1:1/unused',
        encryptionKey: randomBytes(32),
      },
      fakeSessions,
    );
  });

  after(async () => {
    await app.close();
  });

  test('GET /sessions/status requires the shared secret like every other guarded route', async () => {
    const res = await app.inject({ method: 'GET', url: '/sessions/status?tenantId=t&userId=u' });
    assert.equal(res.statusCode, 401);
  });

  test('GET /sessions/status forwards tenantId/userId from the query string', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/sessions/status?tenantId=tenant-1&userId=user-1',
      headers: { authorization: `Bearer ${SECRET}` },
    });
    assert.equal(res.statusCode, 200);
    assert.deepEqual(res.json(), { status: 'disconnected', qr: null, expiresAt: null, phoneNumber: null, connectedAt: null });
    assert.deepEqual(calls.at(-1), { method: 'status', ref: { tenantId: 'tenant-1', sessionId: 'user-1' } });
  });

  test('GET /sessions/status without tenantId or userId is 400, not a crash', async () => {
    const res = await app.inject({
      method: 'GET',
      url: '/sessions/status?tenantId=only-one',
      headers: { authorization: `Bearer ${SECRET}` },
    });
    assert.equal(res.statusCode, 400);
  });

  test('POST /sessions/pair returns the QR the manager produced', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/sessions/pair',
      headers: { authorization: `Bearer ${SECRET}` },
      payload: { tenantId: 'tenant-1', userId: 'user-1' },
    });
    assert.equal(res.statusCode, 200);
    assert.equal(res.json().qr, 'abc123');
  });

  test('POST /sessions/disconnect', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/sessions/disconnect',
      headers: { authorization: `Bearer ${SECRET}` },
      payload: { tenantId: 'tenant-1', userId: 'user-1' },
    });
    assert.equal(res.statusCode, 200);
    assert.equal(res.json().status, 'disconnected');
  });
});

describe('secret comparison', () => {
  test('matches only the exact secret, whatever the length', () => {
    assert.equal(secretMatches(SECRET, SECRET), true);
    assert.equal(secretMatches('', SECRET), false);
    assert.equal(secretMatches(`${SECRET}x`, SECRET), false);
    assert.equal(secretMatches(SECRET.toUpperCase(), SECRET), false);
  });
});

describe('config', () => {
  test('refuses to start without a shared secret', () => {
    assert.throws(() => loadConfig({}), ConfigError);
    assert.throws(() => loadConfig({ WA_GATEWAY_SHARED_SECRET: '' }), ConfigError);
  });

  test('defaults to the port the plan names, and honours an override', () => {
    // The rest of the environment is P2's; see crypto.test.ts for its own
    // refuse-to-start cases.
    const env = {
      WA_GATEWAY_SHARED_SECRET: 'x',
      WA_GATEWAY_DATABASE_URL: 'postgresql://u:p@localhost:5432/db',
      WA_GATEWAY_ENCRYPTION_KEY: randomBytes(32).toString('base64'),
    };
    assert.equal(loadConfig(env).port, 7300);
    assert.equal(loadConfig({ ...env, WA_GATEWAY_PORT: '8080' }).port, 8080);
    assert.throws(() => loadConfig({ ...env, WA_GATEWAY_PORT: 'nope' }), ConfigError);
  });
});
