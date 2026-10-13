"""Optional scanner adapters using temporary, ignore-vetted inputs and explicit settings."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sysconfig
import tempfile
from pathlib import Path

from ..project import SOURCE_EXTENSIONS, DevkitError
from ..query import Snapshot
from ..source import walk
from . import check, finding
from .static import parse, properties

ASSETS = Path(__file__).parent
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock"}
MAX_REPORT = 64 * 1024 * 1024


def search_path():
    return os.pathsep.join(
        filter(
            None,
            [os.environ.get("DEVKIT_AUDIT_BIN_DIRS"), sysconfig.get_path("scripts"), os.environ.get("PATH")],
        )
    )


def executable(name):
    found = shutil.which(name, path=search_path())
    if not found:
        raise DevkitError(
            f"Optional audit engine '{name}' is not installed/on PATH. See docs/guides/AUDIT.md; Devkit does not install tools automatically."
        )
    return str(Path(found).absolute())


def node_package(name, binary=None):
    script = Path(executable(binary or name)).resolve()
    candidates = [script.parent, *script.parents]
    for directory in candidates:
        for root in (directory, directory / "node_modules" / name, directory / name):
            manifest = root / "package.json"
            if not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("name") == name:
                return root, data
    raise DevkitError(
        f"Cannot resolve the installed {name} npm package from its CLI; install it in a dedicated global/tool prefix"
    )


def node_command(name):
    root, metadata = node_package(name)
    version = metadata.get("version", "")
    expected = {"knip": 6, "eslint": 10, "jscpd": 5}[name]
    if not re.match(rf"^{expected}\.\d+\.\d+", version):
        raise DevkitError(f"Unsupported {name} version; supported major: {expected}")
    binary = metadata["bin"]
    binary = binary if isinstance(binary, str) else binary[name]
    return [executable("node"), str(root / binary)], root, version


def environment(directory):
    # Preserve OS and proxy/certificate settings, never engine config, startup hooks or service tokens.
    names = {
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "XDG_CACHE_HOME",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    env.update(
        {
            "PATH": search_path(),
            "CI": "true",
            "NO_COLOR": "1",
            "SEMGREP_SEND_METRICS": "off",
            "SEMGREP_ENABLE_VERSION_CHECK": "0",
            "SEMGREP_SETTINGS_FILE": str(directory / "semgrep-settings.yml"),
            "SEMGREP_LOG_FILE": str(directory / "semgrep.log"),
            "SEMGREP_CACHE_PATH": str(directory / "semgrep-cache"),
            "JSCPD_NO_TIPS": "1",
        }
    )
    return env


def run(argv, cwd, timeout, *, accepted=(0,)):
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment(cwd),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
                    try:
                        subprocess.run(
                            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                            check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    if process.poll() is None:
                        process.kill()
                process.wait()
                raise DevkitError("Audit engine timed out; no clean verdict is available") from None
        except OSError as exc:
            raise DevkitError(f"Could not start audit engine: {type(exc).__name__}") from exc
        if stdout.tell() > MAX_REPORT or stderr.tell() > MAX_REPORT:
            raise DevkitError("Audit engine output exceeded 64 MiB; narrow the scan")
        if code not in accepted:
            # Raw stderr can contain source, credentials or full dependency URLs.
            raise DevkitError(
                f"Audit engine failed with exit code {code}; verify its installation/configuration and network if required. Raw engine output is withheld."
            )
        stdout.seek(0)
        try:
            return stdout.read().decode("utf-8"), code
        except UnicodeError as exc:
            raise DevkitError("Audit engine output is not UTF-8") from exc


def document(raw):
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise DevkitError("Malformed JSON from audit engine; no clean verdict is available") from exc
    if not isinstance(result, (dict, list)):
        raise DevkitError("Invalid audit engine JSON structure")
    return result


def version(binary, directory, timeout, pattern):
    raw, _ = run([binary, "--version"], directory, min(timeout, 15))
    match = re.search(pattern, raw)
    if not match:
        raise DevkitError("Unsupported audit engine version; see docs/guides/AUDIT.md for supported versions")
    return match.group(1)


class Inputs:
    def __init__(self, project, args, directory, *, manifests=False):
        self.project, self.args, self.directory = project, args, directory
        self.snapshots = {}
        self.checks, self.findings, self.engines = [], [], []
        for path in project.entries(args.path):
            eligible = (
                path.name in LOCKFILES if args.action == "dependencies" else path.suffix in SOURCE_EXTENSIONS
            )
            if manifests and path.name == "package.json":
                eligible = True
            if not eligible:
                project.coverage.exclude("audit_unsupported_file", project.rel(path))
                continue
            relative = project.rel(path)
            project.coverage.record("audit_selected", relative)
            content = project.text(path)
            if content is None:
                project.coverage.gap("audit_unreadable", relative, stage="audit")
                continue
            snapshot = Snapshot(relative, content.encode("utf-8"), "", len(self.snapshots))
            self.snapshots[relative] = snapshot
            destination = directory / "input" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.name == "package.json":
                try:
                    original = json.loads(content)
                    # No scripts, Knip config, tool hooks or executable project plugins.
                    content = json.dumps(
                        {
                            k: original[k]
                            for k in (
                                "name",
                                "version",
                                "type",
                                "dependencies",
                                "devDependencies",
                                "peerDependencies",
                                "optionalDependencies",
                                "exports",
                                "main",
                                "module",
                                "types",
                            )
                            if k in original
                        }
                    )
                except (ValueError, TypeError) as exc:
                    raise DevkitError(f"Invalid package.json: {relative}") from exc
            destination.write_text(content, encoding="utf-8", newline="")
        (directory / "input").mkdir(exist_ok=True)

    @property
    def root(self):
        return self.directory / "input"

    def path(self, raw):
        if not isinstance(raw, str):
            raise DevkitError("Engine reported an invalid path")
        path = Path(raw)
        if path.is_absolute():
            try:
                raw = path.relative_to(self.root).as_posix()
            except ValueError as exc:
                raise DevkitError("Engine returned a path outside vetted inputs") from exc
        else:
            raw = path.as_posix()
        if raw not in self.snapshots:
            raise DevkitError("Engine returned an unknown/unselected path")
        return raw

    def location(self, raw, start=None, end=None, *, lines=None, utf16=None):
        path = self.path(raw)
        snapshot = self.snapshots[path]
        if lines:
            first, last = lines
            if not (type(first) is int and type(last) is int and 1 <= first <= last <= len(snapshot.starts)):
                raise DevkitError("Engine returned invalid line bounds")
            start = snapshot.starts[first - 1]
            end = snapshot.starts[last] if last < len(snapshot.starts) else len(snapshot.content)
        elif utf16:

            def offset(line, column):
                if (
                    type(line) is not int
                    or type(column) is not int
                    or not 1 <= line <= len(snapshot.starts)
                    or column < 1
                ):
                    raise ValueError("invalid position")
                beginning = snapshot.starts[line - 1]
                limit = snapshot.starts[line] if line < len(snapshot.starts) else len(snapshot.content)
                value = snapshot.content[beginning:limit].decode("utf-8")
                encoded = value.encode("utf-16-le")
                if 2 * (column - 1) > len(encoded):
                    raise ValueError("invalid column")
                return beginning + len(encoded[: 2 * (column - 1)].decode("utf-16-le").encode("utf-8"))

            try:
                start, end = offset(*utf16[:2]), offset(*utf16[2:])
            except (ValueError, UnicodeError) as exc:
                raise DevkitError("Engine returned invalid source positions") from exc
        if start is None:
            start, end = 0, len(snapshot.content)
        if start == end == 0 and not snapshot.content:
            return {
                "path": path,
                "line": 1,
                "end_line": 1,
                "column": 1,
                "end_column": 1,
                "start_byte": 0,
                "end_byte": 0,
            }
        try:
            result = snapshot.location(
                {
                    "range": {"byteOffset": {"start": start, "end": end}},
                    "text": snapshot.content[start:end].decode("utf-8"),
                }
            )
            result.pop("text")
            return result
        except (ValueError, TypeError, UnicodeError) as exc:
            raise DevkitError("Engine returned invalid source offsets") from exc

    def analyzed(self, paths):
        for raw in paths:
            self.project.coverage.record("audit_analyzed", self.path(raw))

    def incomplete(self, rule, message, path=""):
        self.project.coverage.gap(rule, path, stage="audit")
        self.checks.append(check(rule, "unknown", message, {"path": path} if path else None))

    def result(self):
        return {
            "engines": self.engines,
            "checks": self.checks,
            "findings": self.findings,
            "limitations": [
                "Only Devkit-selected inputs are staged. Project scanner configs/plugins are not loaded; engine-specific suppressions may still apply where documented."
            ],
        }


def security(inputs):
    binary = executable("semgrep")
    engine_version = version(binary, inputs.directory, inputs.args.timeout, r"\b(1\.\d+\.\d+)\b")
    rules = ASSETS / "security.yaml"
    raw, _ = run(
        [
            binary,
            "scan",
            "--config",
            str(rules),
            "--json",
            "--metrics",
            "off",
            "--disable-version-check",
            "--disable-nosem",
            "--no-git-ignore",
            "--max-target-bytes",
            "0",
            "--jobs",
            "1",
            ".",
        ],
        inputs.root,
        inputs.args.timeout,
    )
    report = document(raw)
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("results"), list)
        or not isinstance(report.get("errors"), list)
        or not isinstance(report.get("paths", {}).get("scanned"), list)
    ):
        raise DevkitError("Unsupported Semgrep report structure")
    inputs.engines.append(
        {"name": "semgrep", "version": engine_version, "rules": "devkit-baseline-v1", "network": False}
    )
    scanned = {inputs.path(p) for p in report["paths"]["scanned"]}
    inputs.analyzed(scanned)
    for path in inputs.snapshots.keys() - scanned:
        inputs.incomplete("semgrep_skipped", "Semgrep did not scan this selected file", path)
    if report["errors"] or report.get("skipped_rules"):
        inputs.incomplete(
            "semgrep_partial", "Semgrep reported parse/rule errors or skipped rules; raw diagnostics withheld"
        )
    for row in report["results"]:
        try:
            location = inputs.location(row["path"], row["start"]["offset"], row["end"]["offset"])
            severity = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}[row["extra"]["severity"]]
            rule = row["check_id"].split(".")[-1]
            messages = {
                "dynamic-eval": "Dynamic evaluation candidate",
                "shell-command": "Shell execution candidate",
                "raw-sql": "Unsafe raw SQL candidate",
                "tls-disabled": "TLS certificate verification disabled",
                "jwt-unverified": "JWT decode without verification candidate",
                "hardcoded-secret": "Hardcoded credential candidate (value withheld)",
            }
            if rule not in messages:
                raise ValueError("unknown baseline rule")
        except (KeyError, TypeError, ValueError) as exc:
            raise DevkitError("Invalid Semgrep finding") from exc
        inputs.findings.append(finding("security_" + rule, severity, messages[rule], location))
    result = inputs.result()
    result["limitations"].append(
        "Bundled Semgrep Community rules cover selected risky constructs, not every vulnerability or inter-file data flow. Secrets are not echoed. No remote rule registry or cloud scan is used."
    )
    return result


def duplicates(inputs):
    command, _, engine_version = node_command("jscpd")
    config = inputs.directory / "jscpd.json"
    config.write_text("{}", encoding="utf-8")
    output = inputs.directory / "report"
    run(
        [
            *command,
            str(inputs.root),
            "--config",
            str(config),
            "--reporters",
            "json",
            "--output",
            str(output),
            "--min-lines",
            str(inputs.args.min_lines),
            "--min-tokens",
            str(inputs.args.min_tokens),
            "--format",
            "javascript,typescript,jsx,tsx",
            "--max-size",
            "1073741824",
            "--absolute",
            "--no-gitignore",
            "--silent",
            "--no-tips",
        ],
        inputs.directory,
        inputs.args.timeout,
    )
    report_path = output / "jscpd-report.json"
    if not report_path.is_file() or report_path.stat().st_size > MAX_REPORT:
        raise DevkitError("Missing or oversized jscpd report")
    report = document(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("duplicates"), list):
        raise DevkitError("Unsupported jscpd report structure")
    inputs.engines.append({"name": "jscpd", "version": engine_version, "network": False})
    count = report.get("statistics", {}).get("total", {}).get("sources")
    if type(count) is not int or count < 0:
        raise DevkitError("Invalid jscpd source count")
    if count == len(inputs.snapshots):
        inputs.analyzed(inputs.snapshots)
    else:
        inputs.incomplete(
            "jscpd_scan_count",
            "jscpd source count differs from selected inputs (including files below token thresholds)",
        )
    for row in report["duplicates"]:
        try:
            fragments = [
                inputs.location(row[key]["name"], lines=(row[key]["start"], row[key]["end"]))
                for key in ("firstFile", "secondFile")
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise DevkitError("Invalid jscpd clone") from exc
        inputs.findings.append(
            finding(
                "duplicate_code",
                "low",
                "Token clone candidate; review both occurrences",
                fragments[0],
                related=[fragments[1]],
                lines=row.get("lines"),
                tokens=row.get("tokens"),
            )
        )
    result = inputs.result()
    result["statistics"] = {
        "sources": count,
        "min_lines": inputs.args.min_lines,
        "min_tokens": inputs.args.min_tokens,
    }
    result["limitations"].append(
        "Reports token clones, not proof of equivalent behavior or permission to merge/delete functions. Semantic-model downloads are disabled."
    )
    return result


def dependency_location(inputs, path, package):
    snapshot = inputs.snapshots[path]
    if Path(path).name not in {"package-lock.json", "npm-shrinkwrap.json"}:
        return {"path": path, "location_status": "unavailable"}
    # Parse JSON as a parenthesized JS expression solely to recover exact lock-entry spans.
    content = b"(" + snapshot.content + b")"
    root = parse(content, "lock.js")
    if root.has_error:
        return {"path": path, "location_status": "unavailable"}
    matches = []
    for node in walk(root):
        if node.type != "pair":
            continue
        key = node.child_by_field_name("key")
        try:
            name = json.loads(key.text)
        except (ValueError, TypeError):
            continue
        value = node.child_by_field_name("value")
        props = properties(value)
        if props and "version" in props:
            try:
                locked = json.loads(props["version"].text)
            except (ValueError, TypeError):
                continue
            if (
                name == package["name"] or name.endswith("node_modules/" + package["name"])
            ) and locked == package["version"]:
                matches.append(inputs.location(path, node.start_byte - 1, node.end_byte - 1))
    if not matches:
        return {"path": path, "location_status": "unavailable"}
    return {**matches[0], "related": matches[1:]}


def vulnerability_severity(vulnerability, groups):
    value = vulnerability.get("database_specific", {}).get("severity", "").lower()
    if value in {"low", "moderate", "medium", "high", "critical"}:
        return "medium" if value == "moderate" else value
    for group in groups:
        if vulnerability.get("id") in group.get("ids", []):
            try:
                score = float(group["max_severity"])
                if 0 <= score <= 10:
                    return (
                        "critical"
                        if score >= 9
                        else "high"
                        if score >= 7
                        else "medium"
                        if score >= 4
                        else "low"
                    )
            except (KeyError, ValueError, TypeError):
                pass
    return "unknown"


def dependencies(inputs):
    binary = executable("osv-scanner")
    engine_version = version(binary, inputs.directory, inputs.args.timeout, r"version:\s*(2\.7\.\d+)\b")
    config = inputs.directory / "osv.toml"
    config.write_text("", encoding="utf-8")
    command = [
        binary,
        "scan",
        "source",
        "--config",
        str(config),
        "--format",
        "json",
        "--all-packages",
        "--all-vulns",
        "--no-resolve",
        "--no-call-analysis",
        "all",
    ]
    if inputs.args.offline:
        command += ["--offline", "--offline-vulnerabilities"]
    if inputs.args.offline_db:
        if not inputs.args.offline:
            raise DevkitError("--offline-db requires --offline")
        database = Path(inputs.args.offline_db).absolute()
        if not database.is_dir():
            raise DevkitError("OSV offline database directory does not exist")
        command += ["--local-db-path", str(database)]
    for path in inputs.snapshots:
        command += ["--lockfile", str(inputs.root / path)]
    raw, _ = run(command, inputs.directory, inputs.args.timeout, accepted=(0, 1))
    report = document(raw)
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        raise DevkitError("Unsupported OSV-Scanner report structure")
    inputs.engines.append(
        {"name": "osv-scanner", "version": engine_version, "network": not inputs.args.offline}
    )
    scanned = set()
    package_count = 0
    for group in report["results"]:
        try:
            path = inputs.path(group["source"]["path"])
            packages = group["packages"]
            if not isinstance(packages, list):
                raise TypeError("invalid packages")
            scanned.add(path)
            for item in packages:
                package = item["package"]
                if not all(isinstance(package.get(key), str) for key in ("name", "version", "ecosystem")):
                    raise ValueError("invalid package")
                package_count += 1
                if "source=LOCAL" in package.get("purl", "") or "source=UNKNOWN" in package.get("purl", ""):
                    inputs.incomplete(
                        "dependency_unscannable",
                        "A local/unknown-origin dependency cannot be checked against public advisories",
                        path,
                    )
                for vulnerability in item.get("vulnerabilities", []):
                    identity = vulnerability["id"]
                    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", identity):
                        raise ValueError("invalid advisory id")
                    severity = vulnerability_severity(vulnerability, item.get("groups", []))
                    location = dependency_location(inputs, path, package)
                    if location.get("location_status") == "unavailable":
                        inputs.incomplete(
                            "dependency_location",
                            "Exact affected package span unavailable for this lockfile format",
                            path,
                        )
                    if severity == "unknown":
                        inputs.incomplete(
                            "dependency_severity", "Advisory has no supported severity score", path
                        )
                    inputs.findings.append(
                        finding(
                            "vulnerable_dependency",
                            severity,
                            "Known vulnerability in locked dependency",
                            location,
                            advisory=identity,
                            package={k: package[k] for k in ("name", "version", "ecosystem")},
                        )
                    )
        except (KeyError, TypeError, ValueError) as exc:
            raise DevkitError("Invalid OSV-Scanner result") from exc
    inputs.analyzed(scanned)
    for path in inputs.snapshots.keys() - scanned:
        inputs.incomplete(
            "dependency_lockfile_unreported",
            "Scanner did not report packages for this selected lockfile",
            path,
        )
    result = inputs.result()
    result["statistics"] = {"packages": package_count, "lockfiles": len(scanned)}
    result["limitations"].append(
        "Known advisories depend on the OSV database and locked versions, not runtime reachability. Online mode sends package names/versions to advisory services. Offline mode requires a pre-populated OSV database."
    )
    return result


def unused(inputs, policy):
    from .unused import unused as scan_unused

    return scan_unused(inputs, policy)


def external_audit(project, args, policy):
    with tempfile.TemporaryDirectory(prefix="devkit-audit-") as directory:
        inputs = Inputs(project, args, Path(directory), manifests=args.action == "unused")
        if not inputs.snapshots:
            inputs.incomplete("audit_no_inputs", "No eligible readable files in the selected target")
            return inputs.result()
        try:
            return (
                unused(inputs, policy)
                if args.action == "unused"
                else {"security": security, "dependencies": dependencies, "duplicates": duplicates}[
                    args.action
                ](inputs)
            )
        except (KeyError, TypeError, IndexError) as exc:
            raise DevkitError("Unexpected audit engine report; no clean verdict is available") from exc
