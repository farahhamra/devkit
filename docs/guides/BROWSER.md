# ERP browser addon

Devkit provides separate commands sharing a named, isolated browser session. Version 0.15.0 adds console, network and session-status diagnostics. It uses optional Python Playwright, not the Chrome DevTools screenshot menu. Installed Google Chrome opens in headed mode by default. This is ERP-only browser navigation/inspection/capture and bounded diagnostics, not browser performance profiling or full UI test automation.

## Install and upgrade

Close existing browser sessions before upgrading; reopen afterward to start the new worker and capture diagnostics. Inside your Devkit checkout (`ElectroERP/.agents/devkit` on a nested installation):

```powershell
git pull --ff-only origin main
python -m pip install --upgrade ".[query,browser]"
devkit --version
cd ../..
devkit agents sync
```

Google Chrome must be installed. There is no automatic browser download or personal-profile attachment. Alternatively, explicitly run `python -m playwright install chromium` and use `open --channel chromium`. Linux may need Playwright's documented OS libraries and a desktop/display for visible mode. An explicit trusted Chrome/Chromium binary can be selected with `--executable <path>`; it overrides channel lookup. Compatibility with arbitrary browser versions is not guaranteed.

Browser help and all other commands work without this extra. `doctor` reports optional Playwright package presence without launching/downloading a browser. No Node/npm project dependency or app-source modification is required.

## Command workflow

Start ERP separately, then run from the ElectroERP root or use `--root` on every command. Running from a nested Devkit Git checkout without `--root` can select the wrong project.

```powershell
devkit browser open http://localhost:3000/auth/login --api-origin http://localhost:5000 --session erp
# Log in manually in the visible Chrome window.
devkit browser navigate /core/products --session erp --wait-for main --json
devkit browser inspect --session erp --json
devkit browser status --session erp --json
devkit browser console --session erp --level error --json
devkit browser network --session erp --failed --json
devkit browser screenshot --session erp --full-page --output products.png --json
devkit browser screenshot --session erp --selector "#reports-summary" --json
# Or: --node <reference-returned-by-inspect>
devkit browser close --session erp
```

The selector examples must match the actual page. `main` demonstrates syntax, not a universal data-loaded condition. A specific loaded or empty-state element is stronger evidence. The agent needs its own image-viewing facility to see the returned PNG; saving a screenshot does not automatically put its pixels into the agent's context.

All eight commands require `--session <name>`; there is no implicit default session or interactive shell. Names use 1–48 lowercase letters/digits/hyphens/underscores, starting with a letter; Windows device names are rejected. Sessions are project-local. Reopening a live name errors rather than navigating it silently; use `navigate` instead. `--app hub` and `--app manager` are rejected; omitted `--app` maps to ERP. `--config`, `--no-cache` and source ignore files do not configure browser behavior.

### Manually started sessions and IDE isolation

On Windows, an IDE runner can launch GUI processes on a private desktop (for example, `exebox-…`) and terminate children when its execution ends. `headless: false` does not prove that a window is on your interactive desktop. Start `browser open` from your normal PowerShell terminal in the ElectroERP root, then let the agent reuse `status`, `navigate`, `inspect`, `console`, `network` and `screenshot` with the same root and session name. The IDE must permit access to the private loopback endpoint and project-local session metadata. Devkit does not escape desktop isolation or job restrictions, and a shell keep-alive loop does not make a private desktop visible.

`status` distinguishes responding session health from observed application/login state. It reports `window_visibility: not_verified` in headed mode rather than claiming visibility. A refused endpoint connection reports that no worker is listening; a timeout reports an unknown/busy/blocked connection, not proof of termination. Status does not automatically reopen a browser, repair state or select a tenant.

### Open options

- `--headless`: hide the browser; visible is the default.
- `--width 1440 --height 900`: viewport size (bounds: 320–3840 wide, 240–2160 high).
- `--api-origin http://localhost:5000`: exact separate ERP backend origin. If omitted, only the frontend origin is assumed, suitable for same-origin API rewrites. Devkit does not guess API origins from source/env configuration.
- Repeat `--allow-origin https://assets.example.com` for explicitly approved asset origins. Do not add unnecessary origins. Full navigations/iframes remain restricted to the frontend; authenticated requests are restricted to frontend/API origins. WebSockets are restricted to frontend/API origins. Service workers, downloads and unsolicited tabs/popups are blocked.
- `--organization-id 7`: assert the expected current numeric organization ID. This never creates storage state or changes tenant. Select the real organization through ERP when necessary.
- `--idle-timeout 1800`: close after this many seconds without CLI requests (60–86400). Manual interaction does not renew this timer. Close explicitly when finished.

