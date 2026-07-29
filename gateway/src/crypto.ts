/**
 * AES-256-GCM around every stored auth-state value (plan §3).
 *
 * Baileys' signal keys ARE the WhatsApp identity. A dump of `wa_session_keys`
 * in the clear is every recruiter's session, so the rows are ciphertext and the
 * key lives only in the gateway's own environment — FastAPI can read the table
 * and still cannot read the values, which is the point of putting the key on
 * one service only.
 *
 * ## Stored layout of `value_encrypted` (bytea), in order:
 *
 * ```
 *   byte  0        format/key version (currently 1)
 *   bytes 1..12    IV / nonce, 12 random bytes (GCM's native size)
 *   bytes 13..28   GCM authentication tag, 16 bytes
 *   bytes 29..     ciphertext, same length as the plaintext
 * ```
 *
 * IV and tag are stored inline rather than in their own columns so that a value
 * is one self-describing blob: there is no way to write the ciphertext and lose
 * the tag, and no second column to forget in a migration.
 *
 * ## What the AAD binds
 *
 * AAD = utf8(`${sessionId}:${category}:${keyId}`) — the row's full primary key.
 * GCM authenticates the AAD without storing it, so a ciphertext copied into a
 * different row (another recruiter's session, or another key id in the same
 * session) fails the tag check and throws, instead of decrypting cleanly as
 * someone else's key material. That is the §18 tenant boundary expressed in the
 * cryptography rather than only in an RLS policy.
 */

import { createCipheriv, createDecipheriv, randomBytes, timingSafeEqual } from 'node:crypto';

const ALGORITHM = 'aes-256-gcm';
const KEY_BYTES = 32;
const IV_BYTES = 12;
const TAG_BYTES = 16;
const VERSION = 1;

const VERSION_OFFSET = 0;
const IV_OFFSET = 1;
const TAG_OFFSET = IV_OFFSET + IV_BYTES;
const CIPHERTEXT_OFFSET = TAG_OFFSET + TAG_BYTES;

export class CryptoError extends Error {}

/** The row identity an encrypted value is bound to. */
export interface ValueIdentity {
  readonly sessionId: string;
  readonly category: string;
  readonly keyId: string;
}

function aad(identity: ValueIdentity): Buffer {
  return Buffer.from(
    `${identity.sessionId}:${identity.category}:${identity.keyId}`,
    'utf8',
  );
}

/**
 * Decode a base64 32-byte key. Exported because config validates the key at
 * boot: a bad key must stop the process, not surface as a decrypt failure on
 * the first recruiter to reconnect.
 */
export function decodeKey(base64: string, name: string): Buffer {
  let key: Buffer;
  try {
    key = Buffer.from(base64, 'base64');
  } catch {
    throw new CryptoError(`${name} is not valid base64`);
  }
  if (key.length !== KEY_BYTES) {
    throw new CryptoError(
      `${name} must decode to ${KEY_BYTES} bytes (AES-256); got ${key.length}`,
    );
  }
  return key;
}

export interface ValueCipherOptions {
  /** Current key, used for every write and tried first on read. */
  readonly key: Buffer;
  /**
   * Optional retired key (`WA_GATEWAY_ENCRYPTION_KEY_PREVIOUS`, plan §3). Rows
   * written under it still decrypt; the next write re-encrypts under the
   * current key, so rotation drains lazily with no migration and no downtime.
   */
  readonly previousKey?: Buffer;
}

export class ValueCipher {
  readonly #keys: readonly Buffer[];

  constructor(options: ValueCipherOptions) {
    if (options.key.length !== KEY_BYTES) {
      throw new CryptoError(`encryption key must be ${KEY_BYTES} bytes`);
    }
    if (options.previousKey && options.previousKey.length !== KEY_BYTES) {
      throw new CryptoError(`previous encryption key must be ${KEY_BYTES} bytes`);
    }
    // Order matters: the current key is tried first so the common path is one
    // decrypt, and a rotated-away key never silently keeps being the winner.
    const keys = [options.key];
    if (options.previousKey && !equalKeys(options.key, options.previousKey)) {
      keys.push(options.previousKey);
    }
    this.#keys = keys;
  }

  encrypt(plaintext: Buffer, identity: ValueIdentity): Buffer {
    const iv = randomBytes(IV_BYTES);
    const cipher = createCipheriv(ALGORITHM, this.#keys[0]!, iv);
    cipher.setAAD(aad(identity));
    const body = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    return Buffer.concat([Buffer.from([VERSION]), iv, cipher.getAuthTag(), body]);
  }

  decrypt(stored: Buffer, identity: ValueIdentity): Buffer {
    if (stored.length < CIPHERTEXT_OFFSET) {
      throw new CryptoError('stored value is too short to be a valid ciphertext');
    }
    const version = stored[VERSION_OFFSET];
    if (version !== VERSION) {
      throw new CryptoError(`unknown stored value version: ${version}`);
    }
    const iv = stored.subarray(IV_OFFSET, TAG_OFFSET);
    const tag = stored.subarray(TAG_OFFSET, CIPHERTEXT_OFFSET);
    const body = stored.subarray(CIPHERTEXT_OFFSET);

    for (const key of this.#keys) {
      const decipher = createDecipheriv(ALGORITHM, key, iv);
      decipher.setAAD(aad(identity));
      decipher.setAuthTag(tag);
      try {
        return Buffer.concat([decipher.update(body), decipher.final()]);
      } catch {
        // Wrong key, wrong row, or tampered bytes — indistinguishable by
        // design, and all of them mean "do not return these bytes".
      }
    }
    // Deliberately unspecific, and deliberately an exception rather than null:
    // a caller that treats an undecryptable key as "absent" would hand Baileys
    // a half-empty key store and trigger the silent logout of plan §11.
    throw new CryptoError('value failed authenticated decryption');
  }
}

function equalKeys(a: Buffer, b: Buffer): boolean {
  return a.length === b.length && timingSafeEqual(a, b);
}
