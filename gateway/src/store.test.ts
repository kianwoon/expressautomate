/**
 * The auth-state store against a real Postgres.
 *
 * Plan §11 calls auth-state persistence corruption the riskiest thing in the
 * build: one lost key write and the restored session decrypts nothing,
 * WhatsApp logs the device out, every recruiter re-pairs, and repeated
 * re-pairing is itself a ban signal. So these tests are the phase.
 *
 * They need a real database — `ON CONFLICT`, the `bytea` round trip, and RLS
 * are Postgres behaviours, and a hand-rolled double would agree with whatever
 * bug the store has. `scripts/test-db.sh` provisions one and sets
 * `WA_GATEWAY_TEST_DATABASE_URL`; without it these skip loudly and the crypto
 * tests still run.
 */

import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { after, before, describe, test } from 'node:test';

import { proto } from 'baileys';
import type { AuthenticationCreds, SignalDataTypeMap } from 'baileys';
import { Pool } from 'pg';

import { CREDS_CATEGORY, CREDS_KEY_ID, usePostgresAuthState } from './auth.js';
import { CryptoError, ValueCipher } from './crypto.js';
import { PostgresAuthStore, type SessionRef } from './store.js';

const DSN = process.env.WA_GATEWAY_TEST_DATABASE_URL ?? '';

// Skipping is right on a laptop with no container running. It is not right in
// CI, where a silent skip means the suite that guards the riskiest code in
// this service — losing an auth-state write logs every recruiter out — reports
// green having run nothing. `node --test` counts a skipped suite as a pass and
// prints "skipped 0" for suites declared this way, so nobody would notice.
//
// CI sets this, and then an absent DSN is a failure rather than a shrug.
if (process.env.WA_GATEWAY_REQUIRE_DB_TESTS === '1' && DSN === '') {
  throw new Error(
    'WA_GATEWAY_REQUIRE_DB_TESTS=1 but WA_GATEWAY_TEST_DATABASE_URL is unset: ' +
      'the store suite would have skipped silently.',
  );
}

const SKIP = DSN === '' ? 'set WA_GATEWAY_TEST_DATABASE_URL (see scripts/test-db.sh)' : false;

/**
 * Every category Baileys can ask the key store for, with a value of the right
 * shape.
 *
 * Sourced from Baileys' own `SignalDataTypeMap` in
 * `node_modules/baileys/lib/Types/Auth.d.ts`, not from a list someone
 * remembered. The `Record<keyof SignalDataTypeMap, …>` annotation is what makes
 * that true and keeps it true: if a Baileys upgrade adds or renames a category,
 * `npm run typecheck` fails here instead of a recruiter being logged out.
 */
const SAMPLES: { [T in keyof SignalDataTypeMap]: SignalDataTypeMap[T] } = {
  'pre-key': { private: randomBytes(32), public: randomBytes(32) },
  session: randomBytes(88),
  'sender-key': randomBytes(48),
  'sender-key-memory': { '6591234567@s.whatsapp.net': true, '6598765432@s.whatsapp.net': false },
  'app-state-sync-key': {
    keyData: randomBytes(32),
    fingerprint: { rawId: 7, currentIndex: 2, deviceIndexes: [0, 1] },
    timestamp: 1769000000000,
  },
  'app-state-sync-version': { version: 3, hash: randomBytes(128), indexValueMap: {} },
  'lid-mapping': '6591234567@lid',
  'device-list': ['0', '1', '42'],
  tctoken: { token: randomBytes(24), timestamp: '1769000000', senderTimestamp: 1769000001 },
  'identity-key': randomBytes(33),
};

const CATEGORIES = Object.keys(SAMPLES) as (keyof SignalDataTypeMap)[];

