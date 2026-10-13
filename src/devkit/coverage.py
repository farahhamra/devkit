"""Measured per-invocation coverage, separate from findings and display limits."""

from __future__ import annotations

from collections import defaultdict


class Coverage:
    def __init__(self):
        self.started: set[str] = set()
        self.finished: set[str] = set()
        self.items: dict[str, set] = defaultdict(set)
        self.gaps: dict[str, set] = defaultdict(set)
        self.exclusions: dict[str, set] = defaultdict(set)
        self.affected: set[str] = set()
        self.details: dict = {}
        self.issues: list[dict] = []
        self.aborted = False
        self.required = {"discovery"}

    def start(self, stage):
        self.started.add(stage)

    def finish(self, stage):
        self.started.add(stage)
        self.finished.add(stage)

    def record(self, kind, path):
        self.items[kind].add(str(path))

    def exclude(self, reason, path):
        self.exclusions[reason].add(str(path))

    def gap(self, reason, path="", *, stage="discovery", line=None):
        identity = (str(path), line)
        if identity not in self.gaps[reason]:
            self.gaps[reason].add(identity)
            self.issues.append(
                {"reason": reason, "stage": stage, "path": str(path), **({"line": line} if line else {})}
            )
        self.affected.add(stage)

    def configure(self, project, args):
        if args.command == "search":
            self.required.add("text")
        if args.command in {"symbols", "analyze", "query", "trace", "impact"} or (
            args.command == "module" and args.action in {"impact", "check"}
        ):
            self.required.add("source")
        if args.command == "query":
            self.required.add("query")
        if args.command == "schema" or (args.command == "impact" and args.action in {"model", "field"}):
            self.required.add("schema")
        target = getattr(args, "path", ".")
        selected = project.resolve(target, False)
        if args.command == "analyze" and args.with_context:
            selected = project.root
        self.details["scope"] = {
            "app": project.app,
            "path": target,
            "target": getattr(args, "module", None)
            or getattr(args, "model", None)
            or getattr(args, "target", None),
            "manifest": str(project.config_path),
            "ignore_file": str(project.ignore_file),
            "filters": {
                k: getattr(args, k)
                for k in (
                    "glob",
                    "extension",
                    "type",
                    "depth",
                    "dirs_only",
                    "scope",
                    "related",
                    "kind",
                    "name",
                    "exported_only",
                    "direction",
                    "include_shared",
                    "method",
                    "full",
                    "symbol",
                    "with_context",
                    "lang",
                    "timeout",
                )
                if hasattr(args, k)
            },
        }
        if args.command == "analyze":
            self.details["scope"]["context_scan"] = "selected_app" if args.with_context else "not_requested"
        apps = []
        for name, entry in project.config["apps"].items():
            active = project.app in {"all", name}
            roots = []
            for raw in entry.get("roots", []):
                path = project.resolve(raw, False)
                try:
                    state = "available" if path.is_dir() else "missing_or_not_directory"
                    if state == "available" and any(
                        not project.allowed(p, p.is_dir())
                        for p in (path, *path.parents)
                        if p != project.root and p.is_relative_to(project.root)
                    ):
                        state = "excluded"
                except OSError:
                    state = "inaccessible"
                relevant = active and (path.is_relative_to(selected) or selected.is_relative_to(path))
                roots.append({"path": raw, "state": state, "in_requested_scope": relevant})
                if relevant and state != "available":
                    self.gap("configured_root_" + state, raw)
            apps.append(
                {
                    "app": name,
                    "selected": active,
                    "configured": bool(entry.get("roots") or entry.get("schemas")),
                    "roots": roots,
                }
            )
        self.details["apps"] = apps
        self.details["limits"] = [
            "Coverage measures processing of selected eligible inputs, not runtime correctness or absence of bugs.",
            "Excluded directories are pruned: exclusion counts are encountered entries, not hidden descendant totals. No repository-wide percentage is inferred.",
            "Source analysis supports JS/TS/JSX/TSX, not arbitrary languages, type checking or runtime dispatch. Schema parsing is not Prisma validation.",
            "Scope/filters, relation depth/shared-node stops and static dependency depth limits still apply even when processing is complete.",
        ]

    def report(self, output_truncated=False):
        stages = {}
        query_stages = ("query",) if "query" in self.required or "query" in self.started else ()
        for name in ("discovery", "text", "source", "schema", "dependencies", *query_stages):
            stages[name] = (
                "not_run"
                if name not in self.started
                else "partial"
                if name in self.affected or name not in self.finished
                else "not_analyzed"
                if name == "source" and not self.items["source_analyzed"]
                else "not_analyzed"
                if name == "schema" and not self.items["schema_analyzed"]
                else "not_analyzed"
                if name == "text" and not self.items["text_read"]
                else "not_analyzed"
                if name == "query" and not self.items["query_analyzed"]
                else "complete"
            )
        status = (
            "not_analyzed"
            if not self.started
            else "partial"
            if self.aborted or self.gaps or "partial" in stages.values()
            else "not_analyzed"
            if any(stages[s] == "not_analyzed" for s in self.required)
            else "complete"
        )
        return {
            "version": "1.0",
            "status": status,
            "stages": stages,
            "counts": {
                key: len(self.items[key])
                for key in (
                    "files",
                    "directories",
                    "text_read",
                    "text_skipped",
                    "source_selected",
                    "source_analyzed",
                    "source_partial",
                    "schema_selected",
                    "schema_analyzed",
                    "modules_checked",
                    "modules_unavailable",
                    *(("query_selected", "query_analyzed") if query_stages else ()),
                )
            },
            "gaps": {k: len(v) for k, v in sorted(self.gaps.items())},
            "exclusions": {k: len(v) for k, v in sorted(self.exclusions.items())},
            "output_truncated": output_truncated,
            "details_truncated": False,
            "details": {
                **self.details,
                "issues": self.issues.copy(),
                "cache": {k: len(self.items[k]) for k in ("source_parsed", "source_cached")},
            },
        }


def coverage_line(coverage):
    stages = ", ".join(f"{key}={value}" for key, value in coverage["stages"].items())
    counts = coverage["counts"]
    gaps = ", ".join(f"{key}={value}" for key, value in coverage["gaps"].items()) or "none"
    return (
        f"Coverage: {coverage['status']} (selected inputs, not runtime proof); {stages}. "
        f"Files={counts['files']}, text read/skipped={counts['text_read']}/{counts['text_skipped']}, "
        f"source analyzed/selected={counts['source_analyzed']}/{counts['source_selected']}, "
        f"schemas analyzed/selected={counts['schema_analyzed']}/{counts['schema_selected']}. "
        f"Gaps: {gaps}. Output truncated={str(coverage['output_truncated']).lower()}. "
        "Use --json for scope, exclusions and details."
    )
