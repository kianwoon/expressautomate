import { createHash, timingSafeEqual } from 'node:crypto';

import { initAuthCreds, proto } from 'baileys';
import type {
  AuthenticationCreds,
  AuthenticationState,
  SignalDataSet,
  SignalDataTypeMap,
} from 'baileys';
import type { FastifyReply, FastifyRequest } from 'fastify';

import type { PostgresAuthStore, SessionRef, StoredValue } from './store.js';

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

/**
 * Where `creds` lives in a table keyed by `(category, key_id)`. It is one
 * object rather than a keyed collection, so it gets a fixed address instead of
 * a column of its own: one storage path, one encryption path, one set of
 * tests, and nothing that can drift between two of them.
 */
export const CREDS_CATEGORY = 'creds';
export const CREDS_KEY_ID = '';

/**
 * A Postgres-backed, encrypted `AuthenticationState` — the equivalent of
 * Baileys' `useMultiFileAuthState`, which its own docstring says not to use in
 * production.
 *
 * **Nothing here opens a socket.** P3 wires this to `makeWASocket`; P2 only
 * has to be able to persist a state and restore it byte-identically, because
 * plan §11 says a single lost key write silently logs the device out and
 * re-pairing is itself a ban signal.
 */
export async function usePostgresAuthState(
  store: PostgresAuthStore,
  ref: SessionRef,
): Promise<{ state: AuthenticationState; saveCreds: () => Promise<void> }> {
  const stored = (await store.readOne(ref, CREDS_CATEGORY, CREDS_KEY_ID)) as
    | AuthenticationCreds
    | undefined;
  // A session with no stored creds is a session that has never paired. Fresh
  // creds here means P3's socket asks for a QR, which is correct; what must
  // never happen is fresh creds for a session that *did* pair, so a read
  // failure throws out of `store.readOne` rather than degrading to this.
  const creds: AuthenticationCreds = stored ?? initAuthCreds();

  return {
    state: {
      creds,
      keys: {
        get: async <T extends keyof SignalDataTypeMap>(type: T, ids: string[]) => {
          const found = await store.read(ref, type, ids);
          const result: { [id: string]: SignalDataTypeMap[T] } = {};
          for (const [id, value] of found) {
            result[id] = reviveForCategory(type, value);
          }
          return result;
        },
        set: async (data: SignalDataSet) => {
          // Flattened into one batch so the whole `set` commits or none of it
          // does; see PostgresAuthStore.write.
          const batch: StoredValue[] = [];
          for (const category of Object.keys(data) as (keyof SignalDataTypeMap)[]) {
            const byId = data[category];
            if (!byId) continue;
            for (const [keyId, value] of Object.entries(byId)) {
              batch.push({ category, keyId, value });
            }
          }
          await store.write(ref, batch);
        },
        clear: async () => {
          await store.clear(ref);
        },
      },
    },
    /**
     * Baileys calls this on every `creds.update`. Missing one is the plan §11
     * failure, so P3 must bind it to that event and nothing else may write
     * `creds`.
     */
    saveCreds: async () => {
      await store.write(ref, [
        { category: CREDS_CATEGORY, keyId: CREDS_KEY_ID, value: creds },
      ]);
    },
  };
}

/**
 * `app-state-sync-key` is the one category Baileys does not accept as plain
 * JSON: libsignal expects a protobuf message instance, and handing it the
 * object literal that came out of JSON.parse fails later, during app-state
 * sync, far from here. `useMultiFileAuthState` does exactly this conversion —
 * it is the reason that function is worth reading rather than guessing at.
 */
function reviveForCategory<T extends keyof SignalDataTypeMap>(
  category: T,
  value: unknown,
): SignalDataTypeMap[T] {
  if (category === 'app-state-sync-key' && value) {
    return proto.Message.AppStateSyncKeyData.fromObject(
      value as Record<string, unknown>,
    ) as unknown as SignalDataTypeMap[T];
  }
  return value as SignalDataTypeMap[T];
}