describe('PostgresAuthStore', { skip: SKIP }, () => {
  let pool: Pool;
  let store: PostgresAuthStore;
  const cipher = new ValueCipher({ key: randomBytes(32) });
  const created: SessionRef[] = [];

  /** A tenant, a user and a `wa_sessions` row, all inside the tenant's scope. */
  async function seedSession(): Promise<SessionRef> {
    const tenantId = randomUUID();
    const sessionId = randomUUID();
    const userId = randomUUID();
    const client = await pool.connect();
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
      await client.query(
        'INSERT INTO wa_sessions (id, tenant_id, user_id) VALUES ($1, $2, $3)',
        [sessionId, tenantId, userId],
      );
      await client.query('COMMIT');
    } finally {
      client.release();
    }
    const ref = { tenantId, sessionId };
    created.push(ref);
    return ref;
  }

  before(() => {
    pool = new Pool({ connectionString: DSN });
    store = new PostgresAuthStore(pool, cipher);
  });

  after(async () => {
    for (const ref of created) {
      const client = await pool.connect();
      try {
        await client.query('BEGIN');
        await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', ref.tenantId]);
        await client.query('DELETE FROM wa_session_keys');
        await client.query('DELETE FROM wa_sessions');
        await client.query('DELETE FROM users');
        await client.query('DELETE FROM tenants');
        await client.query('COMMIT');
      } finally {
        client.release();
      }
    }
    await pool.end();
  });

  test('every Baileys key category round-trips byte-identically', async () => {
    const ref = await seedSession();
    await store.write(
      ref,
      CATEGORIES.map((category) => ({ category, keyId: 'id-1', value: SAMPLES[category] })),
    );

    for (const category of CATEGORIES) {
      const back = (await store.read(ref, category, ['id-1'])).get('id-1');
      assert.deepEqual(
        JSON.parse(JSON.stringify(back, bufferToHex)),
        JSON.parse(JSON.stringify(SAMPLES[category], bufferToHex)),
        `${category} did not round-trip`,
      );
    }
  });

  test('Buffers come back as Buffers, not base64 strings or {type:"Buffer"}', async () => {
    const ref = await seedSession();
    const original = SAMPLES['pre-key'];
    await store.write(ref, [{ category: 'pre-key', keyId: 'b', value: original }]);

    const back = (await store.read(ref, 'pre-key', ['b'])).get('b') as {
      private: unknown;
      public: unknown;
    };
    // The classic silent failure: a store that "works" until libsignal is handed
    // a string where it expected bytes.
    assert.ok(Buffer.isBuffer(back.private), 'private key must be a Buffer');
    assert.ok(Buffer.isBuffer(back.public), 'public key must be a Buffer');
    assert.equal(Buffer.compare(back.private as Buffer, original.private), 0);
    assert.equal(Buffer.compare(back.public as Buffer, original.public), 0);

    // …and a bare Uint8Array category too, not only the nested-object one.
    await store.write(ref, [{ category: 'session', keyId: 'b', value: SAMPLES.session }]);
    const session = (await store.read(ref, 'session', ['b'])).get('b');
    assert.ok(Buffer.isBuffer(session));
    assert.equal(Buffer.compare(session as Buffer, Buffer.from(SAMPLES.session)), 0);
  });

  test('writing key B does not disturb key A', async () => {
    const ref = await seedSession();
    const a = { private: randomBytes(32), public: randomBytes(32) };
    await store.write(ref, [{ category: 'pre-key', keyId: 'A', value: a }]);

    // Several later writes, including one that overwrites A's *category* under
    // a different id — the shape of a Baileys `set` that a blob store loses.
    for (let i = 0; i < 5; i += 1) {
      await store.write(ref, [
        { category: 'pre-key', keyId: `B${i}`, value: { private: randomBytes(32), public: randomBytes(32) } },
        { category: 'session', keyId: 'A', value: randomBytes(64) },
      ]);
    }

    const back = (await store.read(ref, 'pre-key', ['A'])).get('A') as { private: Buffer };
    assert.equal(Buffer.compare(back.private, a.private), 0, 'key A was lost');

    const all = await store.read(ref, 'pre-key', ['A', 'B0', 'B1', 'B2', 'B3', 'B4']);
    assert.equal(all.size, 6);
  });

  test('re-writing one key updates in place rather than duplicating', async () => {
    const ref = await seedSession();
    await store.write(ref, [{ category: 'sender-key', keyId: 'k', value: Buffer.from('first') }]);
    await store.write(ref, [{ category: 'sender-key', keyId: 'k', value: Buffer.from('second') }]);

    const back = (await store.read(ref, 'sender-key', ['k'])).get('k') as Buffer;
    assert.equal(back.toString(), 'second');

    // Scoped, because the tables force RLS: an unscoped query sees nothing at
    // all, which would make this assertion pass for the wrong reason.
    const count = await scoped<{ n: number }>(
      pool,
      ref,
      'SELECT count(*)::int AS n FROM wa_session_keys WHERE session_id = $1 AND category = $2',
      [ref.sessionId, 'sender-key'],
    );
    assert.equal(count[0]!.n, 1);
  });

  test('a null value deletes the row — how a consumed pre-key retires', async () => {
    const ref = await seedSession();
    await store.write(ref, [{ category: 'pre-key', keyId: 'used', value: SAMPLES['pre-key'] }]);
    await store.write(ref, [{ category: 'pre-key', keyId: 'used', value: null }]);
    assert.equal((await store.read(ref, 'pre-key', ['used'])).size, 0);
  });

  test('ids with no row are absent, not null-valued', async () => {
    const ref = await seedSession();
    const found = await store.read(ref, 'pre-key', ['never-written']);
    assert.equal(found.size, 0);
    assert.equal(await store.readOne(ref, CREDS_CATEGORY, CREDS_KEY_ID), undefined);
  });

  test('a ciphertext moved to another session fails to decrypt', async () => {
    const victim = await seedSession();
    const attacker = await seedSession();

    await store.write(victim, [{ category: 'session', keyId: 'x', value: randomBytes(64) }]);
    const stolen = await scoped<{ value_encrypted: Buffer }>(
      pool,
      victim,
      'SELECT value_encrypted FROM wa_session_keys WHERE session_id = $1',
      [victim.sessionId],
    );
    const stolenBytes = stolen[0]!.value_encrypted;

    // Splice the bytes into the attacker's own row, which they are entitled to
    // write. The AAD binds the row identity, so reading it back throws instead
    // of handing them the victim's key material.
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', attacker.tenantId]);
      await client.query(
        `INSERT INTO wa_session_keys (tenant_id, session_id, category, key_id, value_encrypted)
         VALUES ($1, $2, 'session', 'x', $3)`,
        [attacker.tenantId, attacker.sessionId, stolenBytes],
      );
      await client.query('COMMIT');
    } finally {
      client.release();
    }

    await assert.rejects(() => store.read(attacker, 'session', ['x']), CryptoError);
  });

  test('a different encryption key fails cleanly rather than returning garbage', async () => {
    const ref = await seedSession();
    await store.write(ref, [{ category: 'session', keyId: 'k', value: randomBytes(32) }]);

    const wrong = new PostgresAuthStore(pool, new ValueCipher({ key: randomBytes(32) }));
    await assert.rejects(() => wrong.read(ref, 'session', ['k']), CryptoError);
  });

  test('RLS: agency B cannot read agency A rows through the store', async () => {
    const a = await seedSession();
    const b = await seedSession();
    await store.write(a, [{ category: 'session', keyId: 'secret', value: randomBytes(32) }]);

    // B's tenant scope, A's session id: the policy filters the row out entirely,
    // so this is empty rather than an undecryptable value.
    const found = await store.read(
      { tenantId: b.tenantId, sessionId: a.sessionId },
      'session',
      ['secret'],
    );
    assert.equal(found.size, 0);
  });

  test('clear removes a session s keys and nobody else s', async () => {
    const a = await seedSession();
    const b = await seedSession();
    await store.write(a, [{ category: 'session', keyId: 'k', value: randomBytes(16) }]);
    await store.write(b, [{ category: 'session', keyId: 'k', value: randomBytes(16) }]);

    await store.clear(a);
    assert.equal((await store.read(a, 'session', ['k'])).size, 0);
    assert.equal((await store.read(b, 'session', ['k'])).size, 1);
  });
});