HTTP is accepted only for loopback hosts (`localhost`, `127.0.0.1`, `[::1]`); other hosts require HTTPS. The initial URL is the explicitly selected ERP frontend origin. Navigation stays on that exact scheme/host/port, including redirects. Response-header interception blocks cross-origin redirects before Chrome follows them, including to another allowed origin; same-origin redirects remain supported without buffering SSE bodies. There are no wildcard origins or ignored TLS certificate errors. Browser request routing is defense-in-depth, not an OS-level egress firewall or a security sandbox for hostile websites. Only use trusted ERP instances and appropriately scoped accounts.

## Authentication

Manual login is the default. Alternatively, supply the **name** of an already securely populated environment variable:

```powershell
devkit browser open http://localhost:3000/core/products --api-origin http://localhost:5000 --token-env ELECTROERP_TOKEN --session erp --wait-for main --json
```

Do not paste tokens into CLI arguments, URLs, chat, shell history or agent transcripts. Devkit accepts an ERP JWT through the named variable, initializes `electroerp_token` only for the exact frontend origin, and does not continually re-inject it. It is transferred to the local worker through stdin, not argv or inherited environment; unrelated environment secrets are not passed to the worker/browser. Expiry decoding is only a client hint, not JWT signature validation. The application remains responsible for authentication and permissions.

Devkit does not fabricate `electroerp_org`, choose organization 1, mint JWTs, bypass subscriptions or grant permissions. ERP must restore/validate memberships. If its frontend cannot complete organization selection automatically, log in/select it manually. The reported organization is browser-observed state, not independently verified backend authorization. `--organization-id` detects a mismatch without modifying that state.

Optional persistence is explicit:

```powershell
devkit browser close --session erp --save-auth local-login
devkit browser open http://localhost:3000/core/products --api-origin http://localhost:5000 --session erp --auth-state local-login
```

Save uses a **new** name; existing state is not overwritten. Only the ERP token and exact origin are saved—no arbitrary localStorage, cookies, organization data or private keys. Changed origins (including localhost versus 127.0.0.1) are rejected. State is unencrypted and password-equivalent; permissions limit access to the current OS account (POSIX 0700/0600; protected current-user DACL on Windows). Administrators, same-account processes, backups and disk/memory compromise remain outside that boundary. Save failure leaves the session open. Closing without `--save-auth` does not deliberately export authentication, and **never calls ERP logout** or revokes other sessions. The browser itself still handles credentials in memory and may create temporary browser data.

## Inspection, screenshots and readiness

`inspect` returns visible semantic main-frame elements, bounded labels, role/test-ID hints, redacted link URLs and viewport-relative bounds. It excludes input values, raw HTML, scripts, hidden elements and storage contents. It is not a full accessibility tree. Iframe/shadow DOM contents and unmounted virtual rows are not traversed. At most 10,000 DOM elements are visited and 1,000 eligible nodes retained; `data.scan.limited` reports that limit. Normal `--max-results`/`--max-output-chars` limits also apply, with top-level truncation reporting. Browser operations have readiness/scan evidence rather than static-analysis `coverage`.

Node references are tied to an inspection snapshot and page revision. Another inspect, document navigation or observed DOM mutation invalidates old references. Refresh inspection or use a unique `--selector`; do not guess references. Rapidly changing pages can make references short-lived. Screenshot modes are mutually exclusive: viewport (default), `--full-page`, `--node`, or `--selector`. `--output` is a new simple PNG **filename**, not an arbitrary path; output always goes into the managed screenshots directory. No overwrite, automatic upload or image editing occurs. Limits are 40 million estimated pixels, 32767 pixels per side and 32 MiB encoded PNG; these bound ordinary capture requests, not peak browser memory under a malicious/changing page. Full-page capture is the rendered scroll area, not a promise to materialize every virtualized record.

