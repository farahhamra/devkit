"""Conservative Express middleware and Prisma scope contracts over full AST nodes."""

from collections import defaultdict
from pathlib import Path

import tree_sitter_javascript as javascript
import tree_sitter_typescript as typescript
from tree_sitter import Language, Parser

from ..schema import Schema
from ..source import SourceIndex, literal, text, walk
from ..structure import span
from . import check, finding

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "all"}
READS = {
    "findMany",
    "findFirst",
    "findFirstOrThrow",
    "findUnique",
    "findUniqueOrThrow",
    "count",
    "aggregate",
    "groupBy",
}
WRITES = {"update", "updateMany", "updateManyAndReturn", "delete", "deleteMany"}


def parse(content, path):
    language = (
        typescript.language_tsx()
        if path.endswith(".tsx")
        else typescript.language_typescript()
        if Path(path).suffix in {".ts", ".mts", ".cts"}
        else javascript.language()
    )
    return Parser(Language(language)).parse(content).root_node


def arguments(node):
    args = node.child_by_field_name("arguments")
    return [c for c in args.named_children if c.type != "comment"] if args else []


def properties(node):
    """None means dynamic, duplicate, spread or otherwise unsupported object syntax."""
    if node is None or node.type != "object":
        return None
    result = {}
    for child in node.named_children:
        if child.type == "comment":
            continue
        if child.type != "pair":
            return None
        key = child.child_by_field_name("key")
        name = (
            literal(key) if key.type == "string" else text(key) if key.type == "property_identifier" else None
        )
        if name is None or name in result:
            return None
        result[name] = child.child_by_field_name("value")
    return result


def top_level(node):
    return bool(
        node.parent
        and node.parent.type == "expression_statement"
        and node.parent.parent
        and node.parent.parent.type == "program"
    )


def joined(prefix, path):
    return "/" + "/".join(p for p in (prefix.strip("/"), path.strip("/")) if p)


def matches_prefix(prefix, path):
    return prefix == "/" or path == prefix or path.startswith(prefix.rstrip("/") + "/")


def literal_values(node):
    value = literal(node)
    if value is not None:
        # Escaped strings need JS interpretation. Do not silently compare raw escapes.
        return {value} if "\\" not in value else None
    if node is not None and node.type == "array":
        values = [literal_values(c) for c in node.named_children if c.type != "comment"]
        return set().union(*values) if values and all(v is not None for v in values) else None
    return None


