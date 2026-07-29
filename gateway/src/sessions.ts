/**
 * `SessionManager` — one Baileys socket per recruiter (plan §2, §5, §6).
 *
 * ## Status vocabulary
 *
 * Exactly the five values the P2 migration CHECK-constrains `wa_sessions.status`
 * to (`backend/alembic/versions/20260729_2100_wa_gateway_sessions.py`) and
 * `backend/app/models/wa_session.py` names identically:
 *
 *   pairing → connected → reconnecting → disconnected | logged_out
 *
 * `pairing` covers both "socket starting" and "QR outstanding" — whether a QR
 * is attached (`current.qr !== null`) is what tells them apart, so there is no
 * separate `qr_required` value to keep in sync with the database's CHECK.
 * `logged_out` (WhatsApp forced the link closed — re-pairing needed) is kept
 * distinct from `disconnected` (the recruiter asked to stop): collapsing them
 * would either strand a `logged_out` recruiter behind a "still connected"
 * status or send a merely-`disconnected` one through a needless re-pair, and
 * repeated re-pairing is itself a ban signal (plan §11).
 *
 * ## Who restores what, and why boot-time restore is per-session, not global
 *
 * Plan §2 asks for "reconnect of all known sessions at boot". `wa_sessions`
 * and `wa_session_keys` are FORCE ROW LEVEL SECURITY, scoped by
 * `app.tenant_id` (see `db.ts`'s `assertRlsNotBypassed` — this service is
 * required to run as a role that cannot see past that policy). With no
 * tenant set, the policy admits **zero rows**, including from `tenants`
 * itself (`20260726_1800_row_level_security.py`), so there is no query this
 * process can run at boot to discover "every tenant with a session" without
 * a bypass role — which the P3 requirement explicitly forbids opening.
 *
 * So restore here is **lazy and per-session**: `ensure()` is the single path
 * both `pair()` and `status()` go through, and it loads that one session's
 * stored `creds` (via `usePostgresAuthState`, itself scoped to the caller's
 * `SessionRef`) before ever asking Baileys for a QR. A session that already
 * has creds resumes silently; only a session with none gets a QR. Restarting
 * this process therefore reconnects a session the moment its recruiter's
 * browser next calls `/session` (pair, or a status refetch) — not at the
 * instant the process comes up — which still satisfies "no QR after a
 * redeploy" (plan §2's user-visible contract) without a cross-tenant query
 * this schema cannot support. P5's kill-and-restart test should call
 * `GET /sessions/status` (or `/pair`) for the known session, not just start
 * the process and wait.
 */

import { Boom } from '@hapi/boom';
import makeWASocket, { DisconnectReason } from 'baileys';
import type { WASocket } from 'baileys';
import type { Pool } from 'pg';

import { usePostgresAuthState } from './auth.js';
import { ensureWaSessionRow } from './db.js';
import { PostgresAuthStore, type SessionRef } from './store.js';

/**
 * ## Session identity
 *
 * `SessionRef.sessionId` here is always the recruiter's `user_id` — not a
 * separate `wa_sessions.id`. `wa_sessions` has `id` as its surrogate key and
 * a unique constraint on `user_id` because one recruiter can only ever have
 * one session, so setting `id = user_id` (see `db.ts#ensureWaSessionRow`)
 * collapses two identifiers this gateway would otherwise have to keep in
 * sync into one it can compute from a request with no database round trip.
 */

export type SessionStatus = 'pairing' | 'connected' | 'reconnecting' | 'disconnected' | 'logged_out';

export interface SessionSnapshot {
  readonly status: SessionStatus;
  /** In-memory only — never written to Postgres (plan §5). */
  readonly qr: string | null;
  readonly expiresAt: string | null;
  readonly phoneNumber: string | null;
  readonly connectedAt: string | null;
}

/** Pushed to FastAPI on every change; matches `InternalStatusIn` in `wa_gateway.py`. */
export interface StatusCallback {
  (ref: SessionRef, snapshot: SessionSnapshot, statusDetail: string | null): void | Promise<void>;
}

/** QR strings rotate roughly every 20s (plan §5) before Baileys gives up. */
const QR_TTL_MS = 20_000;
/** After this many consecutive close-and-retry cycles, stop and report `disconnected`
 *  rather than retrying forever against a dead network or a banned number. */
const MAX_RECONNECT_ATTEMPTS = 3;

interface Runtime {
  socket: WASocket | null;
  status: SessionStatus;
  statusDetail: string | null;
  qr: string | null;
  qrExpiresAt: Date | null;
  phoneNumber: string | null;
  connectedAt: Date | null;
  reconnectAttempts: number;
  /** Baileys fires `connection.update` more than once per logical change;
   *  this dedupes the callback so the push (and its SSE nudge) don't spam. */
  lastPushed: string;
}

