/**
 * Every setting comes from the environment. Nothing is hardcoded — no literal
 * secrets, URLs or ports in source (root CLAUDE.md, "All config comes from the
 * repo-root .env"; here, from the Koyeb service env, which is set by hand).
 *
 * The plan (§1) names :7300 as the port, so that is the *default* rather than a
 * constant: Koyeb can move it without a code change, and the health check the
 * service is created with must match whatever is set.
 */

import { CryptoError, decodeKey } from './crypto.js';

export interface GatewayConfig {
  readonly host: string;
  readonly port: number;
  /** Shared secret FastAPI presents as `Authorization: Bearer …` (plan §4). */
  readonly sharedSecret: string;
  /** Postgres DSN for the gateway's own restricted role (plan §1). */
  readonly databaseUrl: string;
  /** 32 bytes, decoded from base64 `WA_GATEWAY_ENCRYPTION_KEY` (plan §3). */
  readonly encryptionKey: Buffer;
  /** Retired key that still decrypts un-rewritten rows; absent normally. */
  readonly previousEncryptionKey?: Buffer | undefined;
}

export class ConfigError extends Error {}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): GatewayConfig {
  const sharedSecret = env.WA_GATEWAY_SHARED_SECRET ?? '';
  if (sharedSecret === '') {
    // Refuse to boot rather than start an unauthenticated gateway. An empty
    // secret would otherwise make every caller's empty header "match", which is
    // exactly the silent-empty-string failure mode CLAUDE.md records for
    // GRAPH_BASE_URL and R2_*.
    throw new ConfigError('WA_GATEWAY_SHARED_SECRET is unset; refusing to start');
  }

  const rawPort = env.WA_GATEWAY_PORT ?? '7300';
  const port = Number.parseInt(rawPort, 10);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new ConfigError(`WA_GATEWAY_PORT is not a valid port: ${rawPort}`);
  }

  const databaseUrl = env.WA_GATEWAY_DATABASE_URL ?? '';
  if (databaseUrl === '') {
    // Same reasoning as the shared secret: a gateway with no database keeps no
    // auth state, so every recruiter re-pairs on every deploy — and repeated
    // re-pairing is itself a ban signal (plan §11). Fail at boot, loudly.
    throw new ConfigError('WA_GATEWAY_DATABASE_URL is unset; refusing to start');
  }

  const rawKey = env.WA_GATEWAY_ENCRYPTION_KEY ?? '';
  if (rawKey === '') {
    // Without the key the gateway can neither read stored state nor write new
    // state. Starting anyway would mean a process that pairs recruiters and
    // then cannot persist them — worse than not starting.
    throw new ConfigError('WA_GATEWAY_ENCRYPTION_KEY is unset; refusing to start');
  }
  const encryptionKey = wrapCryptoError(() =>
    decodeKey(rawKey, 'WA_GATEWAY_ENCRYPTION_KEY'),
  );

  // Optional: present only while a rotation is draining (plan §3).
  const rawPrevious = env.WA_GATEWAY_ENCRYPTION_KEY_PREVIOUS ?? '';
  const previousEncryptionKey =
    rawPrevious === ''
      ? undefined
      : wrapCryptoError(() =>
          decodeKey(rawPrevious, 'WA_GATEWAY_ENCRYPTION_KEY_PREVIOUS'),
        );

  return {
    // 0.0.0.0 so the container is reachable on the Koyeb service mesh; the
    // service itself is deployed with no public route (plan §4).
    host: env.WA_GATEWAY_HOST ?? '0.0.0.0',
    port,
    sharedSecret,
    databaseUrl,
    encryptionKey,
    previousEncryptionKey,
  };
}

/** A malformed key is a config problem, so it must fail like one. */
function wrapCryptoError<T>(fn: () => T): T {
  try {
    return fn();
  } catch (error) {
    if (error instanceof CryptoError) throw new ConfigError(error.message);
    throw error;
  }
}
