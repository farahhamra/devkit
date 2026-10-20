# Devkit 0.15.0 — browser diagnostics

- Added `browser console --session <name>` for bounded console text and uncaught JavaScript errors; optional `--level` and sequence-based `--since`.
- Added `browser network --session <name>` for latest request states, HTTP statuses, safe transport failures and nullable browser timings; optional `--failed` and `--since`. No HTTP headers/bodies/cookies, URL queries/fragments, or WebSocket/SSE payload capture.
- Added `browser status --session <name>` for responding session/browser health, current page, observed authentication/organization state, uptime and idle timeout. Headed mode explicitly does not prove user-visible desktop placement.
- Diagnostic buffers are memory-only, bounded to 200 console records / 200 requests, persist across navigation, and report retention gaps and observation errors independently of output truncation. Reads do not clear capture or require a logged-in/ready page.
- Refused worker connections and timeouts now have distinct actionable errors. Documented manually opening sessions from a normal Windows terminal for isolated IDE runners; no desktop or supervisor bypass.
- Updated command help, bundled agent templates, package smoke checks and synthetic-browser regression coverage. No new dependency, application-source edits or Node collector behavior changes.

Close old sessions before upgrading, reinstall `.[query,browser]` from inside the Devkit checkout, run `devkit agents sync` from the ElectroERP root, then open a fresh session. Diagnostics are not retroactive. See the [browser guide](../guides/BROWSER.md) for output contracts, redaction limits and safe cursor usage.
