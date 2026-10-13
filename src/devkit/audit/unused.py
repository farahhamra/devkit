"""Unused-code candidates from a controlled Knip graph and TypeScript-aware ESLint."""

import json
import re
from pathlib import Path, PurePosixPath

from ..project import SOURCE_EXTENSIONS, DevkitError
from ..source import text, walk
from . import finding
from .engines import ASSETS, document, executable, node_command, run
from .static import parse


def symbol_location(inputs, path, row):
    snapshot = inputs.snapshots[path]
    root = parse(snapshot.content, path)
    found = []
    for node in walk(root):
        name = node.child_by_field_name("name")
        if (
            name is not None
            and text(name) == row["name"]
            and node.type
            in {
                "function_declaration",
                "generator_function_declaration",
                "variable_declarator",
                "class_declaration",
                "interface_declaration",
                "type_alias_declaration",
                "enum_declaration",
            }
            and (row.get("line") is None or name.start_point.row + 1 == row["line"])
        ):
            found.append(inputs.location(path, node.start_byte, node.end_byte))
    if len(found) == 1:
        return found[0]
    inputs.incomplete("unused_location", "Exact unused symbol span could not be resolved", path)
    return {"path": path, "location_status": "unavailable"}


def dependency_location(inputs, path, name):
    # The original manifest, not the sanitized staged manifest, owns the line positions.
    content = b"(" + inputs.snapshots[path].content + b")"
    root = parse(content, "manifest.js")
    found = []
    for node in walk(root):
        if node.type == "pair":
            try:
                key = json.loads(node.child_by_field_name("key").text)
            except (ValueError, TypeError):
                continue
            if key == name:
                found.append(inputs.location(path, node.start_byte - 1, node.end_byte - 1))
    if len(found) == 1:
        return found[0]
    inputs.incomplete("unused_location", "Exact dependency declaration span unavailable", path)
    return {"path": path, "location_status": "unavailable"}