function keyFor(ref: SessionRef): string {
  return `${ref.tenantId}:${ref.sessionId}`;
}

function snapshotOf(r: Runtime): SessionSnapshot {
  const qrLive = r.qr !== null && r.qrExpiresAt !== null && r.qrExpiresAt.getTime() > Date.now();
  return {
    status: r.status,
    qr: qrLive ? r.qr : null,
    expiresAt: qrLive ? (r.qrExpiresAt as Date).toISOString() : null,
    phoneNumber: r.phoneNumber,
    connectedAt: r.connectedAt ? r.connectedAt.toISOString() : null,
  };
}

/** Seam for tests: never opens a real WhatsApp connection unless given the real Baileys factory. */
export type SocketFactory = (authState: Awaited<ReturnType<typeof usePostgresAuthState>>['state']) => WASocket;

export function realSocketFactory(authState: Awaited<ReturnType<typeof usePostgresAuthState>>['state']): WASocket {
  return makeWASocket({
    auth: authState,
    // The gateway is headless; nothing here renders a terminal QR.
    printQRInTerminal: false,
  });
}

export class SessionManager {
  readonly #pool: Pool;
  readonly #store: PostgresAuthStore;
  readonly #socketFactory: SocketFactory;
  readonly #onStatusChange: StatusCallback | undefined;
  readonly #runtimes = new Map<string, Runtime>();

  constructor(
    pool: Pool,
    cipher: ConstructorParameters<typeof PostgresAuthStore>[1],
    opts: { socketFactory?: SocketFactory; onStatusChange?: StatusCallback } = {},
  ) {
    this.#pool = pool;
    this.#store = new PostgresAuthStore(pool, cipher);
    this.#socketFactory = opts.socketFactory ?? realSocketFactory;
    this.#onStatusChange = opts.onStatusChange;
  }

  /** `GET /sessions/status` — never opens a socket for a session that isn't
   *  already known and has no stored creds; a session nobody has paired yet
   *  is honestly `disconnected`, not a side effect of being asked about. */
  async status(ref: SessionRef): Promise<SessionSnapshot> {
    const existing = this.#runtimes.get(keyFor(ref));
    if (existing) return snapshotOf(existing);

    const hasCreds = await this.#hasStoredCreds(ref);
    if (!hasCreds) {
      return { status: 'disconnected', qr: null, expiresAt: null, phoneNumber: null, connectedAt: null };
    }
    // Known to the store but not to this process — a fresh boot. Resume it,
    // which is the property described in this file's docstring.
    const runtime = await this.#open(ref);
    return snapshotOf(runtime);
  }

  /** `POST /sessions/pair` — idempotent: connected/pairing sessions are
   *  returned as-is rather than restarted, so two tabs get the same QR and a
   *  live socket never gets torn down by a second click. */
  async pair(ref: SessionRef): Promise<SessionSnapshot> {
    const existing = this.#runtimes.get(keyFor(ref));
    if (existing && (existing.status === 'connected' || existing.status === 'pairing' || existing.status === 'reconnecting')) {
      return snapshotOf(existing);
    }
    const runtime = await this.#open(ref);
    return snapshotOf(runtime);
  }

  /** `POST /sessions/disconnect` — the recruiter's own choice; distinct from
   *  a WhatsApp-forced `logged_out` (see file docstring). */
  async disconnect(ref: SessionRef): Promise<SessionSnapshot> {
    const key = keyFor(ref);
    const runtime = this.#runtimes.get(key);
    if (runtime?.socket) {
      try {
        await runtime.socket.logout('disconnected by recruiter');
      } catch {
        // Baileys throws if the socket is already closed; a logout that
        // "fails" because there was nothing to log out of still ends this
        // session locally, which is what matters here.
      }
    }
    await this.#store.clear(ref);
    this.#runtimes.delete(key);
    const snapshot: SessionSnapshot = {
      status: 'disconnected',
      qr: null,
      expiresAt: null,
      phoneNumber: null,
      connectedAt: null,
    };
    await this.#push(ref, snapshot, 'disconnected by recruiter');
    return snapshot;
  }

