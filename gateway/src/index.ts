import { buildApp } from './app.js';
import { makeStatusPusher } from './callback.js';
import { ConfigError, loadConfig } from './config.js';
import { assertRlsNotBypassed, createPool, RlsBypassError } from './db.js';
import { SessionManager } from './sessions.js';
import { ValueCipher } from './crypto.js';

async function main(): Promise<void> {
  let config;
  try {
    config = loadConfig();
  } catch (error) {
    if (error instanceof ConfigError) {
      // Fail loudly at boot. A gateway that starts without its secret is worse
      // than one that does not start.
      console.error(`gateway: ${error.message}`);
      process.exit(1);
    }
    throw error;
  }

  // This is the first pool this service opens, so both P3-mandatory checks
  // (see docs/superpowers/specs/2026-07-29-baileys-gateway-plan.md, "P3
  // carries two requirements") happen right here, before anything else runs.
  const pool = createPool(config.databaseUrl); // explicit `ssl` — see db.ts
  try {
    await assertRlsNotBypassed(pool);
  } catch (error) {
    if (error instanceof RlsBypassError) {
      console.error(`gateway: ${error.message}`);
      await pool.end().catch(() => undefined);
      process.exit(1);
    }
    throw error;
  }

  const cipher = new ValueCipher({
    key: config.encryptionKey,
    ...(config.previousEncryptionKey ? { previousKey: config.previousEncryptionKey } : {}),
  });

  const sessions = new SessionManager(pool, cipher, {
    onStatusChange: makeStatusPusher(config.apiCallbackUrl, config.sharedSecret, {
      warn: (obj, msg) => console.warn(msg, obj),
    }),
    sendMinIntervalSeconds: config.sendMinIntervalSeconds,
    pairQrWaitMs: config.pairQrWaitMs,
  });

  const app = buildApp(config, sessions);

  // Koyeb rolling deploys send SIGTERM; closing cleanly matters more once
  // sockets are live (plan §2), so the handler is here from the start.
  for (const signal of ['SIGTERM', 'SIGINT'] as const) {
    process.on(signal, () => {
      app.close().then(
        () => pool.end(),
        () => pool.end(),
      ).finally(() => process.exit(0));
    });
  }

  await app.listen({ host: config.host, port: config.port });
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
