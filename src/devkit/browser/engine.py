"""A single isolated ERP page. Imported only by the optional browser worker."""

from __future__ import annotations

import json
import re
import time
import uuid
from urllib.parse import urljoin, urlsplit

from ..project import DevkitError
from .policy import ORG_KEY, TOKEN_KEY, expired, origin, redact, safe_url, storage_state
from .storage import write, write_json

# State lives in the browser, not in application DOM attributes. The random key is
# an implementation detail, not a security boundary against the inspected page.
OBSERVE = """key => {
  const state = { revision: 0 };
  Object.defineProperty(window, key, {value: state});
  new MutationObserver(() => state.revision++).observe(document, {
    subtree: true, childList: true, attributes: true, characterData: true
  });
}"""
SNAPSHOT = """({key, limit}) => {
  const tags = new Set(['MAIN','ASIDE','HEADER','NAV','SECTION','ARTICLE','H1','H2','H3',
    'H4','H5','H6','A','BUTTON','INPUT','SELECT','TEXTAREA','TABLE','FORM','IMG']);
  const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_ELEMENT);
  const nodes = []; let visited = 0; let element; let limited = false;
  while ((element = walker.nextNode())) {
    if (++visited > 10000 || nodes.length >= limit) { limited = true; break; }
    if (!tags.has(element.tagName) && !element.hasAttribute('role') && !element.hasAttribute('data-testid')) continue;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    if (!rect.width || !rect.height || style.visibility === 'hidden' || style.display === 'none') continue;
    const isInput = ['INPUT','TEXTAREA','SELECT'].includes(element.tagName);
    // Never return values, HTML, storage, scripts, arbitrary attributes or hidden text.
    const label = element.getAttribute('aria-label') ||
      (isInput ? Array.from(element.labels || []).map(x => x.innerText).join(' ') :
       /^(H[1-6]|A|BUTTON)$/.test(element.tagName) ? element.innerText : '') || '';
    nodes.push({element, info: {tag: element.tagName.toLowerCase(),
      role: (element.getAttribute('role') || '').slice(0,80),
      label: label.slice(0,240), test_id: (element.getAttribute('data-testid') || '').slice(0,160),
      href: element.tagName === 'A' ? element.href.slice(0,2048) : null,
      bounds: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}}});
  }
  return {nodes, visited, limited, revision: window[key]?.revision ?? -1};
}"""


