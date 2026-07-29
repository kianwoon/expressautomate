# WA gateway

Per-recruiter outbound WhatsApp via Baileys. Design:
[docs/superpowers/specs/2026-07-29-baileys-gateway-plan.md](../docs/superpowers/specs/2026-07-29-baileys-gateway-plan.md).

Not to be confused with the Meta Cloud API **WhatsApp notifications** in
`backend/` (`WHATSAPP_*`). Nothing here uses that prefix.

## Status: P1 — skeleton only

What exists: an HTTP server, an unauthenticated `GET /health`, a shared-secret
guard on everything else, and one stub route (`GET /status`) that exists so the
guard is exercised. **Baileys is not a dependency yet and no session, socket or
database code exists.** P2 adds the schema and crypto; P3 adds pairing.

## Develop

```bash
npm ci
npm test          # typecheck + node:test
npm run build     # tsc -> dist/ (tests excluded)
WA_GATEWAY_SHARED_SECRET=dev-secret npm start
```

## Release gate (from P2 onward, plan §11)

The riskiest property is auth-state persistence: miss one `creds.update` and a
restored session decrypts nothing, WhatsApp silently logs the device out, and
every recruiter re-pairs — which is itself a ban signal. Before releasing any
change under `gateway/**` once sessions exist, run the manual integration test
by hand: pair a real test number, kill the process mid-conversation, restart,
and assert (a) no QR is requested and (b) a message still sends.

## Deploy

Own image (`ghcr.io/kianwoon/expressautomate-gateway`), own Koyeb service,
**no public route, scale pinned to 1**. See the deploy table in the root
`CLAUDE.md` — those settings live nowhere in this repo.
