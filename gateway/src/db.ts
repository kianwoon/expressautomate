/**
 * The gateway's own Postgres pool (plan §1, §3) — and the two guarantees P3
 * is required to add before it opens the first connection of this service
 * (plan "P3 carries two requirements from the P2 review").
 */

import { Pool } from 'pg';

import type { SessionRef } from './store.js';

export class RlsBypassError extends Error {}

/**
 * Ensure the `wa_sessions` row a `SessionRef` names actually exists, with
 * `id = user_id` — that equality is this gateway's whole addressing scheme
 * (see `sessions.ts`'s "Session identity" note): `wa_session_keys` FK's to
 * `wa_sessions(tenant_id, id)`, so a key write cannot happen before the row
 * does.
 *
 * Plan §6 says "the gateway only [writes `wa_sessions`], always via the
 * FastAPI internal callback" — but that callback is asynchronous and this
 * FK is not: a key write that raced it would fail. So this function inserts
 * only the **skeleton** (id, tenant_id, user_id) with `ON CONFLICT DO
 * NOTHING`, never touching `status` or any other column the callback owns.
 * `status` keeps the migration's own `server_default('pairing')` on first
 * insert, and every subsequent write to it goes exclusively through
 * `POST /api/wa/internal/status` — so the division of ownership plan §6
 * describes still holds for everything that carries meaning.
 *
 * This is also what makes `scripts/test-db.sh` work with no backend running
 * at all: the gateway is fully self-sufficient against Postgres, exactly as
 * P2's tests already assume.
 */
export async function ensureWaSessionRow(pool: Pool, ref: SessionRef): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', ref.tenantId]);
    await client.query(
      `INSERT INTO wa_sessions (id, tenant_id, user_id)
       VALUES ($1, $2, $1)
       ON CONFLICT (user_id) DO NOTHING`,
      [ref.sessionId, ref.tenantId],
    );
    await client.query('COMMIT');
  } catch (error) {
    await client.query('ROLLBACK').catch(() => undefined);
    throw error;
  } finally {
    client.release();
  }
}

/**
 * `node-postgres` does not parse `sslmode` out of the connection string the
 * way `libpq`/asyncpg does — a pool built from the bare URL silently tries a
 * plaintext connection and Koyeb's Postgres rejects it. The Python side
 * (`Settings.asyncpg_connect_args`) faces the identical DSN and resolves it
 * the same way for the same reason: Koyeb's certificate is not in any trust
 * store reachable from this process, so `sslmode=require` has to mean
 * "encrypt, don't verify" rather than fail outright or silently downgrade to
 * plaintext.
 *
 * A DSN with no `sslmode` (the local test Postgres, `scripts/test-db.sh`)
 * gets `ssl: false` — that database speaks no TLS at all, and asking for it
 * would just fail to connect.
 */
export function sslConfigFor(databaseUrl: string): false | { rejectUnauthorized: false } {
  let sslmode: string | null;
  try {
    sslmode = new URL(databaseUrl).searchParams.get('sslmode');
  } catch {
    // An unparseable URL is a config problem the pool itself will report
    // clearly when it tries to connect; nothing here should also throw.
    return false;
  }
  if (!sslmode || sslmode === 'disable') return false;
  // Every non-disable libpq mode this DSN actually uses (`require`, in
  // practice) maps to encrypt-without-verify, matching
  // `Settings.asyncpg_connect_args` on the Python side. `verify-ca` /
  // `verify-full` are not in use anywhere in this deployment, so there is no
  // trust-store plumbing to add for them.
  return { rejectUnauthorized: false };
}

export function createPool(databaseUrl: string): Pool {
  return new Pool({ connectionString: databaseUrl, ssl: sslConfigFor(databaseUrl) });
}

/**
 * Refuse to serve a single request against a role that can bypass RLS.
 *
 * `PostgresAuthStore` and `SessionsRepo` below both scope every statement
 * with `set_config('app.tenant_id', …)` and lean entirely on the
 * `tenant_isolation` policy the P2 migration forced onto `wa_sessions` and
 * `wa_session_keys` to keep agency A's WhatsApp keys away from agency B
 * (§18). Against a role with `rolbypassrls`, that policy is decorative —
 * every `set_config` call would still "work", every query would still
 * return rows, and nothing in this file would ever notice that the isolation
 * it believes it is providing is not happening. The production URL points at
 * a role the migration created specifically without the attribute, but that
 * is a fact about a Koyeb secret nobody in this repo can see, so the process
 * checks it directly rather than assuming it.
 */
export async function assertRlsNotBypassed(pool: Pool): Promise<void> {
  const { rows } = await pool.query<{ rolbypassrls: boolean }>(
    'SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user',
  );
  if (rows[0]?.rolbypassrls) {
    throw new RlsBypassError(
      'current_user can bypass row-level security; refusing to start the WA gateway ' +
        '— every tenant_isolation policy this service depends on would be decorative (§18).',
    );
  }
}
