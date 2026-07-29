/**
 * Every setting comes from the environment. Nothing is hardcoded — no literal
 * secrets, URLs or ports in source (root CLAUDE.md, "All config comes from the
 * repo-root .env"; here, from the Koyeb service env, which is set by hand).
 *
 * The plan (§1) names :7300 as the port, so that is the *default* rather than a
 * constant: Koyeb can move it without a code change, and the health check the
 * service is created with must match whatever is set.
 */

export interface GatewayConfig {
  readonly host: string;
  readonly port: number;
  /** Shared secret FastAPI presents as `Authorization: Bearer …` (plan §4). */
  readonly sharedSecret: string;
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

  return {
    // 0.0.0.0 so the container is reachable on the Koyeb service mesh; the
    // service itself is deployed with no public route (plan §4).
    host: env.WA_GATEWAY_HOST ?? '0.0.0.0',
    port,
    sharedSecret,
  };
}
