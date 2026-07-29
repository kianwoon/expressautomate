import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { describe, test } from 'node:test';

import { ConfigError, loadConfig } from './config.js';
import { CryptoError, ValueCipher, decodeKey } from './crypto.js';

// Inert test keys, generated here rather than written down, so nothing in this
// file could ever be mistaken for a real one.
const KEY_A = randomBytes(32);
const KEY_B = randomBytes(32);

const IDENTITY = {
  sessionId: '11111111-1111-1111-1111-111111111111',
  category: 'pre-key',
  keyId: '42',
};

describe('ValueCipher', () => {
  const cipher = new ValueCipher({ key: KEY_A });

  test('round-trips bytes exactly', () => {
    const plaintext = randomBytes(200);
    const back = cipher.decrypt(cipher.encrypt(plaintext, IDENTITY), IDENTITY);
    assert.equal(Buffer.compare(back, plaintext), 0);
  });

  test('an empty plaintext round-trips too', () => {
    const back = cipher.decrypt(cipher.encrypt(Buffer.alloc(0), IDENTITY), IDENTITY);
    assert.equal(back.length, 0);
  });

  test('the stored layout is version ‖ iv(12) ‖ tag(16) ‖ ciphertext', () => {
    const plaintext = randomBytes(37);
    const stored = cipher.encrypt(plaintext, IDENTITY);
    assert.equal(stored[0], 1, 'first byte is the version');
    assert.equal(stored.length, 1 + 12 + 16 + plaintext.length);
  });

  test('the nonce differs every time, so equal plaintexts are not equal rows', () => {
    const plaintext = Buffer.from('same');
    const a = cipher.encrypt(plaintext, IDENTITY);
    const b = cipher.encrypt(plaintext, IDENTITY);
    assert.notEqual(a.toString('hex'), b.toString('hex'));
  });

  test('a ciphertext moved to another row fails to decrypt', () => {
    const stored = cipher.encrypt(randomBytes(64), IDENTITY);

    // Another recruiter's session — the §18 splice this AAD exists to stop.
    assert.throws(
      () => cipher.decrypt(stored, { ...IDENTITY, sessionId: '22222222-2222-2222-2222-222222222222' }),
      CryptoError,
    );
    // Same session, different category.
    assert.throws(() => cipher.decrypt(stored, { ...IDENTITY, category: 'session' }), CryptoError);
    // Same session and category, different key id.
    assert.throws(() => cipher.decrypt(stored, { ...IDENTITY, keyId: '43' }), CryptoError);
  });

  test('a wrong key fails cleanly rather than returning garbage', () => {
    const stored = cipher.encrypt(randomBytes(64), IDENTITY);
    const wrong = new ValueCipher({ key: KEY_B });
    assert.throws(() => wrong.decrypt(stored, IDENTITY), CryptoError);
  });

  test('tampered bytes fail the tag check', () => {
    const stored = cipher.encrypt(randomBytes(64), IDENTITY);
    stored[stored.length - 1] = (stored[stored.length - 1] ?? 0) ^ 0xff;
    assert.throws(() => cipher.decrypt(stored, IDENTITY), CryptoError);
  });

  test('truncated or unknown-version values are rejected, not parsed', () => {
    assert.throws(() => cipher.decrypt(Buffer.alloc(4), IDENTITY), CryptoError);
    const stored = cipher.encrypt(randomBytes(8), IDENTITY);
    stored[0] = 9;
    assert.throws(() => cipher.decrypt(stored, IDENTITY), CryptoError);
  });

  test('a rotated-away key still decrypts its old rows', () => {
    const old = new ValueCipher({ key: KEY_B });
    const stored = old.encrypt(Buffer.from('written before rotation'), IDENTITY);

    const rotated = new ValueCipher({ key: KEY_A, previousKey: KEY_B });
    assert.equal(rotated.decrypt(stored, IDENTITY).toString(), 'written before rotation');

    // …and new writes use the new key, so the old one alone can no longer read.
    const fresh = rotated.encrypt(Buffer.from('after'), IDENTITY);
    assert.throws(() => old.decrypt(fresh, IDENTITY), CryptoError);
  });

  test('a key of the wrong size is refused at construction', () => {
    assert.throws(() => new ValueCipher({ key: randomBytes(16) }), CryptoError);
    assert.throws(
      () => new ValueCipher({ key: KEY_A, previousKey: randomBytes(31) }),
      CryptoError,
    );
  });
});

describe('decodeKey', () => {
  test('accepts exactly 32 base64 bytes', () => {
    assert.equal(decodeKey(KEY_A.toString('base64'), 'K').length, 32);
  });

  test('rejects a key of the wrong length', () => {
    assert.throws(() => decodeKey(randomBytes(16).toString('base64'), 'K'), CryptoError);
    assert.throws(() => decodeKey('', 'K'), CryptoError);
  });
});

describe('config refuses to start without the crypto settings', () => {
  const base = {
    WA_GATEWAY_SHARED_SECRET: 'x',
    WA_GATEWAY_DATABASE_URL: 'postgresql://u:p@localhost:5432/db',
    WA_GATEWAY_ENCRYPTION_KEY: KEY_A.toString('base64'),
  };

  test('a missing encryption key is fatal', () => {
    const { WA_GATEWAY_ENCRYPTION_KEY: _omitted, ...withoutKey } = base;
    assert.throws(() => loadConfig(withoutKey), ConfigError);
    assert.throws(() => loadConfig({ ...base, WA_GATEWAY_ENCRYPTION_KEY: '' }), ConfigError);
  });

  test('a malformed encryption key is fatal, not a runtime surprise', () => {
    assert.throws(
      () => loadConfig({ ...base, WA_GATEWAY_ENCRYPTION_KEY: 'too-short' }),
      ConfigError,
    );
  });

  test('a missing database URL is fatal', () => {
    const { WA_GATEWAY_DATABASE_URL: _omitted, ...withoutDb } = base;
    assert.throws(() => loadConfig(withoutDb), ConfigError);
  });

  test('a complete environment loads, and the previous key is optional', () => {
    const config = loadConfig(base);
    assert.equal(config.encryptionKey.length, 32);
    assert.equal(config.previousEncryptionKey, undefined);

    const rotating = loadConfig({
      ...base,
      WA_GATEWAY_ENCRYPTION_KEY_PREVIOUS: KEY_B.toString('base64'),
    });
    assert.equal(rotating.previousEncryptionKey?.length, 32);
    assert.throws(
      () => loadConfig({ ...base, WA_GATEWAY_ENCRYPTION_KEY_PREVIOUS: 'nope' }),
      ConfigError,
    );
  });
});
