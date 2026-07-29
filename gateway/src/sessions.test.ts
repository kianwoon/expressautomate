/**
 * `SessionManager` against a real Postgres (the FK from `wa_session_keys` to
 * `wa_sessions`, and RLS, are both Postgres behaviours — see store.test.ts)
 * and a fake Baileys socket. Never opens a real WhatsApp connection: the
 * fake factory below is the only thing `SessionManager` is ever handed a
 * socket by in this file.
 */

import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { after, before, describe, test } from 'node:test';

import { Pool } from 'pg';

import { ValueCipher } from './crypto.js';
import type { SessionRef } from './store.js';
import { SessionManager, type SocketFactory } from './sessions.js';

const DSN = process.env.WA_GATEWAY_TEST_DATABASE_URL ?? '';
const SKIP = DSN === '' ? 'set WA_GATEWAY_TEST_DATABASE_URL (see scripts/test-db.sh)' : false;

type Handler = (payload: unknown) => void;

/** A Baileys socket with none of Baileys: just enough surface for
 * `SessionManager` to drive, plus a way for the test to fire events on it. */
/**
 * A factory that hands out a **fresh** socket per call and counts them.
 *
 * `fakeSocketFactory` returns one shared instance, which is fine for the tests
 * that only care about events — but it cannot tell one open from two, and it
 * cannot model a reconnect, where each attempt genuinely gets a new socket
 * with its own handlers.
 */
function countingSocketFactory(): {
  factory: SocketFactory;
  opened: () => number;
  emitLatest: (event: string, payload?: unknown) => void;
} {
  let latest: Map<string, Handler[]> | null = null;
  let count = 0;

  const factory = (() => {
    count += 1;
    const handlers = new Map<string, Handler[]>();
    latest = handlers;
    return {
      user: { id: '6591234567:1@s.whatsapp.net' },
      ev: {
        on(event: string, cb: Handler) {
          const list = handlers.get(event) ?? [];
          list.push(cb);
          handlers.set(event, list);
        },
      },
      logout: async () => {},
    };
  }) as unknown as SocketFactory;

  return {
    factory,
    opened: () => count,
    emitLatest: (event, payload) => {
      for (const cb of latest?.get(event) ?? []) cb(payload);
    },
  };
}

function fakeSocketFactory(): { factory: SocketFactory; emit: (event: string, payload?: unknown) => void; sockets: unknown[] } {
  const handlers = new Map<string, Handler[]>();
  let loggedOut = false;
  const socket = {
    user: undefined as { id: string } | undefined,
    ev: {
      on(event: string, cb: Handler) {
        const list = handlers.get(event) ?? [];
        list.push(cb);
        handlers.set(event, list);
      },
    },
    logout: async () => {
      loggedOut = true;
    },
    get loggedOut() {
      return loggedOut;
    },
  };
  const sockets = [socket];
  return {
    // Cast: this test double is intentionally not a real WASocket — see file docstring.
    factory: (() => socket) as unknown as SocketFactory,
    emit: (event, payload) => {
      // Baileys itself sets `sock.user` before firing the `open` update, so
      // the fake mirrors that ordering rather than the handler ever seeing
      // `open` with no `user` set yet.
      if (event === 'connection.update' && (payload as { connection?: string })?.connection === 'open') {
        socket.user = { id: '6591234567:1@s.whatsapp.net' };
      }
      for (const cb of handlers.get(event) ?? []) cb(payload);
    },
    sockets,
  };
}