def unused(inputs, policy):
    sources = [p for p in inputs.snapshots if Path(p).suffix in SOURCE_EXTENSIONS]
    if not sources:
        inputs.incomplete("unused_no_sources", "No supported source files in target")
        return inputs.result()
    _, eslint_root, eslint_version = node_command("eslint")
    config = inputs.directory / "eslint-input.json"
    config.write_text(json.dumps({"root": str(inputs.root), "files": sources}), encoding="utf-8")
    raw, _ = run(
        [executable("node"), str(ASSETS / "unused.mjs"), str(eslint_root), str(config)],
        inputs.directory,
        inputs.args.timeout,
    )
    report = document(raw)
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        raise DevkitError("Unsupported ESLint bridge output")
    inputs.engines.append(
        {
            "name": "eslint",
            "version": eslint_version,
            "parser": report.get("parserVersion"),
            "plugin": report.get("pluginVersion"),
            "network": False,
        }
    )
    scanned = set()
    for result in report["results"]:
        path = inputs.path(result["filePath"])
        if path in scanned or path not in sources:
            raise DevkitError("Unexpected/duplicate ESLint source result")
        scanned.add(path)
        for message in result["messages"]:
            if message.get("fatal") or message.get("ruleId") != "@typescript-eslint/no-unused-vars":
                inputs.incomplete("eslint_partial", "ESLint could not analyze part of this file", path)
                continue
            location = inputs.location(
                path, utf16=(message["line"], message["column"], message["endLine"], message["endColumn"])
            )
            inputs.findings.append(
                finding("unused_local", "low", "Unused local declaration/import candidate", location)
            )
    inputs.analyzed(scanned)
    for path in set(sources) - scanned:
        inputs.incomplete("eslint_skipped", "ESLint did not report this selected source file", path)

    entries = inputs.args.entry or policy.get("unused", {}).get("entry", [])
    if not entries:
        inputs.incomplete(
            "knip_entrypoints",
            "Configure unused.entry or repeat --entry with explicit project-relative entry files to enable Knip graph analysis",
        )
        return inputs.result()
    if any(entry not in sources for entry in entries):
        raise DevkitError(
            "Every unused-code entrypoint must be an eligible source file inside the selected path/app (no globs)"
        )
    command, root, knip_version = node_command("knip")
    manifest = inputs.root / "package.json"
    if not manifest.exists():
        # Knip requires a package boundary even for a path-only source target.
        manifest.write_text('{"name":"devkit-audit-snapshot","private":true}', encoding="utf-8")
    raw_plugins, _ = run(
        [executable("node"), str(ASSETS / "knip-plugins.mjs"), str(root)],
        inputs.directory,
        min(inputs.args.timeout, 15),
    )
    plugins = document(raw_plugins)
    if not isinstance(plugins, list) or not plugins or any(not isinstance(p, str) for p in plugins):
        raise DevkitError("Cannot obtain Knip plugin catalog; refusing implicit project plugins")
    # Each selected package is its own graph boundary, including ERP backend/frontend.
    manifests = {
        str(PurePosixPath(p).parent): p
        for p in inputs.snapshots
        if p.endswith("/package.json") or p == "package.json"
    }
    groups = {}
    for source in sources:
        parents = [
            parent for parent in manifests if PurePosixPath(source).is_relative_to(PurePosixPath(parent))
        ]
        workspace = max(parents, key=lambda p: len(PurePosixPath(p).parts)) if parents else "."
        groups.setdefault(workspace, []).append(source)
    disabled = {name: False for name in plugins}
    workspaces = {}
    graph_manifests = []
    for workspace, files in groups.items():
        entry_files = [entry for entry in entries if entry in files]
        if not entry_files:
            inputs.incomplete(
                "knip_workspace_entries",
                "No entrypoint configured for this selected package; graph audit skipped",
                manifests.get(workspace, workspace),
            )
            workspaces[workspace] = {**disabled, "entry": [], "project": []}
            continue

        def exact_glob(path, workspace=workspace):
            relative = PurePosixPath(path).relative_to(PurePosixPath(workspace)).as_posix()
            return re.sub(r"([*?\[\]{}()!+@])", r"\\\1", relative)

        workspaces[workspace] = {
            **disabled,
            "entry": [exact_glob(p) for p in entry_files],
            "project": [exact_glob(p) for p in files],
        }
        if workspace in manifests:
            graph_manifests.append(manifests[workspace])
    knip_config = inputs.directory / "knip.json"
    knip_config.write_text(
        json.dumps({**disabled, "entry": [], "project": [], "workspaces": workspaces}),
        encoding="utf-8",
    )
    # Explicit empty TS config prevents discovery/extends of project-controlled config.
    tsconfig = inputs.directory / "tsconfig.json"
    tsconfig.write_text('{"compilerOptions":{"allowJs":true,"jsx":"preserve"}}', encoding="utf-8")
    raw, _ = run(
        [
            *command,
            "--config",
            str(knip_config),
            "--tsConfig",
            str(tsconfig),
            "--reporter",
            "json",
            "--no-progress",
            "--no-gitignore",
            "--include",
            "files,exports,types,dependencies,unresolved",
            "--no-config-hints",
        ],
        inputs.root,
        inputs.args.timeout,
        accepted=(0, 1),
    )
    report = document(raw)
    if not isinstance(report, dict) or not isinstance(report.get("issues"), list):
        raise DevkitError("Unsupported Knip report structure")
    inputs.engines.append({"name": "knip", "version": knip_version, "network": False, "entrypoints": entries})
    for issue in report["issues"]:
        path = inputs.path(issue["file"])
        for kind in ("files", "exports", "types", "dependencies", "devDependencies", "unresolved"):
            for row in issue.get(kind, []):
                if kind == "unresolved":
                    inputs.incomplete(
                        "knip_unresolved_import",
                        "Knip could not resolve an import; dependent unused candidates require review",
                        path,
                    )
                    continue
                if kind == "files":
                    location = inputs.location(path)
                elif kind in {"dependencies", "devDependencies"}:
                    location = dependency_location(inputs, path, row["name"])
                else:
                    location = symbol_location(inputs, path, row)
                inputs.findings.append(
                    finding(
                        "unused_" + kind,
                        "low",
                        "Unused graph candidate relative to configured entries",
                        location,
                        name=row.get("name"),
                    )
                )
    inputs.analyzed(graph_manifests)
    result = inputs.result()
    result["limitations"].extend(
        [
            "Knip uses explicit entries and selected files only; omitted apps, ignored files, runtime imports and external consumers can make candidates appear unused.",
            "Project plugins/config, package scripts and custom TS path aliases are not loaded. Dependency findings can include build/test tooling. Review before deleting anything.",
            "ESLint checks local bindings with TypeScript syntax support; it does not run the TypeScript compiler or project-specific type-aware rules. Unused parameters/catch bindings are not reported.",
        ]
    )
    return result
