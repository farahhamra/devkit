"""Opt-in static security and code-quality audits; never a runtime security verdict."""

from collections import Counter

LEVELS = {"none": 99, "unknown": -1, "info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def audit(project, args):
    from .policy import Policy

    coverage = project.coverage
    coverage.start("audit")
    policy = Policy(project, args.policy) if args.action in {"routes", "isolation", "unused"} else None
    if args.action in {"routes", "isolation"}:
        from .static import StaticAudit

        result = getattr(StaticAudit(project, args, policy), args.action)()
    else:
        from .engines import external_audit

        result = external_audit(project, args, policy)
    result.update(audit=args.action)
    if policy is not None:
        result["policy"] = {"path": policy.path, "configured": policy.present, "version": 1}
    checks = result.setdefault("checks", [])
    findings = result.setdefault("findings", [])
    for row in checks:
        if row["status"] == "unknown":
            coverage.gap("audit_unknown", row.get("path", ""), stage="audit", line=row.get("line"))
    counts = Counter(row["status"] for row in checks)
    severities = Counter(row["severity"] for row in findings)
    gated = sum(LEVELS[row["severity"]] >= LEVELS[args.fail_on] for row in findings)
    result["summary"] = {
        **{key: counts[key] for key in ("pass", "fail", "unknown", "not_applicable")},
        "findings": len(findings),
        "severity": {key: severities[key] for key in LEVELS if key != "none"},
        "fail_on": args.fail_on,
        "gated_findings": gated,
    }
    coverage.finish("audit")
    result["summary"]["complete"] = coverage.report()["status"] == "complete"
    result["summary"]["strict"] = args.strict
    result.setdefault("limitations", []).append(
        "Static evidence and scanner candidates require review and runtime tests. "
        "Exit 0 means no gate was triggered, not proof that the application is secure or code is safe to remove."
    )
    return result, int(bool(counts["fail"] or gated or (args.strict and not result["summary"]["complete"])))


def check(rule, status, message, location=None, **details):
    return {"rule": rule, "status": status, "message": message, **(location or {}), **details}


def finding(rule, severity, message, location=None, **details):
    return {
        "rule": rule,
        "severity": severity,
        "message": message,
        "confidence": "candidate",
        **(location or {}),
        **details,
    }
