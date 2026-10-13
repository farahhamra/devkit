"""Strict, local, declarative audit contracts. No guessed guard or tenant identities."""

from pathlib import PurePosixPath

import yaml

from ..project import DevkitError


class Loader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(keys) != len(set(keys)):
            raise DevkitError("Audit policy keys must be unique strings")
        return super().construct_mapping(node, deep=deep)


def mapping(value, keys, required=()):
    if not isinstance(value, dict) or set(value) - set(keys) or set(required) - set(value):
        raise DevkitError(
            f"Invalid audit policy object; allowed: {', '.join(keys)}; required: {', '.join(required)}"
        )
    return value


def strings(value):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise DevkitError("Audit policy expects a list of nonempty strings")
    if len(set(value)) != len(value):
        raise DevkitError("Audit policy lists must not contain duplicates")
    return value


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise DevkitError("Audit policy paths must be project-relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise DevkitError("Audit policy paths must be normalized project-relative paths")
    return value


def rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise DevkitError("Audit policy expects a list of objects")
    return value


class Policy:
    def __init__(self, project, path=None):
        target = project.resolve(path or project.ignore_file.parent / "audit.yaml", False)
        self.path = project.rel(target)
        self.present = target.exists()
        self.data = {}
        if not self.present:
            if path:
                raise DevkitError(f"Audit policy does not exist: {self.path}")
            return
        if not target.is_file() or target.stat().st_size > 2 * 1024 * 1024:
            raise DevkitError("Audit policy must be a regular file smaller than 2 MiB")
        try:
            self.data = yaml.load(target.read_text(encoding="utf-8-sig"), Loader=Loader)
        except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
            raise DevkitError("Invalid YAML or encoding in audit policy") from exc
        try:
            self.validate()
        except (TypeError, KeyError, RecursionError) as exc:
            raise DevkitError("Invalid audit policy value type or recursive structure") from exc

    def validate(self):
        mapping(
            self.data, ("version", "entrypoints", "guards", "routes", "isolation", "unused"), ("version",)
        )
        if type(self.data["version"]) is not int or self.data["version"] != 1:
            raise DevkitError("Supported audit policy version: 1")
        for entry in rows(self.data.get("entrypoints", [])):
            mapping(entry, ("path", "router"), ("path", "router"))
            relative(entry["path"])
            strings([entry["router"]])
        guards = mapping(self.data.get("guards", {}), ("authentication", "permissions", "roles"))
        for kind, entries in guards.items():
            for guard in rows(entries):
                mapping(guard, ("path", "symbol", "argument", "mode"), ("path", "symbol"))
                relative(guard["path"])
                strings([guard["symbol"]])
                if kind == "authentication" and ("argument" in guard or "mode" in guard):
                    raise DevkitError("Authentication guards are direct middleware identifiers")
                if kind != "authentication":
                    if type(guard.get("argument", 0)) is not int or not 0 <= guard.get("argument", 0) <= 10:
                        raise DevkitError("Guard argument must be an integer from 0 to 10")
                    if guard.get("mode", "all") not in {"all", "any"}:
                        raise DevkitError("Guard mode must be all or any")
        identities = set()
        for rule in rows(self.data.get("routes", [])):
            mapping(
                rule,
                (
                    "app",
                    "method",
                    "route",
                    "authentication",
                    "permissions",
                    "roles",
                    "permission_mode",
                    "role_mode",
                ),
                ("app", "method", "route", "authentication"),
            )
            if rule["app"] not in {"erp", "hub", "manager"} or rule["method"] not in {
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
                "ALL",
            }:
                raise DevkitError("Route policy requires a supported app and uppercase HTTP method")
            if (
                not isinstance(rule["route"], str)
                or not rule["route"].startswith("/")
                or "*" in rule["route"]
            ):
                raise DevkitError("Route policy must use an exact literal route, including parameter names")
            if rule["authentication"] not in {"required", "public"}:
                raise DevkitError("Route authentication must be required or public")
            for kind, mode in (("permissions", "permission_mode"), ("roles", "role_mode")):
                strings(rule.get(kind, []))
                if rule.get(mode, "all" if kind == "permissions" else "any") not in {"all", "any"}:
                    raise DevkitError("Route permission/role mode must be all or any")
                if rule["authentication"] == "public" and rule.get(kind):
                    raise DevkitError("Public route policies cannot require roles or permissions")
            key = (rule["app"], rule["method"], rule["route"])
            if key in identities:
                raise DevkitError("Duplicate route policy")
            identities.add(key)
        isolation = mapping(self.data.get("isolation", {}), ("models",))
        identities = set()
        for model in rows(isolation.get("models", [])):
            mapping(
                model, ("app", "model", "field", "receivers", "trusted_values", "global"), ("app", "model")
            )
            if model["app"] not in {"erp", "hub", "manager"}:
                raise DevkitError("Isolation policy requires a supported app")
            strings([model["model"]])
            if type(model.get("global", False)) is not bool:
                raise DevkitError("Isolation global flag must be a boolean")
            if not model.get("global", False):
                strings([model.get("field")])
                if not strings(model.get("receivers", [])) or not strings(model.get("trusted_values", [])):
                    raise DevkitError("Scoped isolation models require receivers and trusted_values")
            key = (model["app"], model["model"])
            if key in identities:
                raise DevkitError("Duplicate isolation model policy")
            identities.add(key)
        unused = mapping(self.data.get("unused", {}), ("entry",))
        for entry_path in strings(unused.get("entry", [])):
            relative(entry_path)

    def get(self, key, default=None):
        return self.data.get(key, default)
