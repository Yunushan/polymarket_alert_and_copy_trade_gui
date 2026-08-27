# Security Policy

## Supported releases

Security fixes are made on the latest release line and `main`. Upgrade to the
latest published release before reporting an issue unless doing so would expose
funds or credentials.

## Reporting a vulnerability

Do not create a public issue for a suspected vulnerability, leaked credential,
or unsafe funded-trading behavior. Use the repository's GitHub private security
advisory flow: `Security` -> `Report a vulnerability`.

Include the affected version/commit, reproduction steps, impact, and whether
real credentials or funds were involved. Redact private keys, API secrets,
signed headers, and wallet recovery material. Acknowledgement and remediation
targets are documented in `docs/PRODUCTION_OPERATIONS.md`.

## Security boundaries

- The web API is loopback-only by default. A remote bind needs an explicit
  acknowledgement and API token; production access should use a TLS reverse
  proxy with authentication.
- Live trading is disabled by default and requires explicit safety gates.
- Credentials must be supplied through a protected environment file or secret
  manager, never through committed configuration files. Configuration load,
  save, HTTP mutation, and CLI mutation reject credential values; only validated
  environment-variable names and external credential-file paths may persist.
- Outbound adapter traffic requires HTTPS/WSS, disables redirects, rejects
  non-public DNS/IP destinations, and applies a response-byte cap. A reviewed
  local gateway requires an exact origin in
  `MARKET_SENTINEL_OUTBOUND_PRIVATE_ORIGINS` before process startup. Endpoint
  and custom-host policy settings cannot be changed through the HTTP API.
