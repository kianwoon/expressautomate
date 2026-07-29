import { buildApp } from './app.js';
import { ConfigError, loadConfig } from './config.js';

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

  const app = buildApp(config);

  // Koyeb rolling deploys send SIGTERM; closing cleanly matters more once
  // sockets are live (plan §2), so the handler is here from the start.
  for (const signal of ['SIGTERM', 'SIGINT'] as const) {
    process.on(signal, () => {
      app.close().then(
        () => process.exit(0),
        () => process.exit(1),
      );
    });
  }

  await app.listen({ host: config.host, port: config.port });
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
