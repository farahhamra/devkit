# Devkit 0.14.0 — isolated ERP browser sessions

- Optional `browser` extra adds Playwright-backed `browser open`, `navigate`, `inspect`, `screenshot`, and `close` commands. Visible installed Google Chrome is the default; headless, Playwright Chromium and an explicit trusted executable are options.
- Named project-local sessions preserve the page/login between separate commands through a private capability-authenticated loopback worker, with serialized operations and idle expiry.
- ERP-only manual login, environment-name token input and explicitly saved exact-origin token state. No raw-token arguments, invented organization, Hub authentication or application logout on close.
- Structured main-frame semantic inspection with snapshot-scoped references; viewport/full-page/element PNGs; explicit DOM readiness, timeout/authentication gates and truncation. No network-idle dependency, automated click/type commands or arbitrary JavaScript command.
- Private local Git-ignored/source-excluded artifacts, origin restrictions, credential-safe output, Windows account-only ACLs, stale-reference checks and non-overwriting captures.
- Bundled agent guidance describes capabilities and limits without requiring agents to use the new commands. Browser navigation runs application JavaScript and is not covered by static analysis's read-only guarantee.

See [installation, usage and limitations](../guides/BROWSER.md). Validation uses synthetic local pages; real ERP behavior still needs separately authorized validation against the running app.