class BrowserEngine:
    def __init__(self, project, options):
        try:
            from playwright.sync_api import Error, TimeoutError, sync_playwright
        except ImportError:
            raise DevkitError(
                'Browser addon missing. Install with: python -m pip install ".[browser]"'
            ) from None

        self.error = Error
        self.timeout_error = TimeoutError
        self.project = project
        self.options = options
        self.frontend = options["origin"]
        self.allowed = set(options["allowed_origins"])
        self.auth_origins = {self.frontend, options["api_origin"]}
        self.secrets = [options.pop("token", None)]
        self.key = "__devkit_" + uuid.uuid4().hex
        self.nodes = {}
        self.revision = None
        self.document = 0
        self.closed = False
        self.blocked_requests = 0
        self.api_denials = set()
        self.playwright = sync_playwright().start()
        self.browser = None
        self.context = None
        try:
            self.browser = self.playwright.chromium.launch(
                channel=options["channel"]
                if options["channel"] == "chrome" and not options.get("executable")
                else None,
                executable_path=options.get("executable"),
                headless=options["headless"],
                timeout=30000,
            )
            self.context = self.browser.new_context(
                storage_state=storage_state(self.frontend, self.secrets[0]),
                accept_downloads=False,
                service_workers="block",
                viewport={"width": options["width"], "height": options["height"]},
            )
            self.context.set_default_timeout(options["timeout"] * 1000)
            self.context.route("**/*", self.route)
            self.context.route_web_socket("**/*", self.websocket)
            # The observer contains no credentials and does not re-inject login state.
            self.context.add_init_script(f"({OBSERVE})({json.dumps(self.key)})")
            self.page = self.context.new_page()
            # Playwright routing only handles the first URL of some redirect
            # chains. Intercept response headers via CDP as well, before Chrome
            # follows a Location header; never buffer SSE response bodies.
            self.cdp = self.context.new_cdp_session(self.page)
            self.cdp.on("Fetch.requestPaused", self.redirect_response)
            self.cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
            self.page.on("framenavigated", self.navigated)
            self.page.on("response", self.response)
            self.page.on("dialog", lambda dialog: dialog.dismiss())
            self.page.on("download", lambda download: download.cancel())
            self.context.on("page", lambda page: page.close() if page != self.page else None)
        except Exception:  # noqa: BLE001 -- never expose browser launch logs or secret-bearing options
            self.close()
            raise DevkitError(
                "Could not launch the isolated browser. Install Google Chrome for the default channel, "
                "or run 'python -m playwright install chromium' and use --channel chromium. "
                "Visible mode also needs a desktop/display. Browser logs are omitted to protect credentials."
            ) from None

    def route(self, route):
        request = route.request
        try:
            target = origin(request.url)
            allowed = target in self.allowed
            if request.is_navigation_request():
                allowed = target == self.frontend
            if target not in self.auth_origins:
                headers = request.all_headers()
                if (
                    headers.get("authorization")
                    or headers.get("cookie")
                    or any(secret and secret in request.url for secret in self.secrets)
                ):
                    allowed = False
            if allowed:
                route.continue_()
                return
        except (DevkitError, self.error, ValueError):
            pass
        self.blocked_requests += 1
        route.abort("blockedbyclient")

    def websocket(self, route):
        try:
            url = route.url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
            if origin(url) in self.auth_origins:
                route.connect_to_server()
                return
        except (DevkitError, self.error):
            pass
        self.blocked_requests += 1
        route.close()

    def redirect_response(self, event):
        request_id = event["requestId"]
        try:
            status = event.get("responseStatusCode")
            if status in {301, 302, 303, 307, 308}:
                headers = event.get("responseHeaders", [])
                locations = [item["value"] for item in headers if item["name"].lower() == "location"]
                source = event["request"]["url"]
                if len(locations) != 1 or origin(urljoin(source, locations[0])) != origin(source):
                    self.blocked_requests += 1
                    self.cdp.send(
                        "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                    )
                    return
            method = "Fetch.continueResponse" if status else "Fetch.continueRequest"
            self.cdp.send(method, {"requestId": request_id})
        except (DevkitError, KeyError, ValueError, self.error):
            self.blocked_requests += 1
            try:
                self.cdp.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )
            except self.error:
                pass

    def response(self, response):
        try:
            if (
                origin(response.url) in self.auth_origins
                and urlsplit(response.url).path.startswith("/api/")
                and response.status in {401, 403}
            ):
                self.api_denials.add(response.status)
        except (DevkitError, ValueError):
            pass

    def navigated(self, frame):
        if frame == self.page.main_frame:
            self.document += 1
            self.api_denials.clear()
            self.invalidate()

    def invalidate(self):
        for handle in self.nodes.values():
            try:
                handle.dispose()
            except self.error:
                pass
        self.nodes.clear()
        self.revision = None

    def status(self):
        if self.closed or self.page.is_closed() or not self.browser.is_connected():
            raise DevkitError("The browser window was closed. Start a new session with browser open.")
        url = self.page.url
        result = {
            "session": self.options["session"],
            "url": safe_url(url),
            "headless": self.options["headless"],
            "channel": self.options["channel"],
            "browser_version": self.browser.version,
            "blocked_requests": self.blocked_requests,
            "authentication": "not_verified",
            "organization_id": None,
            "readiness": "unverified",
            "readiness_basis": "DOM evidence only; not backend authorization proof",
        }
        if url == "about:blank":
            return result
        if origin(url) != self.frontend:
            result["readiness"] = "outside_origin"
            return result
        state = self.page.evaluate(
            """keys => ({
          token: localStorage.getItem(keys[0]), org: localStorage.getItem(keys[1]),
          loading: document.readyState === 'loading',
          busy: !!document.querySelector('main[aria-busy="true"], main [aria-busy="true"]')
        })""",
            [TOKEN_KEY, ORG_KEY],
        )
        token = state["token"]
        if token and token not in self.secrets:
            # Bounded retention of actual tokens for output redaction only.
            self.secrets = (self.secrets + [token])[-16:]
        org_id = None
        try:
            organization = json.loads(state["org"] or "null")
            if isinstance(organization, dict) and isinstance(organization.get("id"), (str, int)):
                candidate = str(organization["id"])
                if re.fullmatch(r"[0-9]{1,20}", candidate):
                    org_id = candidate
        except ValueError:
            pass
        result["organization_id"] = org_id
        result["authentication"] = (
            "expired_client_token"
            if expired(token)
            else "client_token_present"
            if token
            else "no_client_token"
        )
        path = urlsplit(url).path
        if path.startswith("/auth"):
            result["readiness"] = "login_required"
        elif 401 in self.api_denials or expired(token):
            result["readiness"] = "authentication_required"
        elif 403 in self.api_denials:
            result["readiness"] = "access_denied"
        elif path.startswith("/core/organizations/setup"):
            result["readiness"] = "organization_required"
        elif path.startswith("/core/subscriptions"):
            result["readiness"] = "subscription_required"
        elif path.startswith("/error"):
            result["readiness"] = "application_error"
        elif path.startswith("/core") and not token:
            result["readiness"] = "authentication_required"
        elif path.startswith("/core") and not org_id:
            result["readiness"] = "organization_required"
        elif self.options.get("organization_id") and org_id != self.options["organization_id"]:
            result["readiness"] = "wrong_organization"
        elif state["loading"] or state["busy"]:
            result["readiness"] = "loading"
        return result

    def wait_ready(self, options, intended=None):
        deadline = time.monotonic() + options["timeout"]
        selector = options.get("wait_for")
        result = self.status()
        while True:
            result = self.status()
            # Intentionally opening the login page is useful for manual authentication.
            login_target = intended and urlsplit(intended).path.startswith("/auth")
            blocked = result["readiness"] not in {"unverified", "loading"} and not (
                login_target and result["readiness"] == "login_required"
            )
            if blocked:
                if (
                    result["readiness"]
                    in {"organization_required", "authentication_required", "wrong_organization"}
                    and time.monotonic() < deadline
                ):
                    self.page.wait_for_timeout(100)
                    continue
                return result, 1
            visible = False
            if selector:
                locator = self.page.locator(selector)
                count = locator.count()
                if count > 1:
                    raise DevkitError(
                        "Readiness selector matches multiple elements; choose a unique selector."
                    )
                visible = count == 1 and locator.is_visible()
            if result["readiness"] != "loading" and (not selector or visible):
                if selector:
                    result["readiness"] = "ready"
                    result["readiness_basis"] = (
                        "Explicit readiness selector visible; not authorization/data completeness proof"
                    )
                return result, 0
            if time.monotonic() >= deadline:
                result["readiness"] = "timeout"
                return result, 1
            self.page.wait_for_timeout(100)

    def navigate(self, options):
        self.invalidate()
        self.api_denials.clear()
        try:
            response = self.page.goto(
                options["url"], wait_until="domcontentloaded", timeout=options["timeout"] * 1000
            )
        except self.timeout_error:
            return {
                **self.status(),
                "readiness": "timeout",
                "message": "Navigation timed out; session remains open.",
            }, 1
        except self.error:
            # A denied redirect can leave a pending Chrome error-page navigation.
            # Settle on a credential-free blank page, never retry the failed URL.
            for _ in range(2):
                try:
                    self.page.goto("about:blank", wait_until="domcontentloaded", timeout=1000)
                    break
                except self.error:
                    pass
            raise DevkitError(
                "Navigation failed or was blocked by the origin policy; session remains open. Raw browser errors are omitted."
            ) from None
        data, code = self.wait_ready(options, intended=options["url"])
        if response and response.status >= 400:
            data.update(http_status=response.status, readiness="http_error")
            code = 1
        return data, code

    def inspect(self, options):
        data, code = self.wait_ready(options)
        self.invalidate()
        if data["readiness"] == "outside_origin":
            return data, code
        document = self.document
        snapshot = self.page.evaluate_handle(SNAPSHOT, {"key": self.key, "limit": 1000})
        array = None
        try:
            metadata = snapshot.evaluate(
                "s => ({visited: s.visited, limited: s.limited, revision: s.revision})"
            )
            array = snapshot.get_property("nodes")
            generation = uuid.uuid4().hex[:12]
            rows = []
            for index, row in array.get_properties().items():
                if not index.isdigit():
                    row.dispose()
                    continue
                try:
                    info = row.get_property("info")
                    try:
                        item = info.json_value()
                    finally:
                        info.dispose()
                    element = row.get_property("element").as_element()
                    reference = f"n{generation}_{index}"
                    self.nodes[reference] = element
                    item["node"] = reference
                    if item["href"]:
                        item["href"] = safe_url(item["href"])
                    for key in ("label", "test_id", "role"):
                        item[key] = redact(item[key], self.secrets)
                    rows.append(item)
                finally:
                    row.dispose()
            self.revision = (document, metadata["revision"])
            data.update(
                nodes=rows,
                snapshot=generation,
                scan={
                    "visited_elements": metadata["visited"],
                    "limited": metadata["limited"],
                    "scope": "Visible semantic elements in main frame; iframe/shadow DOM and offscreen virtual rows not traversed",
                },
            )
        finally:
            if array:
                array.dispose()
            snapshot.dispose()
        return data, code

    def screenshot(self, options):
        data, code = self.wait_ready(options, intended=options.get("expected_url"))
        # Public/login pages can be deliberately captured; a previous navigation
        # target is required to distinguish these from redirects to login.
        if code:
            data["captured"] = False
            return data, code
        target = self.page
        if options.get("node"):
            revision = self.page.evaluate("key => window[key]?.revision ?? -1", self.key)
            if self.revision != (self.document, revision) or options["node"] not in self.nodes:
                raise DevkitError(
                    "Stale or unknown node reference. Run browser inspect again, or use a unique --selector."
                )
            target = self.nodes[options["node"]]
            if not target.evaluate("node => node.isConnected"):
                raise DevkitError("Node is detached; inspect again before capturing it.")
        elif options.get("selector"):
            target = self.page.locator(options["selector"])
            if target.count() != 1:
                raise DevkitError("Screenshot selector must match exactly one element; no capture was saved.")
        dimensions = (
            target.evaluate("""node => {
          const r = node.getBoundingClientRect(); return {width:r.width,height:r.height};
        }""")
            if target != self.page
            else self.page.evaluate(
                """full => ({
          width: full ? Math.max(document.documentElement.scrollWidth,innerWidth) : innerWidth,
          height: full ? Math.max(document.documentElement.scrollHeight,innerHeight) : innerHeight
        })""",
                options.get("full_page", False),
            )
        )
        if dimensions["width"] * dimensions["height"] > 40_000_000 or max(dimensions.values()) > 32767:
            raise DevkitError(
                "Screenshot exceeds the 40-million-pixel / 32767-pixel-side limit; capture a smaller element or viewport."
            )
        kwargs = {"type": "png", "timeout": options["timeout"] * 1000, "animations": "disabled"}
        if target == self.page:
            kwargs["full_page"] = options.get("full_page", False)
        content = target.screenshot(**kwargs)
        if len(content) > 32 * 1024 * 1024:
            raise DevkitError("Screenshot exceeds 32 MiB; no artifact was saved.")
        filename = options.get("output") or f"{self.options['session']}-{uuid.uuid4().hex}.png"
        destination = self.project.base / "screenshots" / filename
        write(destination, content)
        data.update(
            captured=True,
            path=str(destination),
            relative_path=destination.relative_to(self.project.root).as_posix(),
            bytes=len(content),
            format="png",
            full_page=bool(options.get("full_page")),
            element=bool(options.get("node") or options.get("selector")),
        )
        return data, 0

    def save_auth(self, label):
        data = self.status()
        if data["authentication"] != "client_token_present" or data["readiness"] in {
            "access_denied",
            "authentication_required",
            "login_required",
        }:
            raise DevkitError(
                "No usable ERP client login state to save. Log in and inspect the session first."
            )
        token = self.page.evaluate("key => localStorage.getItem(key)", TOKEN_KEY)
        if not token or expired(token):
            raise DevkitError("ERP token is missing or expired; authentication state was not saved.")
        # Only the ERP token is retained. ERP must restore/validate memberships on
        # the next open; never copy arbitrary cookies, tenant data or unrelated storage.
        write_json(self.project.auth(label), {"version": 1, "origin": self.frontend, "token": token})

    def close(self):
        self.closed = True
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
        finally:
            self.playwright.stop()

    def run(self, action, options):
        try:
            if action == "ping":
                return {"alive": True}, 0
            if action == "close":
                if options.get("save_auth"):
                    self.save_auth(options["save_auth"])
                self.close()
                return {
                    "session": self.options["session"],
                    "closed": True,
                    "logged_out": False,
                    "saved_auth": options.get("save_auth"),
                }, 0
            return {"navigate": self.navigate, "inspect": self.inspect, "screenshot": self.screenshot}[
                action
            ](options)
        except self.timeout_error:
            return {
                "session": self.options["session"],
                "readiness": "timeout",
                "message": "Browser operation timed out; no successful capture is claimed.",
            }, 1
        except self.error:
            raise DevkitError(
                "Browser operation failed (invalid selector, closed window or changed page). Inspect again; raw errors are omitted to protect credentials."
            ) from None
