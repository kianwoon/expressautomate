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

import { Boom } from '@hapi/boom';
import { Pool } from 'pg';

import { ValueCipher } from './crypto.js';
import type { SessionRef } from './store.js';
import {
  MAX_LOGOUT_ATTEMPTS,
  RECONNECT_PARK_MS,
  SessionManager,
  type SocketFactory,
} from './sessions.js';

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

function fakeSocketFactory(): {
  factory: SocketFactory;
  emit: (event: string, payload?: unknown) => void;
  sockets: unknown[];
  sent: { jid: string; text: string }[];
  /** Resolves once a handler for `event` is attached — immediately if one
   *  already is, otherwise the moment `ev.on(event, ...)` runs. This is what
   *  lets a test emit right after the real handler attaches instead of
   *  racing a wall-clock timer against `SessionManager`'s own DB awaits. */
  subscribed: (event: string) => Promise<void>;
} {
  const handlers = new Map<string, Handler[]>();
  const sent: { jid: string; text: string }[] = [];
  const subscribers = new Map<string, (() => void)[]>();
  let loggedOut = false;
  const socket = {
    user: undefined as { id: string } | undefined,
    ev: {
      on(event: string, cb: Handler) {
        const list = handlers.get(event) ?? [];
        list.push(cb);
        handlers.set(event, list);
        for (const resolve of subscribers.get(event) ?? []) resolve();
        subscribers.delete(event);
      },
    },
    logout: async () => {
      loggedOut = true;
    },
    get loggedOut() {
      return loggedOut;
    },
    sendMessage: async (jid: string, content: { text: string }) => {
      sent.push({ jid, text: content.text });
      return { key: { id: `WAMSG-${sent.length}` } };
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
    sent,
    subscribed: (event) =>
      new Promise<void>((resolve) => {
        if ((handlers.get(event) ?? []).length > 0) {
          resolve();
          return;
        }
        const list = subscribers.get(event) ?? [];
        list.push(resolve);
        subscribers.set(event, list);
      }),
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

  test('pair waits for the first QR and returns it, instead of an empty pairing', async () => {
    // The reason this exists: `pair()` used to answer the moment the socket
    // object existed, so the browser got `pairing` with nothing to draw and
    // had to wait for the API's SSE nudge — and when that nudge did not
    // arrive, for the settings panel's 15s fallback poll. A QR that existed
    // after one second took fifteen to appear.
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    const manager = new SessionManager(pool, cipher, {
      socketFactory: fake.factory,
      pairQrWaitMs: 2_000,
    });

    // Start pairing, then wait for the moment `pair()` has actually attached
    // its `connection.update` handler — not a fixed wall-clock delay racing
    // `#openOnce()`'s two database round trips (ensureWaSessionRow, then
    // usePostgresAuthState) — before emitting the QR. A timed emit dropped
    // the event silently whenever those awaits ran long, e.g. on a loaded CI
    // runner, since the fake never buffers or replays events.
    const pairing = manager.pair(ref);
    await fake.subscribed('connection.update');
    // `subscribed()` resolves the instant `ev.on` runs, which is still inside
    // `#open()` — one promise-chain hop earlier than `pair()`'s own
    // continuation past `await this.#openOnce(ref)`. Racing the emit directly
    // off `subscribed()` would let it land ahead of that continuation on
    // microtask ordering alone, which made a broken `pair()` (the wait
    // deleted) pass by accident in the mutation check below. Yielding to a
    // macrotask flushes every pending microtask first — including that
    // continuation, if it exists — so what happens next depends only on
    // whether `pair()` is actually still waiting, not on tick-count luck.
    await new Promise((resolve) => setImmediate(resolve));
    fake.emit('connection.update', { qr: 'qr-in-first-response' });

    const snapshot = await pairing;
    assert.equal(snapshot.status, 'pairing');
    assert.equal(snapshot.qr, 'qr-in-first-response');
  });

  test('pair gives up waiting and answers anyway, rather than hanging on a socket that never speaks', async () => {
    // The timeout is what keeps this safe to add: a WhatsApp that never sends
    // a QR must degrade to exactly the old behaviour, not to a request that
    // never returns. A held connection per click would be a worse failure
    // than the blank panel this replaces.
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    const manager = new SessionManager(pool, cipher, {
      socketFactory: fake.factory,
      pairQrWaitMs: 30,
    });

    const started = Date.now();
    const snapshot = await manager.pair(ref);
    const waited = Date.now() - started;
    assert.equal(snapshot.status, 'pairing');
    assert.equal(snapshot.qr, null);
    // Both bounds, because only the lower one proves a wait happened at all:
    // asserting `qr === null` and "fast" alone passes just as well if the
    // wait is deleted, which is the shape of a test that guards nothing.
    assert.ok(waited >= 25, `expected to wait for the timeout, waited ${waited}ms`);
    assert.ok(waited < 1_000, `must not wait longer than it was told to, waited ${waited}ms`);
  });

  test('pair → QR → open drives pairing through connected, and the QR is never written to the database', async () => {
    const ref = await seedTenantAndUser();
    const pushed: { status: string; qr: string | null }[] = [];
    const fake = fakeSocketFactory();
    const manager = new SessionManager(pool, cipher, {
      socketFactory: fake.factory,
      // Zero, so this test still describes what it always described: `pair()`
      // returning before any QR exists, and the QR arriving by push after.
      // The waiting behaviour has its own test below.
      pairQrWaitMs: 0,
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
    assert.equal(connected.phoneNumber, '+6591234567');

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

  test('a session that will not come back keeps trying, and waits longer each time', async () => {
    // Each retry used to build a runtime with the attempt count reset, so the
    // ceiling was unreachable and a dead session reconnected at whatever speed
    // the machine allowed — the repeated-reconnect pattern the plan names as a
    // ban signal. The fix has two halves: the backoff still grows (quick
    // attempts, then a long park), and the session is *never given up on*
    // while its credentials are stored — the retention amendment means a drop
    // that outlasts the quick retries must not strand the session at
    // `disconnected`, because nothing in the system ever claims a
    // `disconnected` row to bring it back.
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
    for (let i = 0; i < 7; i += 1) {
      counting.emitLatest('connection.update', { connection: 'close' });
      await new Promise((resolve) => setImmediate(resolve));
    }

    const final = await manager.status(ref);
    assert.equal(
      final.status,
      'reconnecting',
      'a session with stored credentials must keep retrying, never be given up on',
    );
    assert.ok(waits.length >= 2, `expected several backoff waits, saw ${waits.length}`);
    const [first, second] = waits as [number, number];
    assert.ok(second > first, `each wait must exceed the last: ${first} then ${second}`);
    // Once past the quick backoff, the cadence settles at the park interval —
    // bounded, so a dead session is not hammering WhatsApp every few seconds.
    assert.equal(waits[waits.length - 1], RECONNECT_PARK_MS, 'parked retries wait the long interval');

    // And the session still comes back when the connection succeeds again —
    // this is the retention property the whole amendment exists for.
    counting.emitLatest('connection.update', { connection: 'open' });
    await new Promise((resolve) => setImmediate(resolve));
    const recovered = await manager.status(ref);
    assert.equal(recovered.status, 'connected', 'a parked session must resume when it reconnects');
  });

  test('a single 401 close does not destroy the session — it reconnects like any drop', async () => {
    // Retention amendment: WhatsApp can answer a reconnect with a
    // loggedOut-coded close for transient server-side reasons. The old code
    // treated *any* 401 as a genuine logout, cleared the stored credentials,
    // and forced a re-pair — and repeated re-pairing is itself a ban signal
    // (plan §11). A single 401 must fall through to the normal reconnect path
    // and leave the session recoverable.
    const ref = await seedTenantAndUser();
    const first = fakeSocketFactory();
    const manager1 = new SessionManager(pool, cipher, { socketFactory: first.factory });
    await manager1.pair(ref);
    first.emit('creds.update'); // persists creds so the store has something to keep
    await new Promise((resolve) => setTimeout(resolve, 50));
    first.emit('connection.update', { connection: 'open' });

    first.emit('connection.update', {
      connection: 'close',
      lastDisconnect: { error: new Boom('Stream Errored (401)', { statusCode: 401 }) },
    });
    await new Promise((resolve) => setImmediate(resolve));

    const during = await manager1.status(ref);
    assert.equal(during.status, 'reconnecting', 'a 401 is not a logout until it keeps happening');

    // Credentials must still be stored: a fresh manager resumes the session
    // with no QR, exactly like after a process restart.
    const second = fakeSocketFactory();
    const manager2 = new SessionManager(pool, cipher, { socketFactory: second.factory });
    const restored = await manager2.status(ref);
    assert.equal(restored.qr, null, 'credentials must not be destroyed by a single 401');
  });

  test('a session that keeps 401ing is finally treated as logged out and cleared', async () => {
    // The other half of the amendment: a genuinely de-registered device 401s
    // on *every* attempt. After MAX_LOGOUT_ATTEMPTS of them in a row the
    // close is a real logout — creds are cleared and the UI is told that
    // re-pairing is required, so the next Connect genuinely shows a QR.
    const ref = await seedTenantAndUser();
    const counting = countingSocketFactory();
    const manager = new SessionManager(pool, cipher, { socketFactory: counting.factory });
    await manager.pair(ref);
    counting.emitLatest('connection.update', { connection: 'open' });
    await new Promise((resolve) => setImmediate(resolve));

    for (let i = 0; i < MAX_LOGOUT_ATTEMPTS + 1; i += 1) {
      counting.emitLatest('connection.update', {
        connection: 'close',
        lastDisconnect: { error: new Boom('Stream Errored (401)', { statusCode: 401 }) },
      });
      await new Promise((resolve) => setImmediate(resolve));
    }

    const final = await manager.status(ref);
    assert.equal(final.status, 'logged_out', 'persistent 401s must end in a genuine logout');
    assert.equal(final.qr, null);

    // The credentials are gone, so nothing can silently resume the session —
    // the next pair genuinely needs a fresh QR.
    const second = fakeSocketFactory();
    const manager2 = new SessionManager(pool, cipher, { socketFactory: second.factory });
    const after = await manager2.status(ref);
    assert.equal(after.status, 'disconnected', 'cleared credentials mean no silent resume');
    assert.equal(after.qr, null);
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
  test('send refuses a session that is not connected, and says which status it is', async () => {
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    const manager = new SessionManager(pool, cipher, { socketFactory: fake.factory });

    // Never paired at all.
    assert.deepEqual(await manager.send(ref, '+6591234567', 'hi'), {
      ok: false,
      status: 'disconnected',
    });

    // Pairing: a QR is outstanding, so there is no socket to accept a message.
    await manager.pair(ref);
    fake.emit('connection.update', { qr: 'raw-qr-string' });
    assert.deepEqual(await manager.send(ref, '+6591234567', 'hi'), {
      ok: false,
      status: 'pairing',
    });

    assert.deepEqual(fake.sent, [], 'nothing may be handed to a socket in these states');
  });

  test('send refuses a reconnecting session rather than waiting on it', async () => {
    const ref = await seedTenantAndUser();
    const counting = countingSocketFactory();
    const manager = new SessionManager(pool, cipher, {
      socketFactory: counting.factory,
      // Hold the retry open so the session stays observably `reconnecting`.
      sleep: () => new Promise<void>(() => {}),
    });
    await manager.pair(ref);
    counting.emitLatest('connection.update', { connection: 'close' });
    await new Promise((resolve) => setImmediate(resolve));

    const outcome = await manager.send(ref, '+6591234567', 'hi');
    assert.deepEqual(outcome, { ok: false, status: 'reconnecting' });
  });

  test('a connected session sends, and returns WhatsApp\'s own message id', async () => {
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    const manager = new SessionManager(pool, cipher, { socketFactory: fake.factory });
    await manager.pair(ref);
    fake.emit('connection.update', { connection: 'open' });

    const outcome = await manager.send(ref, '+65 9123 4567', 'Hi Hui Ling Tan,');
    assert.equal(outcome.ok, true);
    assert.equal(outcome.ok && outcome.providerMessageId, 'WAMSG-1');
    // E.164 in, WhatsApp jid out — the separators and the `+` are not part of
    // a jid's user part, and the digits came from the candidate row.
    assert.deepEqual(fake.sent, [{ jid: '6591234567@s.whatsapp.net', text: 'Hi Hui Ling Tan,' }]);
  });

  test('a second send inside the spacing floor is refused with the real remaining wait', async () => {
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    let now = 1_000_000;
    const manager = new SessionManager(pool, cipher, {
      socketFactory: fake.factory,
      sendMinIntervalSeconds: 30,
      now: () => now,
    });
    await manager.pair(ref);
    fake.emit('connection.update', { connection: 'open' });

    const first = await manager.send(ref, '+6591234567', 'one');
    assert.equal(first.ok, true);

    now += 5_000; // 5s later, well inside the 30s floor
    const second = await manager.send(ref, '+6591234567', 'two');
    assert.equal(second.ok, false);
    assert.ok(!second.ok && 'retryAfterSeconds' in second);
    if (!second.ok && 'retryAfterSeconds' in second) {
      // Between 25s (30-5) and 25s + a quarter of 30s of jitter.
      assert.ok(second.retryAfterSeconds >= 25 && second.retryAfterSeconds <= 33);
    }
    // Refused, so nothing new went out.
    assert.equal(fake.sent.length, 1);
  });

  test('the jitter is drawn once at admission and never re-rolled on a refusal', async () => {
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    let now = 1_000_000;
    const manager = new SessionManager(pool, cipher, {
      socketFactory: fake.factory,
      sendMinIntervalSeconds: 30,
      now: () => now,
    });
    await manager.pair(ref);
    fake.emit('connection.update', { connection: 'open' });

    await manager.send(ref, '+6591234567', 'one');

    now += 5_000;
    const first = await manager.send(ref, '+6591234567', 'two');
    now += 1_000;
    const second = await manager.send(ref, '+6591234567', 'two');
    assert.ok(!first.ok && 'retryAfterSeconds' in first);
    assert.ok(!second.ok && 'retryAfterSeconds' in second);
    if (!first.ok && 'retryAfterSeconds' in first && !second.ok && 'retryAfterSeconds' in second) {
      // The deadline itself (now + retryAfterSeconds) must be identical
      // between the two refusals — only the "now" measured against it moved.
      assert.equal(now - 1_000 + first.retryAfterSeconds * 1000, now + second.retryAfterSeconds * 1000);
    }
  });

  test('a retry at exactly the quoted deadline succeeds', async () => {
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    let now = 1_000_000;
    const manager = new SessionManager(pool, cipher, {
      socketFactory: fake.factory,
      sendMinIntervalSeconds: 30,
      now: () => now,
    });
    await manager.pair(ref);
    fake.emit('connection.update', { connection: 'open' });

    await manager.send(ref, '+6591234567', 'one');
    const refusal = await manager.send(ref, '+6591234567', 'two');
    assert.ok(!refusal.ok && 'retryAfterSeconds' in refusal);
    const retryAfterSeconds = !refusal.ok && 'retryAfterSeconds' in refusal ? refusal.retryAfterSeconds : 0;

    now += retryAfterSeconds * 1000;
    const retried = await manager.send(ref, '+6591234567', 'two');
    assert.equal(retried.ok, true);
  });

  test('a throw from sendMessage becomes a refusal carrying WhatsApp\'s own words', async () => {
    // Not an exception out of `send`, and not a status: the socket was live,
    // so this is a real observed refusal of this one message. It is the only
    // thing the API is allowed to record as `failed`, and the string is
    // passed on unchanged — rewording it would be inventing a reason for
    // someone else's refusal.
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    const manager = new SessionManager(pool, cipher, { socketFactory: fake.factory });
    await manager.pair(ref);
    fake.emit('connection.update', { connection: 'open' });
    (fake.sockets[0] as { sendMessage: () => Promise<never> }).sendMessage = async () => {
      // 403 `forbidden` — an answer, not a silence. Only a code we can point
      // at earns `failed`; see `POSITIVE_REFUSAL_CODES`.
      throw new Boom('not-authorized: blocked by recipient', { statusCode: 403 });
    };

    const outcome = await manager.send(ref, '+6591234567', 'hi');
    assert.equal(outcome.ok, false);
    assert.equal(!outcome.ok && 'refusal' in outcome && outcome.refusal,
      'not-authorized: blocked by recipient');
    assert.equal(outcome.status, 'connected');
  });

  test('an error we cannot classify is unknown, not a failure', async () => {
    // The direction of the default, which is the whole finding. A socket torn
    // down mid-send (500 badSession, 515 restartRequired, 503 unavailable) or
    // a plain network error carries no proof the frame stayed home — and
    // guessing "failed" is what sends a candidate a second copy.
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    // This test sends repeatedly on the same session to exercise every error
    // shape; the spacing floor (plan §9, P5) is a per-send concern unrelated
    // to what is under test here, so it is disabled rather than padding the
    // test with waits.
    const manager = new SessionManager(pool, cipher, {
      socketFactory: fake.factory,
      sendMinIntervalSeconds: 0,
    });
    await manager.pair(ref);
    fake.emit('connection.update', { connection: 'open' });

    const thrown: unknown[] = [
      new Boom('bad session', { statusCode: 500 }),
      new Boom('restart required', { statusCode: 515 }),
      new Boom('service unavailable', { statusCode: 503 }),
      new Error('socket hang up'),
    ];
    for (const error of thrown) {
      (fake.sockets[0] as { sendMessage: () => Promise<never> }).sendMessage = async () => {
        throw error;
      };
      const outcome = await manager.send(ref, '+6591234567', 'hi');
      assert.ok(
        !outcome.ok && 'indeterminate' in outcome,
        `${String(error)} must be unknown, not a refusal`,
      );
    }
  });

  test('a timed-out send is indeterminate, not a refusal — the message may have gone', async () => {
    // The failure this guards against is subtle and expensive. Baileys throws
    // the same way whether the frame left or not, so calling a timeout a
    // refusal records `failed` for a message that may well have arrived. The
    // recruiter is told it failed, sends it again, and the candidate gets it
    // twice — the exact double-message the idempotency key cannot stop,
    // because a resend of an edited draft is a genuinely new request.
    const ref = await seedTenantAndUser();
    const fake = fakeSocketFactory();
    // Repeated same-session sends to exercise every timeout code; the
    // spacing floor (plan §9, P5) is unrelated to what is under test here.
    const manager = new SessionManager(pool, cipher, {
      socketFactory: fake.factory,
      sendMinIntervalSeconds: 0,
    });
    await manager.pair(ref);
    fake.emit('connection.update', { connection: 'open' });

    for (const statusCode of [408, 428, 440]) {
      (fake.sockets[0] as { sendMessage: () => Promise<never> }).sendMessage = async () => {
        throw new Boom('Timed Out', { statusCode });
      };

      const outcome = await manager.send(ref, '+6591234567', 'hi');
      assert.equal(outcome.ok, false);
      assert.ok(
        !outcome.ok && 'indeterminate' in outcome,
        `Boom ${statusCode} must be indeterminate, not a refusal`,
      );
      assert.ok(
        !outcome.ok && !('refusal' in outcome),
        `Boom ${statusCode} must not be recorded as a refusal`,
      );
    }
  });

  test('send wakes a session this process has forgotten, instead of calling it disconnected', async () => {
    // The redeploy case, and the reason `send` goes through `status()`: every
    // socket dies on deploy, and sending is usually the first thing a
    // recruiter does afterwards. A `send` that only looked in this process's
    // memory would report a perfectly good pairing as disconnected.
    const ref = await seedTenantAndUser();
    const first = fakeSocketFactory();
    const manager1 = new SessionManager(pool, cipher, { socketFactory: first.factory });
    await manager1.pair(ref);
    first.emit('creds.update');
    await new Promise((resolve) => setTimeout(resolve, 50));
    first.emit('connection.update', { connection: 'open' });

    // A brand-new manager: the process restarted, only the store remembers.
    const second = fakeSocketFactory();
    const manager2 = new SessionManager(pool, cipher, { socketFactory: second.factory });
    const outcome = await manager2.send(ref, '+6591234567', 'hi');

    // It resumed from stored credentials with no QR. The resumed socket is
    // still `pairing` until Baileys reports `open`, so the honest answer is a
    // refusal naming that — not `disconnected`, which would have sent the
    // recruiter back to Settings to re-pair a session that is fine.
    assert.equal(outcome.ok, false);
    assert.equal(outcome.ok === false && outcome.status, 'pairing');
    assert.equal((await manager2.status(ref)).qr, null, 'no fresh QR was demanded');

    // And once it reports open, the same session sends without re-pairing.
    second.emit('connection.update', { connection: 'open' });
    const after = await manager2.send(ref, '+6591234567', 'hi');
    assert.equal(after.ok, true);
    assert.deepEqual(second.sent, [{ jid: '6591234567@s.whatsapp.net', text: 'hi' }]);
  });
});
