"""Offline performance analysis of explicitly supplied evidence."""


def performance(project, args):
    from .capture import load
    from .database import DatabaseAnalysis

    coverage = project.coverage
    coverage.start("performance")
    capture, path = load(project, args.input)
    coverage.details["performance_capture"] = {
        "path": path,
        "app": capture["metadata"]["app"],
        "version": "1.0",
    }
    result = DatabaseAnalysis(project, capture).run()
    result["input"] = path
    coverage.finish("performance")
    result["summary"].update(complete=coverage.report()["status"] == "complete", strict=args.strict)
    return result, int(args.strict and not result["summary"]["complete"])