describe('SessionManager', { skip: SKIP }, () => {
  let pool: Pool;
  const cipher = new ValueCipher({ key: randomBytes(32) });
  const created: SessionRef[] = [];

  async function seedTenantAndUser(): Promise<SessionRef> {
    const tenantId = randomUUID();
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
      await client.query('COMMIT');
    } finally {
      client.release();
    }
    const ref: SessionRef = { tenantId, sessionId: userId };
    created.push(ref);
    return ref;
  }

  before(() => {
    pool = new Pool({ connectionString: DSN });
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

  test('an unknown session is honestly disconnected, with no side effects', async () => {
    const ref = await seedTenantAndUser();
    const { factory } = fakeSocketFactory();
    const manager = new SessionManager(pool, cipher, { socketFactory: factory });

    const snapshot = await manager.status(ref);
    assert.equal(snapshot.status, 'disconnected');
    assert.equal(snapshot.qr, null);
  });

  test('pair → QR → open drives pairing through connected, and the QR is never written to the database', async () => {
    const ref = await seedTenantAndUser();
    const pushed: { status: string; qr: string | null }[] = [];
    const fake = fakeSocketFactory();
    const manager = new SessionManager(pool, cipher, {
      socketFactory: fake.factory,
      onStatusChange: (_ref, snapshot) => {
        pushed.push({ status: snapshot.status, qr: snapshot.qr });
      },
    });

    const started = await manager.pair(ref);
    assert.equal(started.status, 'pairing');
    assert.equal(started.qr, null);

    fake.emit('connection.update', { qr: 'raw-qr-string' });
    const withQr = await manager.status(ref);
    assert.equal(withQr.status, 'pairing');
    assert.equal(withQr.qr, 'raw-qr-string');
    assert.ok(withQr.expiresAt);

    fake.emit('connection.update', { connection: 'open' });
    const connected = await manager.status(ref);
    assert.equal(connected.status, 'connected');
    assert.equal(connected.qr, null);
    assert.equal(connected.phoneNumber, '6591234567');

    assert.ok(pushed.some((p) => p.qr === 'raw-qr-string'), 'the QR change was pushed for the SSE nudge');

    // The database is checked directly (bypassing the manager's own
    // read path) because a bug that put the QR in `wa_session_keys` would
    // still round-trip through `status()` and this test would pass anyway.
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', ref.tenantId]);
      const { rows } = await client.query(
        "SELECT value_encrypted FROM wa_session_keys WHERE session_id = $1",
        [ref.sessionId],
      );
      await client.query('COMMIT');
      for (const row of rows) {
        assert.ok(
          !row.value_encrypted.includes(Buffer.from('raw-qr-string')),
          'the QR string leaked into an encrypted key row',
        );
      }
    } finally {
      client.release();
    }
  });

  test('a restored session (creds already stored, process forgot it) asks for no QR', async () => {
    const ref = await seedTenantAndUser();
    const first = fakeSocketFactory();
    const manager1 = new SessionManager(pool, cipher, { socketFactory: first.factory });
    await manager1.pair(ref);
    first.emit('creds.update'); // Baileys fires this as pairing completes; persists via saveCreds
    // The handler fires `saveCreds()` without the caller being able to await
    // it (Baileys' own `ev.on` is synchronous) — give the write a moment to
    // land before asserting a second manager can see it.
    await new Promise((resolve) => setTimeout(resolve, 50));
    first.emit('connection.update', { connection: 'open' });

    // A brand-new manager stands in for the process having restarted: no
    // in-memory runtime knows about `ref` any more, only the store does.
    const second = fakeSocketFactory();
    const manager2 = new SessionManager(pool, cipher, { socketFactory: second.factory });
    const restored = await manager2.status(ref);
    assert.equal(restored.qr, null, 'a restored session must never demand a fresh QR');
  });

  test('disconnect logs the socket out, clears stored keys, and reports disconnected — not logged_out', async () => {
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    const manager = new SessionManager(pool, cipher, { socketFactory: fake.factory });
    await manager.pair(ref);
    fake.emit('connection.update', { connection: 'open' });

    const result = await manager.disconnect(ref);
    assert.equal(result.status, 'disconnected');
    assert.equal((fake.sockets[0] as { loggedOut: boolean }).loggedOut, true);

    const again = await manager.status(ref);
    assert.equal(again.status, 'disconnected');
  });

  test('two callers racing to open a session get one socket, not two', async () => {
    // `#open` reaches the runtime map only after two awaits. Two tabs pressing
    // Connect inside that window both used to build a socket over the same
    // credentials, and WhatsApp answers a second socket on one identity with a
    // stream conflict that closes both — straight into the reconnect path.
    const ref = await seedTenantAndUser();
    const counting = countingSocketFactory();
    const manager = new SessionManager(pool, cipher, { socketFactory: counting.factory });

    // Both callers must be ones that actually open. `status()` short-circuits
    // to `disconnected` for a session with no stored creds without touching
    // the factory, so racing it against `pair()` would assert nothing.
    const [a, b] = await Promise.all([manager.pair(ref), manager.pair(ref)]);

    assert.equal(counting.opened(), 1, 'a raced open must not create a second socket');
    assert.equal(a.status, b.status);
    assert.equal(a.status, 'pairing');
  });

  test('a session that will not come back stops retrying, and waits longer each time', async () => {
    // Each retry used to build a runtime with the attempt count reset, so the
    // ceiling was unreachable and a dead session reconnected at whatever speed
    // the machine allowed — the repeated-reconnect pattern the plan names as a
    // ban signal.
    const ref = await seedTenantAndUser();
    const counting = countingSocketFactory();
    const waits: number[] = [];
    const manager = new SessionManager(pool, cipher, {
      socketFactory: counting.factory,
      sleep: async (ms) => {
        waits.push(ms);
      },
    });

    await manager.pair(ref);
    // Drop the connection repeatedly. Every close lands on whichever socket is
    // current, exactly as a genuinely unreachable number would behave.
    for (let i = 0; i < 6; i += 1) {
      counting.emitLatest('connection.update', { connection: 'close' });
      await new Promise((resolve) => setImmediate(resolve));
    }

    const final = await manager.status(ref);
    assert.equal(final.status, 'disconnected', 'retries must be given up on, not repeated forever');
    assert.ok(waits.length >= 2, `expected several backoff waits, saw ${waits.length}`);
    const [first, second] = waits as [number, number];
    assert.ok(second > first, `each wait must exceed the last: ${first} then ${second}`);
  });

  test('asking about a session mid-backoff reports reconnecting and opens nothing', async () => {
    // The regression that made the first fix worthless. Pushing `reconnecting`
    // nudges the browser, the browser refetches, and if that refetch found no
    // runtime it reopened immediately with the attempt count back at zero —
    // so watching the panel drove the very loop the backoff prevents.
    const ref = await seedTenantAndUser();
    const counting = countingSocketFactory();
    const gate: { release: () => void } = { release: () => {} };
    const manager = new SessionManager(pool, cipher, {
      socketFactory: counting.factory,
      // Hold the retry inside the sleep so the assertions below run in exactly
      // the window the bug lived in.
      sleep: () => new Promise<void>((resolve) => { gate.release = resolve; }),
    });

    await manager.pair(ref);
    counting.emitLatest('connection.update', { connection: 'close' });
    await new Promise((resolve) => setImmediate(resolve));

    const during = await manager.status(ref);
    assert.equal(during.status, 'reconnecting', 'a session waiting to retry is reconnecting');
    assert.equal(counting.opened(), 1, 'asking must not start a second socket mid-backoff');

    gate.release();
  });

  test('disconnecting mid-backoff stays disconnected — the sleeping retry gives up', async () => {
    const ref = await seedTenantAndUser();
    const counting = countingSocketFactory();
    const gate: { release: () => void } = { release: () => {} };
    const manager = new SessionManager(pool, cipher, {
      socketFactory: counting.factory,
      sleep: () => new Promise<void>((resolve) => { gate.release = resolve; }),
    });

    await manager.pair(ref);
    counting.emitLatest('connection.update', { connection: 'close' });
    await new Promise((resolve) => setImmediate(resolve));

    await manager.disconnect(ref);
    // Let the retry wake up now that the recruiter has already disconnected.
    gate.release();
    await new Promise((resolve) => setImmediate(resolve));

    const after = await manager.status(ref);
    assert.equal(after.status, 'disconnected', 'a retry must not resurrect a disconnected session');
    assert.equal(after.qr, null);
  });
});
