"""Offline performance analysis of explicitly supplied evidence."""


def performance(project, args):
    coverage = project.coverage
    coverage.start("performance")
    if args.action == "compare":
        from .compare import compare

        result = compare(project, args)
    elif args.action == "database":
        from .capture import load
        from .database import DatabaseAnalysis

        capture, path = load(project, args.input)
        coverage.details["performance_capture"] = {
            "path": path,
            "app": capture["metadata"]["app"],
            "version": capture["capture_version"],
        }
        result = DatabaseAnalysis(project, capture).run()
        result["input"] = path
    else:
        from . import cache, memory, runtime
        from .evidence import load

        kind, capture, path = load(project, args.input, args.action)
        coverage.details["performance_capture"] = {
            "path": path,
            "app": (capture.get("metadata") or {}).get("app"),
            "version": capture["capture_version"],
            "kind": kind,
        }
        result = {"runtime": runtime, "memory": memory, "cache": cache}[kind].analyze(project, capture)
        result["input"] = path
    coverage.finish("performance")
    result["summary"].update(complete=coverage.report()["status"] == "complete", strict=args.strict)
    failed = args.strict and not result["summary"]["complete"]
    if args.action == "compare" and args.fail_on_regression:
        failed |= bool(result["summary"]["regression_candidates"])
    return result, int(failed)