  async #hasStoredCreds(ref: SessionRef): Promise<boolean> {
    const { CREDS_CATEGORY, CREDS_KEY_ID } = await import('./auth.js');
    const stored = await this.#store.readOne(ref, CREDS_CATEGORY, CREDS_KEY_ID);
    // `initAuthCreds()` shape always has `.noiseKey`; a truly-unpaired session
    // has no row at all, so presence is the right test — not `.registered`,
    // which Baileys itself only sets after a *completed* pairing and would
    // therefore also be false mid-pairing, which is not what this asks.
    return stored !== undefined;
  }

  async #open(ref: SessionRef): Promise<Runtime> {
    const key = keyFor(ref);
    // Must precede the auth-state read/write below: `wa_session_keys` FK's
    // to `wa_sessions(tenant_id, id)`, so the row has to exist first. See
    // `ensureWaSessionRow`'s docstring for why this is the gateway's job and
    // does not conflict with plan §6's "status is written only via the
    // callback" — this never touches `status`.
    await ensureWaSessionRow(this.#pool, ref);
    const { state, saveCreds } = await usePostgresAuthState(this.#store, ref);
    const runtime: Runtime = {
      socket: null,
      status: 'pairing',
      statusDetail: null,
      qr: null,
      qrExpiresAt: null,
      phoneNumber: null,
      connectedAt: null,
      reconnectAttempts: 0,
      lastPushed: '',
    };
    this.#runtimes.set(key, runtime);

    const socket = this.#socketFactory(state);
    runtime.socket = socket;

    socket.ev.on('creds.update', () => {
      // Plan §11: the riskiest failure in this build is missing one of these.
      void saveCreds();
    });

    socket.ev.on('connection.update', (update) => {
      void this.#handleUpdate(ref, runtime, update);
    });

    return runtime;
  }

  async #handleUpdate(
    ref: SessionRef,
    runtime: Runtime,
    update: { connection?: string; qr?: string; lastDisconnect?: { error?: unknown } },
  ): Promise<void> {
    if (update.qr) {
      runtime.status = 'pairing';
      runtime.qr = update.qr;
      runtime.qrExpiresAt = new Date(Date.now() + QR_TTL_MS);
      runtime.statusDetail = null;
      await this.#push(ref, snapshotOf(runtime), null);
      return;
    }

    if (update.connection === 'open') {
      runtime.status = 'connected';
      runtime.qr = null;
      runtime.qrExpiresAt = null;
      runtime.statusDetail = null;
      runtime.reconnectAttempts = 0;
      runtime.connectedAt = new Date();
      runtime.phoneNumber = runtime.socket?.user?.id ? jidToE164(runtime.socket.user.id) : runtime.phoneNumber;
      await this.#push(ref, snapshotOf(runtime), null);
      return;
    }

    if (update.connection === 'close') {
      const statusCode = (update.lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;

      if (loggedOut) {
        runtime.status = 'logged_out';
        runtime.statusDetail = 'WhatsApp ended the link — re-pairing is required.';
        runtime.qr = null;
        runtime.qrExpiresAt = null;
        await this.#store.clear(ref);
        this.#runtimes.delete(keyFor(ref));
        await this.#push(ref, snapshotOf(runtime), runtime.statusDetail);
        return;
      }

      runtime.reconnectAttempts += 1;
      if (runtime.reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
        runtime.status = 'disconnected';
        runtime.statusDetail = 'Reconnect attempts exhausted.';
        this.#runtimes.delete(keyFor(ref));
        await this.#push(ref, snapshotOf(runtime), runtime.statusDetail);
        return;
      }

      runtime.status = 'reconnecting';
      runtime.statusDetail = 'Connection dropped; retrying.';
      await this.#push(ref, snapshotOf(runtime), runtime.statusDetail);
      // Baileys does not auto-reconnect; a fresh socket over the same
      // (now-updated) auth state is what resumes without a new QR.
      await this.#open(ref);
    }
  }

  async #push(ref: SessionRef, snapshot: SessionSnapshot, statusDetail: string | null): Promise<void> {
    if (!this.#onStatusChange) return;
    const key = keyFor(ref);
    const runtime = this.#runtimes.get(key);
    // Dedup on status+phone+connectedAt, deliberately excluding the QR: a QR
    // rotation is exactly the kind of change plan §5 wants pushed (it drives
    // the SSE nudge → refetch cycle), so it must not be swallowed here.
    const fingerprint = `${snapshot.status}:${snapshot.qr}:${snapshot.phoneNumber}:${snapshot.connectedAt}`;
    if (runtime && runtime.lastPushed === fingerprint) return;
    if (runtime) runtime.lastPushed = fingerprint;
    try {
      await this.#onStatusChange(ref, snapshot, statusDetail);
    } catch {
      // A failed push must not take down the socket it is reporting on —
      // the next state change (or a `GET /sessions/status` poll) will retry
      // implicitly. Matches `events_service.publish`'s own "never break the
      // thing it's a convenience for" rule on the Python side.
    }
  }
}

/** `"6591234567:12@s.whatsapp.net"` → `"6591234567"`. Baileys' own jid can
 *  carry a device suffix after `:`; the phone number is only the user part. */
function jidToE164(jid: string): string {
  return jid.split('@')[0]?.split(':')[0] ?? jid;
}
