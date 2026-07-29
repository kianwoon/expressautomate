import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { after, before, describe, test } from 'node:test';

import type { FastifyInstance } from 'fastify';

import { buildApp } from './app.js';
import { secretMatches } from './auth.js';
import { ConfigError, loadConfig } from './config.js';
import type { SendOutcome, SessionManager } from './sessions.js';

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
      sendMinIntervalSeconds: 30,
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
        sendMinIntervalSeconds: 30,
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

describe('POST /send (P4)', () => {
  let app: FastifyInstance;
  const sends: { ref: unknown; to: string; text: string }[] = [];
  let outcome: SendOutcome = { ok: true, status: 'connected', providerMessageId: 'WAMSG-1' };
  const fakeSessions = {
    send: async (ref: unknown, to: string, text: string) => {
      sends.push({ ref, to, text });
      return outcome;
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
        sendMinIntervalSeconds: 30,
      },
      fakeSessions,
    );
  });

  after(async () => {
    await app.close();
  });

  function send(payload: unknown, secret: string | null = SECRET) {
    return app.inject({
      method: 'POST',
      url: '/send',
      headers: secret === null ? {} : { authorization: `Bearer ${secret}` },
      payload: payload as object,
    });
  }

  test('is behind the shared secret like every other guarded route', async () => {
    const res = await send({ tenantId: 't', userId: 'u', to: '+6591234567', text: 'hi' }, null);
    assert.equal(res.statusCode, 401);
  });

  test('a successful send returns the provider message id', async () => {
    outcome = { ok: true, status: 'connected', providerMessageId: 'WAMSG-42' };
    const res = await send({ tenantId: 'tenant-1', userId: 'user-1', to: '+6591234567', text: 'hi' });
    assert.equal(res.statusCode, 200);
    assert.deepEqual(res.json(), { status: 'sent', providerMessageId: 'WAMSG-42' });
    assert.deepEqual(sends.at(-1), {
      ref: { tenantId: 'tenant-1', sessionId: 'user-1' },
      to: '+6591234567',
      text: 'hi',
    });
  });

  // The point of the loop: a refusal must name the status it actually is.
  // "Not connected" alone would leave the recruiter unable to tell "wait a
  // moment" from "pair again", which are the two ends of this list.
  for (const status of ['pairing', 'reconnecting', 'disconnected', 'logged_out'] as const) {
    test(`refuses a ${status} session with 409 naming that status`, async () => {
      outcome = { ok: false, status };
      const res = await send({ tenantId: 't', userId: 'u', to: '+6591234567', text: 'hi' });
      assert.equal(res.statusCode, 409);
      assert.equal(res.json().status, status);
    });
  }

  // 409 and 422 are two different facts, and the API records them as two
  // different rows: 409 means we never tried, so no activity row exists at
  // all; 422 means WhatsApp itself refused on a live socket, which is the
  // only thing that earns a `failed` row. Collapsing them would make every
  // refusal look like a broken pairing.
  test('a message WhatsApp refused on a live socket is 422 carrying its own words', async () => {
    outcome = { ok: false, status: 'connected', refusal: 'not-authorized: blocked by recipient' };
    const res = await send({ tenantId: 't', userId: 'u', to: '+6591234567', text: 'hi' });
    assert.equal(res.statusCode, 422);
    assert.equal(res.json().error, 'not-authorized: blocked by recipient');
    assert.equal(res.json().status, 'connected');
  });

  // The third fact, and the one that costs a candidate a duplicate message if
  // it is folded into either of the others. 502 rather than 422 is what makes
  // the API write `unknown` instead of `failed`: it maps a gateway 5xx to "we
  // do not know", and a 4xx to "WhatsApp said no". Without this test the
  // classification could be right in `sessions.ts` and thrown away here, and
  // every other test would still pass.
  test('a send whose outcome we never learned is 502, so the API records unknown', async () => {
    outcome = { ok: false, status: 'connected', indeterminate: 'Timed Out' };
    const res = await send({ tenantId: 't', userId: 'u', to: '+6591234567', text: 'hi' });
    assert.equal(res.statusCode, 502, 'a 4xx here would be recorded as a failure');
    assert.equal(res.json().error, 'Timed Out');
    assert.equal(res.json().status, 'connected');
  });

  test('a missing recipient or empty text is 400, not an empty message sent to nobody', async () => {
    outcome = { ok: true, status: 'connected', providerMessageId: 'WAMSG-1' };
    const before = sends.length;
    assert.equal((await send({ tenantId: 't', userId: 'u', text: 'hi' })).statusCode, 400);
    assert.equal((await send({ tenantId: 't', userId: 'u', to: '+65', text: '' })).statusCode, 400);
    assert.equal(sends.length, before, 'nothing reached the SessionManager');
  });

  test('there is no bulk send endpoint, and that omission is the rate limit (plan §9)', async () => {
    for (const url of ['/send/bulk', '/broadcast', '/sends']) {
      const res = await app.inject({
        method: 'POST',
        url,
        headers: { authorization: `Bearer ${SECRET}` },
        payload: { tenantId: 't', userId: 'u', to: ['a', 'b'], text: 'hi' },
      });
      assert.equal(res.statusCode, 404, `${url} must not exist`);
    }
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