describe('usePostgresAuthState', { skip: SKIP }, () => {
  let pool: Pool;
  let store: PostgresAuthStore;
  const cipher = new ValueCipher({ key: randomBytes(32) });
  const created: SessionRef[] = [];

  async function seedSession(): Promise<SessionRef> {
    const tenantId = randomUUID();
    const sessionId = randomUUID();
    const userId = randomUUID();
    const client = await pool.connect();
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
      await client.query('INSERT INTO wa_sessions (id, tenant_id, user_id) VALUES ($1, $2, $3)', [
        sessionId,
        tenantId,
        userId,
      ]);
      await client.query('COMMIT');
    } finally {
      client.release();
    }
    const ref = { tenantId, sessionId };
    created.push(ref);
    return ref;
  }

  before(() => {
    pool = new Pool({ connectionString: DSN });
    store = new PostgresAuthStore(pool, cipher);
  });

  after(async () => {
    for (const ref of created) {
      const client = await pool.connect();
      try {
        await client.query('BEGIN');
        await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', ref.tenantId]);
        await client.query('DELETE FROM wa_session_keys');
        await client.query('DELETE FROM wa_sessions');
        await client.query('DELETE FROM users');
        await client.query('DELETE FROM tenants');
        await client.query('COMMIT');
      } finally {
        client.release();
      }
    }
    await pool.end();
  });

  test('a fresh session gets fresh creds and persists them on saveCreds', async () => {
    const ref = await seedSession();
    const { state, saveCreds } = await usePostgresAuthState(store, ref);

    assert.ok(Buffer.isBuffer(state.creds.noiseKey.private));
    assert.equal(state.creds.registered, false);
    await saveCreds();

    const stored = (await store.readOne(ref, CREDS_CATEGORY, CREDS_KEY_ID)) as AuthenticationCreds;
    assert.equal(Buffer.compare(stored.noiseKey.private, state.creds.noiseKey.private), 0);
    assert.equal(Buffer.compare(stored.signedIdentityKey.public, state.creds.signedIdentityKey.public), 0);
  });

  test('a restart restores the same creds — the no-QR property of plan §2', async () => {
    const ref = await seedSession();
    const first = await usePostgresAuthState(store, ref);
    first.state.creds.registered = true;
    first.state.creds.me = { id: '6591234567:1@s.whatsapp.net', name: 'A Recruiter' };
    await first.saveCreds();

    // A whole new process would do exactly this and nothing else.
    const second = await usePostgresAuthState(store, ref);
    assert.equal(second.state.creds.registered, true);
    assert.equal(second.state.creds.me?.id, '6591234567:1@s.whatsapp.net');
    assert.equal(
      Buffer.compare(second.state.creds.noiseKey.private, first.state.creds.noiseKey.private),
      0,
      'restored noise key must be byte-identical or the session cannot resume',
    );
    assert.ok(Buffer.isBuffer(second.state.creds.signedPreKey.keyPair.private));
  });

  test('keys.set writes every category and keys.get reads them back', async () => {
    const ref = await seedSession();
    const { state } = await usePostgresAuthState(store, ref);

    // One `set` carrying every category at once, the way Baileys batches them.
    const batch: Record<string, Record<string, unknown>> = {};
    for (const category of CATEGORIES) {
      batch[category] = { one: SAMPLES[category] };
    }
    await state.keys.set(batch as never);

    for (const category of CATEGORIES) {
      const got = await state.keys.get(category, ['one']);
      assert.ok(got.one !== undefined, `${category} was not stored`);
    }

    const preKey = (await state.keys.get('pre-key', ['one'])).one!;
    assert.equal(Buffer.compare(preKey.private as Buffer, SAMPLES['pre-key'].private), 0);
  });

  test('app-state-sync-key comes back as a protobuf, not a plain object', async () => {
    const ref = await seedSession();
    const { state } = await usePostgresAuthState(store, ref);
    await state.keys.set({ 'app-state-sync-key': { k: SAMPLES['app-state-sync-key'] } });

    const got = (await state.keys.get('app-state-sync-key', ['k'])).k;
    // libsignal is handed this object; a JSON literal fails much later, during
    // app-state sync, a long way from the store.
    assert.ok(got instanceof proto.Message.AppStateSyncKeyData);
    assert.equal(
      Buffer.compare(Buffer.from(got.keyData!), Buffer.from(SAMPLES['app-state-sync-key'].keyData!)),
      0,
    );
  });

  test('keys.set with a null value removes the key', async () => {
    const ref = await seedSession();
    const { state } = await usePostgresAuthState(store, ref);
    await state.keys.set({ 'pre-key': { gone: SAMPLES['pre-key'] } });
    await state.keys.set({ 'pre-key': { gone: null } });
    assert.deepEqual(await state.keys.get('pre-key', ['gone']), {});
  });
});

/**
 * Run a raw query inside a tenant's RLS scope.
 *
 * Every direct `pool.query` against these tables must go through this. An
 * unscoped read returns zero rows rather than erroring — fail-closed, and the
 * reason two assertions here first "passed" by seeing nothing at all.
 */
async function scoped<R extends Record<string, unknown>>(
  pool: Pool,
  ref: SessionRef,
  sql: string,
  params: unknown[],
): Promise<R[]> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', ref.tenantId]);
    const result = await client.query<R>(sql, params);
    await client.query('COMMIT');
    return result.rows;
  } finally {
    client.release();
  }
}

/** Compare Buffers by content in a deepEqual that would otherwise ignore them. */
function bufferToHex(_key: string, value: unknown): unknown {
  if (Buffer.isBuffer(value)) return { __hex: value.toString('hex') };
  if (value instanceof Uint8Array) return { __hex: Buffer.from(value).toString('hex') };
  return value;
}