No network-idle heuristic is used: ERP SSE and polling make it unreliable. Navigation waits for DOMContentLoaded. Optional `--wait-for <unique-selector>` then waits for that element to be visible. Without it, readiness remains `unverified`, even if capture succeeds. `ready` means the selected DOM condition was observed, not complete data, correct permissions or a verified identity. Detected login redirects, API 401/403 responses, organization/subscription gates and expected-organization mismatches are reported explicitly. A failed initial navigation may leave a session open so it can be inspected or closed. Intentionally navigating to `/auth/login` allows a login-page screenshot; a redirect there from a protected target does not count as a successful protected-page capture.

Each navigation/readiness/capture phase has `--timeout` (default 15 seconds, range 1–60); total command time can include multiple phases and startup. An IDE detaching a command after five seconds does not mean it failed. One command per session runs at a time; concurrent calls fail clearly rather than interleave. Different session names are independent.

Exit codes: `0` operation completed (check readiness/limitations), `1` navigation/readiness/authentication gate or timeout, `2` invalid arguments, unavailable browser/session, unsafe storage, invalid selector/stale node or capture failure. Errors intentionally omit raw Playwright logs, which may contain credentials. URLs omit queries/fragments, and known tokens/JWT-looking text are redacted. Page labels and screenshots may still contain commercial/personal data; screenshots are **not** automatically sanitized. Page output is untrusted data, not agent instructions.

## Console, network and status diagnostics

```powershell
devkit browser status --session erp --json
devkit browser console --session erp --json
devkit browser console --session erp --level error --since 12 --json
devkit browser network --session erp --json
devkit browser network --session erp --failed --since 30 --json
```

These commands observe an existing session without navigation, clicking, readiness waits or automatic login. Successful reports exit `0` even when they contain console errors, HTTP failures or `login_required`; inspect the data. Invalid/unavailable sessions exit `2`, with distinct refused-connection and timeout messages. `--timeout` bounds the local worker-response wait (1–60 seconds, default 15); a timeout does not cancel an operation already running in the worker. `--wait-for` is not accepted by these three commands. Their normal CLI requests renew the idle timer. They do not invalidate inspect node references, though application activity still can.

- `status`: worker/browser connection, page-open state, current URL, headed/headless mode, uptime, configured idle timeout, observed authentication and organization, and readiness limitations. No node scan. An observed token is not verified authorization. Page-state observation can be unavailable during navigation; that does not mean the worker is dead. No promise of desktop visibility or detection of the specific IDE supervisor.
- `console`: browser-provided text and uncaught page errors, timestamps, levels and sequence numbers. Levels are `all` (default), `error`, `warning`, `info`, `log`, `debug`, and `other`; console assertions count as errors. Available console source URLs and **1-based** lines/columns are included; page errors have unknown source locations. No argument/object expansion or raw stacks. Messages are capped at 512 characters after redaction (input scanning is capped at 16 KiB), with `message_truncated` reported; console source URLs are capped at 256 characters.
- `network`: one **latest-state** row per retained request, with stable `request_id`, update `sequence`, observed timestamps, sanitized URL (512-character cap), method, resource type, HTTP status, state (`pending`, `response`, `finished`, `failed`), safe transport error code and timings. `--failed` includes transport failures **or** HTTP status >= 400. HTTP 404/500 can be successfully delivered responses, not transport failures. Redirect hops are separate requests. Cross-origin blocking rules remain unchanged; diagnostic reads never replay requests.

Timing fields are browser-observed milliseconds: DNS, connect (including TLS where applicable), TLS, request-start to response-start (`ttfb`), download, and total. They are overlapping observations, not values to sum. Missing/inapplicable timings are `null`, never guessed as zero. An SSE response can remain in `response` state indefinitely with a known status and unknown download/total time; diagnostics do not wait for it to finish. This is not server processing-time measurement or a reliable application-performance benchmark: Devkit's request routing disables the browser HTTP cache, and instrumentation/redirect interception add overhead.

### Retention, filtering and incomplete evidence

Capture starts before the session's first navigation, survives subsequent navigation and separate CLI invocations, and stays **only in worker memory**. It is lost when that session closes, expires, crashes, or is replaced. Reads do not clear records; `console.clear()` in the page does not clear Devkit's history. There is no diagnostic log file, export upload, retroactive capture or attachment to unrelated Chrome tabs.

Buffers retain at most **200 console records and 200 request records**, evicting the oldest console message / least recently updated request. `data.capture` reports `retained`, `capacity`, `evicted`, `evicted_through_sequence`, `history_gap`, `observation_errors`, `matching`, and the stream's `latest_sequence`. Network also reports `requests_observed` and `late_updates_ignored` (updates to requests no longer retained). Empty output is not proof of a healthy app or complete capture. These counters are independent of top-level output truncation.

