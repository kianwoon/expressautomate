/**
 * The two P3-mandatory checks this file adds before the gateway opens its
 * first connection (plan "P3 carries two requirements from the P2 review").
 * Real Postgres, same reasoning as store.test.ts: `rolbypassrls` and
 * `sslmode` are both facts about a real connection, not something a double
 * could stand in for.
 */

import assert from 'node:assert/strict';
import { after, describe, test } from 'node:test';

import { Pool } from 'pg';

import { assertRlsNotBypassed, ensureWaSessionRow, RlsBypassError, sslConfigFor } from './db.js';

const APP_DSN = process.env.WA_GATEWAY_TEST_DATABASE_URL ?? '';
const BYPASS_DSN = process.env.WA_GATEWAY_TEST_BYPASS_DATABASE_URL ?? '';
const SKIP = APP_DSN === '' ? 'set WA_GATEWAY_TEST_DATABASE_URL (see scripts/test-db.sh)' : false;

describe('assertRlsNotBypassed', { skip: SKIP }, () => {
  const pools: Pool[] = [];
  after(async () => {
    await Promise.all(pools.map((p) => p.end()));
  });

  test('the restricted app role — the one production actually uses — is admitted', async () => {
    const pool = new Pool({ connectionString: APP_DSN });
    pools.push(pool);
    await assert.doesNotReject(() => assertRlsNotBypassed(pool));
  });

  test('a role that can bypass RLS is refused', { skip: BYPASS_DSN === '' ? 'set WA_GATEWAY_TEST_BYPASS_DATABASE_URL' : false }, async () => {
    const pool = new Pool({ connectionString: BYPASS_DSN });
    pools.push(pool);
    await assert.rejects(() => assertRlsNotBypassed(pool), RlsBypassError);
  });
});

describe('sslConfigFor', () => {
  test('no sslmode (the local test Postgres) means no TLS', () => {
    assert.equal(sslConfigFor('postgresql://u:p@localhost:5432/db'), false);
  });

  test('sslmode=disable means no TLS', () => {
    assert.equal(sslConfigFor('postgresql://u:p@localhost:5432/db?sslmode=disable'), false);
  });

  test('sslmode=require encrypts without verifying — the Koyeb-cert case', () => {
    assert.deepEqual(sslConfigFor('postgresql://u:p@host:5432/db?sslmode=require'), {
      rejectUnauthorized: false,
    });
  });

  test('an unparseable URL degrades to no TLS rather than throwing', () => {
    assert.equal(sslConfigFor('not a url'), false);
  });
});

describe('ensureWaSessionRow', { skip: SKIP }, () => {
  const pools: Pool[] = [];
  after(async () => {
    await Promise.all(pools.map((p) => p.end()));
  });

  test('id equals user_id, and a second call is a no-op', async () => {
    const pool = new Pool({ connectionString: APP_DSN });
    pools.push(pool);
    const client = await pool.connect();
    const tenantId = crypto.randomUUID();
    const userId = crypto.randomUUID();
    try {
      await client.query('BEGIN');
      await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', tenantId]);
      await client.query('INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $2)', [
        tenantId,
        `agency-${tenantId.slice(0, 8)}`,
      ]);
      await client.query(
        "INSERT INTO users (id, tenant_id, email, role) VALUES ($1, $2, $3, 'recruiter')",
        [userId, tenantId, `r-${userId.slice(0, 8)}@example.test`],
      );
      await client.query('COMMIT');

      await ensureWaSessionRow(pool, { tenantId, sessionId: userId });
      await ensureWaSessionRow(pool, { tenantId, sessionId: userId }); // no-op, no throw

      await client.query('BEGIN');
      await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', tenantId]);
      const { rows } = await client.query('SELECT id, user_id, status FROM wa_sessions WHERE user_id = $1', [
        userId,
      ]);
      await client.query('COMMIT');
      assert.equal(rows.length, 1);
      assert.equal(rows[0].id, userId);
      assert.equal(rows[0].status, 'pairing');
    } finally {
      await client.query('BEGIN');
      await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', tenantId]);
      await client.query('DELETE FROM wa_sessions');
      await client.query('DELETE FROM users');
      await client.query('DELETE FROM tenants');
      await client.query('COMMIT');
      client.release();
    }
  });
});
