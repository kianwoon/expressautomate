/**
 * The encrypted Postgres auth-state store (plan §3).
 *
 * Every value Baileys hands us is serialised with Baileys' own `BufferJSON`
 * replacer/reviver, encrypted with the row's identity as AAD, and upserted into
 * `wa_session_keys` at `(session_id, category, key_id)`.
 *
 * Two properties this file exists to guarantee, both from plan §11:
 *
 * 1. **A write cannot lose a key it did not name.** The statement is
 *    `INSERT … ON CONFLICT (session_id, category, key_id) DO UPDATE`, one row
 *    per key. There is no read-modify-write of a combined blob anywhere, so
 *    there is no window in which two concurrent writers can drop one another's
 *    keys.
 * 2. **A batch is all-or-nothing.** Baileys' `keys.set` delivers several
 *    categories at once and they are mutually consistent; applying half of one
 *    would leave a session that decrypts some messages and not others. One
 *    transaction per `set`.
 *
 * `BufferJSON` is not an invention here — it is `baileys/Utils/generics`, the
 * exact serialiser `useMultiFileAuthState` uses, so Buffers come back as
 * Buffers rather than as base64 strings or `{type:'Buffer'}` husks. That silent
 * degradation is the classic way this store breaks.
 */

import { BufferJSON } from 'baileys';
import type { Pool, PoolClient } from 'pg';

import { ValueCipher } from './crypto.js';

/** Which recruiter's session a value belongs to. */
export interface SessionRef {
  readonly tenantId: string;
  readonly sessionId: string;
}

export interface StoredValue {
  readonly category: string;
  readonly keyId: string;
  /** Anything `BufferJSON` can round-trip. `null` means "delete this row". */
  readonly value: unknown;
}

export class PostgresAuthStore {
  readonly #pool: Pool;
  readonly #cipher: ValueCipher;

  constructor(pool: Pool, cipher: ValueCipher) {
    this.#pool = pool;
    this.#cipher = cipher;
  }

  /**
   * Read several values of one category in a single round trip — the shape
   * Baileys' `keys.get(type, ids)` asks for. Ids with no row are simply absent
   * from the result, which is how Baileys signals "not held".
   */
  async read(ref: SessionRef, category: string, keyIds: readonly string[]): Promise<Map<string, unknown>> {
    const found = new Map<string, unknown>();
    if (keyIds.length === 0) return found;

    const rows = await this.#inTenantTransaction(ref, async (client) => {
      const result = await client.query<{ key_id: string; value_encrypted: Buffer }>(
        `SELECT key_id, value_encrypted
           FROM wa_session_keys
          WHERE session_id = $1 AND category = $2 AND key_id = ANY($3::text[])`,
        [ref.sessionId, category, [...keyIds]],
      );
      return result.rows;
    });

    for (const row of rows) {
      const plaintext = this.#cipher.decrypt(row.value_encrypted, {
        sessionId: ref.sessionId,
        category,
        keyId: row.key_id,
      });
      found.set(row.key_id, deserialise(plaintext));
    }
    return found;
  }

  /** Convenience for the single-value categories (`creds`). */
  async readOne(ref: SessionRef, category: string, keyId: string): Promise<unknown | undefined> {
    const found = await this.read(ref, category, [keyId]);
    return found.get(keyId);
  }

  /**
   * Apply a batch of writes and deletes atomically. A `null`/`undefined` value
   * deletes the row, which is how Baileys retires a consumed pre-key.
   */
  async write(ref: SessionRef, values: readonly StoredValue[]): Promise<void> {
    if (values.length === 0) return;

    await this.#inTenantTransaction(ref, async (client) => {
      for (const entry of values) {
        if (entry.value === null || entry.value === undefined) {
          await client.query(
            `DELETE FROM wa_session_keys
              WHERE session_id = $1 AND category = $2 AND key_id = $3`,
            [ref.sessionId, entry.category, entry.keyId],
          );
          continue;
        }

        const ciphertext = this.#cipher.encrypt(serialise(entry.value), {
          sessionId: ref.sessionId,
          category: entry.category,
          keyId: entry.keyId,
        });
        await client.query(
          `INSERT INTO wa_session_keys
                 (tenant_id, session_id, category, key_id, value_encrypted)
               VALUES ($1, $2, $3, $4, $5)
          ON CONFLICT (session_id, category, key_id)
          DO UPDATE SET value_encrypted = EXCLUDED.value_encrypted,
                        updated_at = now()`,
          [ref.tenantId, ref.sessionId, entry.category, entry.keyId, ciphertext],
        );
      }
    });
  }

  /** Drop every value for a session — a logout, not a disconnect. */
  async clear(ref: SessionRef): Promise<void> {
    await this.#inTenantTransaction(ref, async (client) => {
      await client.query('DELETE FROM wa_session_keys WHERE session_id = $1', [
        ref.sessionId,
      ]);
    });
  }

  /**
   * Every statement runs inside a transaction that has set `app.tenant_id`,
   * because the tables force row-level security. `SET LOCAL` rather than `SET`:
   * the setting must die with the transaction or a pooled connection carries
   * one agency's scope into the next agency's query — the exact leak
   * `test_setting_does_not_leak_between_sessions` guards on the Python side.
   */
  async #inTenantTransaction<T>(ref: SessionRef, fn: (client: PoolClient) => Promise<T>): Promise<T> {
    const client = await this.#pool.connect();
    try {
      await client.query('BEGIN');
      await client.query('SELECT set_config($1, $2, true)', ['app.tenant_id', ref.tenantId]);
      const result = await fn(client);
      await client.query('COMMIT');
      return result;
    } catch (error) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw error;
    } finally {
      client.release();
    }
  }
}

export function serialise(value: unknown): Buffer {
  return Buffer.from(JSON.stringify(value, BufferJSON.replacer), 'utf8');
}

export function deserialise(plaintext: Buffer): unknown {
  return JSON.parse(plaintext.toString('utf8'), BufferJSON.reviver);
}