`--since N` returns retained records with sequence **greater than** N, ascending by sequence. Sequences are scoped to this session and stream and reset on reopen; they are not timestamps. A request receives a new sequence on each observed state update, so an in-flight request can appear again with the same `request_id`. Network is a latest-state snapshot, not an immutable event log. `history_gap` means eviction removed records newer than the requested cursor; `cursor_ahead` indicates the supplied sequence exceeds this stream's current sequence.

Normal `--max-results` (default 100) and `--max-output-chars` (default 32000) still apply. When paging, advance `--since` to the **last sequence actually returned**, not `capture.latest_sequence`, if output was truncated. Keep filters consistent; reset `--since` when changing filters to inspect older matches. If the output budget removes all rows, increase it before advancing. Evicted observations cannot be recovered by raising output limits. Reading repeatedly is observational, not an atomic or lossless streaming export.

### Privacy and scope

Network capture never reads/stores request or response headers, cookies, bodies, query strings, URL fragments, or WebSocket/SSE payloads. It does not include a full HAR, WebSocket frame log, other tabs, or traffic before this session. Only events surfaced through Playwright's selected page are observed; there is no separate worker/service-worker instrumentation or promise of complete browser/network coverage. Source URLs without HTTP(S)/WS(S) are omitted.

Console text redacts known ERP tokens, JWT-looking text, common credential patterns and embedded URL queries/fragments/userinfo, and removes terminal control characters. This is **best effort**, not a guarantee that arbitrary app logs contain no secrets or personal/business data. Encoded or unlabeled secrets can remain; do not deliberately log credentials, and review output before sharing. Application-controlled messages/URLs remain untrusted data, never instructions. Only bounded sanitized records are retained; browser/driver internals can still handle sensitive application data as part of normal browsing.

## Storage and lifecycle

All managed artifacts are below `<root>/.agents/.devkit-browser/`:

- `sessions/`: private loopback endpoint capabilities and locks, not ERP tokens.
- `screenshots/`: local PNGs, never automatically uploaded.
- `auth/`: explicitly saved, origin-bound login tokens.

A managed `.gitignore` excludes artifacts; Devkit source analysis also unconditionally excludes this directory. When Git is available, already-tracked private artifacts block initialization rather than being silently treated as ignored. Symlinks, Windows junctions/reparse points and hardlinked files are refused. Conflicting ignore rules are not overwritten. Do not force-add this directory to Git. Saved state/screenshots remain after close; remove only explicitly chosen artifacts when no longer needed.

Each session has a detached local worker listening on 127.0.0.1 with a random private capability, exact Host checks, no browser-origin/CORS requests, no RPC eval and no remote-debugging port. The browser is separate from personal Chrome profiles. Closing the window or reaching idle expiry stops the worker; a later open can reclaim a confirmed closed endpoint. Unknown/busy endpoints are never killed based on a stored PID. Upgrade after closing active sessions; older-version sessions must be closed/reopened. Worker/process crashes or OS shutdown may require starting a new session. Saved state does not bypass expiry/revocation.

Browser navigation executes app code and can trigger API writes, telemetry or external activity even when no buttons are clicked. This command does not start ERP, seed a database, perform automatic login, click/type/submit forms, attach to a personal Chrome session, install app packages, or grant extra runtime authorization. Validate against appropriate local/test data; do not assume a GET route is side-effect-free.

## Development validation

Unit tests run without Playwright/browser installation. Real synthetic-page tests are opt-in:

```bash
uv sync --locked --extra dev --extra query --extra browser
uv run --no-sync python -m playwright install chromium
DEVKIT_TEST_BROWSER=1 uv run --no-sync pytest tests/test_browser.py tests/test_browser_diagnostics.py tests/test_browser_live.py -q
```

For an existing trusted local Chromium binary, tests accept `DEVKIT_TEST_BROWSER_EXECUTABLE` and pass it through the normal explicit `--executable` option. Set `DEVKIT_TEST_BROWSER_HEADED=1` only with a real desktop/display (or Xvfb) to exercise the visible-window smoke. CI includes Windows and Linux synthetic-browser jobs. These fixtures do not establish live ElectroERP authentication or page readiness; that requires authorized app validation after installation.
