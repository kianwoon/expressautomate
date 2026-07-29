import assert from 'node:assert/strict';
import { after, before, describe, test } from 'node:test';

import type { FastifyInstance } from 'fastify';

import { buildApp } from './app.js';
import { secretMatches } from './auth.js';
import { ConfigError, loadConfig } from './config.js';

const SECRET = 'test-shared-secret-not-a-real-one';

describe('gateway HTTP surface', () => {
  let app: FastifyInstance;

  before(() => {
    app = buildApp({ host: '127.0.0.1', port: 0, sharedSecret: SECRET });
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
    assert.equal(loadConfig({ WA_GATEWAY_SHARED_SECRET: 'x' }).port, 7300);
    assert.equal(
      loadConfig({ WA_GATEWAY_SHARED_SECRET: 'x', WA_GATEWAY_PORT: '8080' }).port,
      8080,
    );
    assert.throws(
      () => loadConfig({ WA_GATEWAY_SHARED_SECRET: 'x', WA_GATEWAY_PORT: 'nope' }),
      ConfigError,
    );
  });
});