class StaticAudit:
    def __init__(self, project, args, policy):
        self.project, self.args, self.policy = project, args, policy
        self.selected = {project.rel(p) for p in project.entries(args.path)}
        self.index = SourceIndex(
            project,
            paths=None if args.action == "routes" else [project.resolve(p) for p in sorted(self.selected)],
        )
        self.content = {p: project.text(project.resolve(p)).encode("utf-8") for p in self.index.files}
        self.roots = {p: parse(content, p) for p, content in self.content.items()}
        self.identities = {}
        self.checks, self.findings = [], []
        self.events = defaultdict(list)
        self.invalid = set()
        self.guard_origins = defaultdict(list)
        self.unresolved_guards = set()
        for kind, guards in policy.get("guards", {}).items():
            for guard in guards:
                guard_app = project.app_for(project.resolve(guard["path"], False))
                if project.app != "all" and guard_app not in {None, project.app}:
                    continue
                origin = self.index.export_origin(guard["path"], guard["symbol"])
                if origin and not self.index.files[origin[0]]["errors"]:
                    self.guard_origins[origin].append((kind, guard))
                else:
                    self.unresolved_guards.add(kind)
                    project.coverage.gap("audit_guard_unresolved", guard["path"], stage="audit")

    def loc(self, path, node):
        return span(node, self.content[path], path)

    def result(self, limitations):
        return {
            "engines": [{"name": "devkit", "kind": "static_ast"}],
            "checks": self.checks,
            "findings": self.findings,
            "limitations": limitations,
        }

    def identity(self, path, node):
        if node is None or node.type != "identifier":
            return None
        name = text(node)
        key = (path, name)
        if key not in self.identities:
            self.identities[key] = self.resolve_identity(path, name)
        return self.identities[key]

    def resolve_identity(self, path, name):
        # Reassignments or duplicate lexical declarations make identity uncertain.
        declarations = 0
        for other in walk(self.roots[path]):
            if other.type in {
                "assignment_expression",
                "augmented_assignment_expression",
                "update_expression",
            }:
                left = other.child_by_field_name("left") or other.child_by_field_name("argument")
                if text(left) == name:
                    return None
            if (
                other.type in {"variable_declarator", "function_declaration", "class_declaration"}
                and text(other.child_by_field_name("name")) == name
            ):
                declarations += 1
        imports = [
            imp
            for imp in self.index.files[path]["imports"]
            if imp["kind"] == "import" and name in imp["bindings"]
        ]
        if len(imports) + declarations > 1:
            return None
        if imports:
            imp = imports[0]
            return (
                self.index.export_origin(imp.get("resolved"), imp["bindings"][name])
                if imp.get("resolved")
                else None
            )
        return (path, name) if declarations == 1 else None

    def router(self, path, node):
        origin = self.identity(path, node)
        return origin if origin and origin[1] in self.index.files[origin[0]]["routers"] else None

    def middleware(self, path, nodes, *, handler=False):
        result, unknown = [], False
        for i, node in enumerate(nodes):
            called = node.type == "call_expression"
            origin = self.identity(path, node.child_by_field_name("function") if called else node)
            guards = self.guard_origins.get(origin, [])
            if len(guards) == 1:
                kind, spec = guards[0]
                values = None
                if kind == "authentication" and called:
                    unknown = True
                    continue
                if kind != "authentication":
                    args = arguments(node) if called else []
                    arg = spec.get("argument", 0)
                    values = literal_values(args[arg]) if len(args) == arg + 1 else None
                    if values is None:
                        unknown = True
                        continue
                result.append(
                    {
                        "kind": kind,
                        "values": sorted(values or []),
                        "mode": spec.get("mode", "all"),
                        "definition": {"path": origin[0], "symbol": origin[1]},
                        **self.loc(path, node),
                    }
                )
            elif not (handler and i == len(nodes) - 1) and (origin is None or called or guards):
                unknown = True
        return result, unknown

    def routes(self):
        for path, root in self.roots.items():
            self.project.coverage.record("audit_selected", path)
            self.project.coverage.record("audit_analyzed", path)
            for node in walk(root):
                if node.type != "call_expression":
                    continue
                callee = node.child_by_field_name("function")
                if callee is None or callee.type != "member_expression":
                    continue
                receiver = callee.child_by_field_name("object")
                method = text(callee.child_by_field_name("property"))
                if method not in METHODS | {"use", "route"}:
                    continue
                router = self.router(path, receiver)
                if router is None:
                    # Chained and aliased registrations are visible as unknown candidates.
                    if method == "route" or (receiver and receiver.type == "call_expression"):
                        self.checks.append(
                            check(
                                "route_registration",
                                "unknown",
                                "Unsupported router identity or chained registration",
                                self.loc(path, node),
                            )
                        )
                    continue
                if router[0] != path:
                    self.invalid.add(router)
                    self.checks.append(
                        check(
                            "route_registration",
                            "unknown",
                            "Registration mutates an imported router; cross-file execution order is unresolved",
                            self.loc(path, node),
                        )
                    )
                    continue
                args = arguments(node)
                route = literal(args[0]) if args else None
                if (
                    method == "use"
                    and route is None
                    and args
                    and args[0].type not in {"string", "template_string", "regex", "array"}
                ):
                    route = "/"
                    handlers = args
                else:
                    handlers = args[1:]
                if (
                    not top_level(node)
                    or route is None
                    or "\\" in route
                    or method == "route"
                    or self.index.files[path]["errors"]
                ):
                    self.invalid.add(router)
                    self.checks.append(
                        check(
                            "route_registration",
                            "unknown",
                            "Dynamic/conditional registration, unsupported path, or parse error",
                            self.loc(path, node),
                        )
                    )
                    continue
                self.events[router].append((node, method, route, handlers))
        entries = self.policy.get("entrypoints", [])
        roots = [
            (e["path"], e["router"])
            for e in entries
            if self.project.app in {"all", self.project.app_for(self.project.resolve(e["path"], False))}
        ]
        if not roots:
            self.checks.append(
                check(
                    "route_entrypoints",
                    "unknown",
                    "Configure explicit Express app/router entrypoints in audit.yaml",
                )
            )
        visited = set()
        policies_seen = set()
        exposures = 0

        def expand(router, prefix, inherited, ancestry, uncertain=False):
            nonlocal exposures
            if router in ancestry or len(ancestry) > 30:
                self.checks.append(
                    check(
                        "route_mount",
                        "unknown",
                        "Cyclic or excessively deep router mounts",
                        {"path": router[0]},
                    )
                )
                return
            if router not in self.events:
                self.checks.append(
                    check(
                        "route_mount",
                        "unknown",
                        "Entrypoint/mounted router has no resolved registrations",
                        {"path": router[0]},
                        router=router[1],
                    )
                )
                return
            inherited = list(inherited)
            uncertain |= router in self.invalid
            for node, method, local_path, nodes in self.events[router]:
                full = joined(prefix, local_path)
                visited.add((router[0], node.start_byte))
                if method == "use":
                    pending = []
                    for item in nodes:
                        child = self.router(router[0], item)
                        if child:
                            guards, unknown = self.middleware(router[0], pending)
                            expand(
                                child,
                                full,
                                inherited + [(full, guards, unknown)],
                                ancestry | {router},
                                uncertain,
                            )
                            # Middleware after a child does not retroactively protect it.
                        else:
                            pending.append(item)
                    guards, unknown = self.middleware(router[0], pending)
                    inherited.append((full, guards, unknown))
                    continue
                if router[0] not in self.selected:
                    continue
                exposures += 1
                if exposures > 50000:
                    self.project.coverage.gap("audit_route_expansion_limit", stage="audit")
                    return
                guards, unknown = self.middleware(router[0], nodes, handler=True)
                unknown |= uncertain
                for mount, prior, partial in inherited:
                    if matches_prefix(mount, full):
                        guards = prior + guards
                        unknown |= partial
                location = self.loc(router[0], node)
                app = self.project.app_for(self.project.resolve(router[0]))
                matching = [
                    (i, p)
                    for i, p in enumerate(self.policy.get("routes", []))
                    if p["app"] == app and p["route"] == full and p["method"] in {method.upper(), "ALL"}
                ]
                details = {"app": app, "method": method.upper(), "route": full, "middleware": guards}
                if len(matching) != 1:
                    self.checks.append(
                        check(
                            "route_policy",
                            "unknown",
                            "No unique policy for exposed route",
                            location,
                            **details,
                        )
                    )
                    continue
                number, rule = matching[0]
                policies_seen.add(number)
                checks = [
                    (
                        "authentication",
                        "not_applicable"
                        if rule["authentication"] == "public"
                        else "pass"
                        if any(g["kind"] == "authentication" for g in guards)
                        else "unknown"
                        if unknown
                        else "fail",
                    )
                ]
                for kind, mode_key, default in (
                    ("permissions", "permission_mode", "all"),
                    ("roles", "role_mode", "any"),
                ):
                    expected = set(rule.get(kind, []))
                    selected = [g for g in guards if g["kind"] == kind]
                    certain = set().union(
                        *(set(g["values"]) for g in selected if g["mode"] == "all" or len(g["values"]) == 1)
                    )
                    satisfied = (
                        expected <= certain
                        if rule.get(mode_key, default) == "all"
                        else bool(expected & certain)
                        or any(set(g["values"]) and set(g["values"]) <= expected for g in selected)
                    )
                    status = (
                        "not_applicable"
                        if not expected
                        else "pass"
                        if satisfied
                        else "unknown"
                        if unknown
                        else "fail"
                    )
                    checks.append((kind, status))
                for kind, status in checks:
                    if status != "not_applicable" and (
                        not self.policy.get("guards", {}).get(kind) or kind in self.unresolved_guards
                    ):
                        status = "unknown"
                    # Unsupported registration/mount ordering cannot certify even a visible guard.
                    if uncertain and status == "pass":
                        status = "unknown"
                    message = {
                        "pass": "Configured middleware contract observed",
                        "fail": "Required configured middleware contract missing",
                        "unknown": "Middleware contract could not be established",
                        "not_applicable": "Policy does not require this guard",
                    }[status]
                    self.checks.append(check("route_" + kind, status, message, location, **details))
                    if status == "fail":
                        self.findings.append(
                            finding(
                                "route_" + kind, "high", message, location, route=full, method=method.upper()
                            )
                        )

        for root in roots:
            expand(root, "/", [], set())
        for router, events in self.events.items():
            for node, method, route, _ in events:
                if (
                    method in METHODS
                    and router[0] in self.selected
                    and (router[0], node.start_byte) not in visited
                ):
                    self.checks.append(
                        check(
                            "route_exposure",
                            "unknown",
                            "Registration not reachable from configured entrypoints",
                            self.loc(router[0], node),
                            route=route,
                            method=method.upper(),
                        )
                    )
        for i, policy in enumerate(self.policy.get("routes", [])):
            if i not in policies_seen and self.project.app in {"all", policy["app"]}:
                self.checks.append(
                    check(
                        "route_policy_target",
                        "unknown",
                        "Configured route was not uniquely observed in this target",
                        route=policy["route"],
                        method=policy["method"],
                    )
                )
        return self.result(
            [
                "Checks match configured exported guard identities and literal guard arguments; guard implementation and runtime enforcement are not proven.",
                "Supports top-level Express registrations, imported/same-file routers, ordered .use middleware and literal mount prefixes. Dynamic registrations, wrappers and chained .route calls remain unknown.",
                "Route discovery scans the selected app to resolve mounts; the positional path filters route findings. Auth inside handlers is outside the middleware contract.",
            ]
        )

    def isolation(self):
        schema = Schema(self.project)
        models = {(m["app"], m["name"][0].lower() + m["name"][1:]): m for m in schema.models}
        policies = {(m["app"], m["model"]): m for m in self.policy.get("isolation", {}).get("models", [])}
        if not schema.models:
            self.checks.append(check("isolation_schema", "unknown", "No live Prisma models found"))
        for key, policy in policies.items():
            if self.project.app not in {"all", key[0]}:
                continue
            found = [m for m in schema.models if (m["app"], m["name"]) == key]
            if len(found) != 1 or (
                not policy.get("global")
                and not any(
                    f["name"] == policy["field"] and f["kind"] == "scalar" for f in found[0]["fields"]
                )
            ):
                self.checks.append(
                    check(
                        "isolation_policy",
                        "unknown",
                        "Policy model/scope field does not match a unique live model",
                        model=key[1],
                        app=key[0],
                    )
                )
        for path, root in self.roots.items():
            self.project.coverage.record("audit_selected", path)
            self.project.coverage.record("audit_analyzed", path)
            app = self.project.app_for(self.project.resolve(path))
            for node in walk(root):
                if node.type != "call_expression":
                    continue
                callee = node.child_by_field_name("function")
                if callee is None:
                    continue
                # Explicitly expose raw queries: Prisma scope cannot be inferred from SQL here.
                if text(callee).endswith(
                    (".$queryRaw", ".$queryRawUnsafe", ".$executeRaw", ".$executeRawUnsafe")
                ):
                    self.checks.append(
                        check(
                            "organization_scope",
                            "unknown",
                            "Raw SQL requires separate tenant-isolation review",
                            self.loc(path, node),
                        )
                    )
                    continue
                if callee.type != "member_expression":
                    continue
                delegate = callee.child_by_field_name("object")
                operation = text(callee.child_by_field_name("property"))
                if delegate is None or delegate.type != "member_expression":
                    if operation in READS | WRITES | {"create", "createMany", "upsert"}:
                        self.checks.append(
                            check(
                                "organization_scope",
                                "unknown",
                                "Possible aliased/computed Prisma delegate; identity is unresolved",
                                self.loc(path, node),
                            )
                        )
                    continue
                model = models.get((app, text(delegate.child_by_field_name("property"))))
                if model is None:
                    continue
                receiver = text(delegate.child_by_field_name("object"))
                policy = policies.get((app, model["name"]))
                location = self.loc(path, node)
                status, message = (
                    "unknown",
                    "Configure an explicit scope contract for this model and receiver",
                )
                if policy and policy.get("global"):
                    status, message = "not_applicable", "Model is explicitly classified as global"
                elif (
                    policy
                    and receiver in policy["receivers"]
                    and any(f["name"] == policy["field"] and f["kind"] == "scalar" for f in model["fields"])
                ):
                    args = arguments(node)
                    options = properties(args[0]) if len(args) == 1 else {} if not args else None
                    if root.has_error or options is None:
                        status, message = "unknown", "Parse error or dynamic options prevent scope validation"
                    elif operation in READS | WRITES:
                        status = scope_status(options.get("where"), policy, missing="fail")
                        message = (
                            "Required where scope "
                            + {
                                "pass": "observed",
                                "fail": "missing or not guaranteed",
                                "unknown": "cannot be resolved",
                            }[status]
                        )
                    elif operation in {"create", "createMany", "createManyAndReturn"}:
                        data = options.get("data")
                        items = data.named_children if data is not None and data.type == "array" else [data]
                        statuses = [
                            scope_status(item, policy, missing="fail", data=True)
                            for item in items
                            if item is None or item.type != "comment"
                        ]
                        status = (
                            "fail"
                            if "fail" in statuses
                            else "unknown"
                            if not statuses or "unknown" in statuses
                            else "pass"
                        )
                        message = (
                            "Required create-data scope "
                            + {"pass": "observed", "fail": "missing", "unknown": "cannot be resolved"}[status]
                        )
                    else:
                        status, message = (
                            "unknown",
                            "Unsupported Prisma operation; inspect its scope manually",
                        )
                self.checks.append(
                    check(
                        "organization_scope",
                        status,
                        message,
                        location,
                        app=app,
                        model=model["name"],
                        operation=operation,
                        receiver=receiver,
                    )
                )
                if status == "fail":
                    self.findings.append(
                        finding(
                            "organization_scope",
                            "high",
                            message,
                            location,
                            model=model["name"],
                            operation=operation,
                        )
                    )
        return self.result(
            [
                "Receiver names and trusted_values are explicit project contracts, not inferred data-flow guarantees. Review the trust of each configured expression.",
                "Checks direct Prisma calls: mandatory where constraints for reads/updates/deletes and data scope for creates. Nested writes, relation reads, raw SQL, extensions, compound unique selectors, upsert and dynamic filters require separate review.",
                "A scoped update check covers row selection only, not whether new data can change ownership. Runtime cross-organization API tests remain necessary.",
            ]
        )


def scope_status(node, policy, *, missing="unknown", data=False):
    if node is None:
        return missing
    props = properties(node)
    if props is None:
        return "unknown"
    direct = props.get(policy["field"])
    trusted = set(policy["trusted_values"])
    if direct is not None:
        value = text(direct)
        equal = properties(direct)
        if not data and equal is not None and set(equal) == {"equals"}:
            value = text(equal["equals"])
        if value in trusted:
            return "pass"
        return "unknown"  # No proof of provenance, even when the field name matches.
    if data:
        return "fail"
    unknown = False
    for key in ("AND", "OR"):
        value = props.get(key)
        if value is None:
            continue
        items = [c for c in value.named_children if c.type != "comment"] if value.type == "array" else [value]
        statuses = [scope_status(item, policy) for item in items]
        if (key == "AND" and "pass" in statuses) or (
            key == "OR" and statuses and all(s == "pass" for s in statuses)
        ):
            return "pass"
        unknown |= "unknown" in statuses
    return "unknown" if unknown else "fail"
