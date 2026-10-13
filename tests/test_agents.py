from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from devkit import __version__
from devkit.agents import (
    BEGIN,
    END,
    IGNORE,
    INTERNAL,
    LEGACY_BODY,
    LEGACY_TITLE,
    STATE,
    TARGETS,
    AgentSync,
    templates,
)
from devkit.cli import main
from devkit.project import Project
from devkit.source import SourceIndex


def invoke(root, capsys, *args, status=0):
    code = main(["agents", "sync", "--root", str(root), *args, "--json"])
    captured = capsys.readouterr()
    assert code == status, captured.out + captured.err
    assert not captured.err
    return json.loads(captured.out)


def snapshot(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def upgrade(monkeypatch):
    newer = {name: text + "\nUpdated template guidance.\n" for name, text in templates().items()}
    monkeypatch.setattr("devkit.agents.templates", lambda: newer.copy())
    return newer


def test_preview_check_and_help_never_initialize_files(tmp_path, capsys):
    preview = invoke(tmp_path, capsys, "--dry-run")["data"]
    assert preview["mode"] == "dry-run" and not preview["up_to_date"]
    assert {f["action"] for f in preview["files"]} == {"create"}
    assert all("diff" in f for f in preview["files"])
    invoke(tmp_path, capsys, "--check", status=1)
    assert main(["agents", "--root", str(tmp_path), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["data"]) == 2
    assert main(["help", "agents", "sync", "--json"]) == 0
    assert "drift" in json.loads(capsys.readouterr().out)["data"][0]["exit_codes"]["1"]
    assert not list(tmp_path.iterdir())


def test_install_and_idempotence(tmp_path, capsys):
    result = invoke(tmp_path, capsys)["data"]
    assert result["applied"] and result["up_to_date"] and result["backup"] is None
    assert result["template_version"] == __version__
    for relative, template in templates().items():
        actual = (tmp_path / relative).read_text(encoding="utf-8")
        assert template in actual
    state = json.loads((tmp_path / STATE).read_bytes())
    assert state["version"] == 1 and set(state["files"]) == set(TARGETS)
    before = snapshot(tmp_path)
    assert invoke(tmp_path, capsys)["data"]["up_to_date"]
    assert invoke(tmp_path, capsys, "--check")["data"]["up_to_date"]
    invoke(tmp_path, capsys, "--dry-run")
    assert snapshot(tmp_path) == before
    assert not (tmp_path / INTERNAL / "sync.lock").exists()


@pytest.mark.parametrize("ending,bom", [("\n", b""), ("\r\n", b"\xef\xbb\xbf")])
def test_legacy_migration_preserves_general_directives_exactly(tmp_path, capsys, ending, bom):
    prefix = "# General instructions\n\n1. Keep unrelated instructions exactly as written.\n\n"
    legacy = "6. " + LEGACY_TITLE + "\n" + LEGACY_BODY
    suffix = "\n\n## Other operations\n\n```powershell\npython memory.py sync\n```\nUnicode: café\n"
    original = bom + (prefix + legacy + suffix).replace("\n", ending).encode()
    path = write(tmp_path, "AGENTS.md", original)
    # Full migration diffs can exceed the normal display budget as templates grow.
    preview_result = invoke(tmp_path, capsys, "--dry-run", "--max-output-chars", "100000")
    assert not preview_result["truncated"]
    preview = preview_result["data"]
    assert preview["files"][2]["action"] == "migrate"
    assert "python memory.py" not in preview["files"][2]["diff"]
    assert path.read_bytes() == original
    result = invoke(tmp_path, capsys)["data"]
    actual = path.read_bytes()
    assert actual.startswith(bom + prefix.replace("\n", ending).encode())
    assert actual.endswith(suffix.replace("\n", ending).encode())
    assert actual.count(BEGIN.encode()) == actual.count(END.encode()) == 1
    assert (tmp_path / result["backup"] / "AGENTS.md").read_bytes() == original
    invoke(tmp_path, capsys, "--check")


def test_append_to_general_agents_without_changing_original_bytes(tmp_path, capsys):
    original = b"\xef\xbb\xbf# General rules\r\nKeep every byte."
    path = write(tmp_path, "AGENTS.md", original)
    result = invoke(tmp_path, capsys)["data"]
    assert path.read_bytes().startswith(original + b"\r\n\r\n" + BEGIN.encode())
    assert (tmp_path / result["backup"] / "AGENTS.md").read_bytes() == original


def test_unmanaged_dedicated_files_require_explicit_force(tmp_path, capsys):
    rule = write(tmp_path, ".agents/rules/devkit-rules.md", "# My old rules\n")
    skill = write(tmp_path, ".agents/skills/devkit/SKILL.md", "# My old skill\n")
    before = snapshot(tmp_path)
    result = invoke(tmp_path, capsys, status=2)["data"]
    assert result["conflicts"] and not result["applied"]
    assert snapshot(tmp_path) == before
    invoke(tmp_path, capsys, "--dry-run", status=2)
    assert snapshot(tmp_path) == before
    invoke(tmp_path, capsys, "--dry-run", "--force")
    assert snapshot(tmp_path) == before
    result = invoke(tmp_path, capsys, "--force")["data"]
    assert (tmp_path / result["backup"] / rule.relative_to(tmp_path)).read_text() == "# My old rules\n"
    assert (tmp_path / result["backup"] / skill.relative_to(tmp_path)).read_text() == "# My old skill\n"


def test_upgrade_preserves_outside_block_edits_and_backs_up_all_replacements(tmp_path, capsys, monkeypatch):
    invoke(tmp_path, capsys)
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"# User general rules\n\n" + path.read_bytes() + b"\nKeep this appendix.\n")
    before = snapshot(tmp_path)
    upgrade(monkeypatch)
    invoke(tmp_path, capsys, "--check", status=1)
    assert snapshot(tmp_path) == before
    result = invoke(tmp_path, capsys)["data"]
    assert {f["action"] for f in result["files"]} == {"update"}
    assert path.read_bytes().startswith(b"# User general rules\n\n")
    assert path.read_bytes().endswith(b"\nKeep this appendix.\n")
    for name in TARGETS:
        assert (tmp_path / result["backup"] / name).read_bytes() == before[name][0]
    assert (tmp_path / result["backup"] / "previous-state.json").read_bytes() == before[STATE][0]
    invoke(tmp_path, capsys, "--check")


@pytest.mark.parametrize("relative", list(TARGETS))
def test_local_edits_block_entire_upgrade_unless_forced(tmp_path, capsys, monkeypatch, relative):
    invoke(tmp_path, capsys)
    path = tmp_path / relative
    original = path.read_bytes()
    edited = (
        original.replace(BEGIN.encode(), (BEGIN + "\nLocal Devkit edit.").encode())
        if relative == "AGENTS.md"
        else original + b"\nLocal edit.\n"
    )
    path.write_bytes(edited)
    before = snapshot(tmp_path)
    upgrade(monkeypatch)
    invoke(tmp_path, capsys, "--check", status=1)
    invoke(tmp_path, capsys, status=2)
    assert snapshot(tmp_path) == before
    result = invoke(tmp_path, capsys, "--force")["data"]
    assert (tmp_path / result["backup"] / relative).read_bytes() == edited
    assert b"Local edit." not in path.read_bytes()
    invoke(tmp_path, capsys, "--check")


@pytest.mark.parametrize(
    "text",
    [
        "Use devkit for everything.\n",
        "# General rules\n```text\nUnclosed example\n",
        BEGIN + "\nMissing end\n",
        END + "\nWrong order\n" + BEGIN,
        BEGIN + "\n" + BEGIN + "\n" + END,
        BEGIN + "\nOne\n" + END + "\n" + BEGIN + "\nTwo\n" + END,
        "```markdown\n" + BEGIN + "\nExample only\n" + END + "\n```\n",
        "6. " + LEGACY_TITLE + "\n" + LEGACY_BODY + "\n   Keep this custom addition.\n",
        "6. " + LEGACY_TITLE + "\nCustom content\n",
        "```markdown\n6. " + LEGACY_TITLE + "\n" + LEGACY_BODY + "\n```\n",
    ],
)
def test_ambiguous_agents_is_never_replaced_even_with_force(tmp_path, capsys, text):
    write(tmp_path, "AGENTS.md", text)
    before = snapshot(tmp_path)
    result = invoke(tmp_path, capsys, "--force", status=2)["data"]
    assert result["files"][2]["action"] == "conflict"
    assert snapshot(tmp_path) == before
    invoke(tmp_path, capsys, "--check", status=1)


def test_untracked_marked_block_can_be_adopted_with_force(tmp_path, capsys):
    prefix, suffix = "# Other directives\n\n", "\n\n## Independent rules\nRetain this.\n"
    write(tmp_path, "AGENTS.md", prefix + BEGIN + "\nOld Devkit instructions\n" + END + suffix)
    invoke(tmp_path, capsys, status=2)
    invoke(tmp_path, capsys, "--force")
    result = (tmp_path / "AGENTS.md").read_text()
    assert result.startswith(prefix) and result.endswith(suffix)


def test_matching_templates_can_be_registered_without_force(tmp_path, capsys):
    for name, text in templates().items():
        write(
            tmp_path, name, BEGIN + "\n" + text.rstrip() + "\n" + END + "\n" if name == "AGENTS.md" else text
        )
    before = snapshot(tmp_path)
    result = invoke(tmp_path, capsys)["data"]
    assert all(f["action"] == "unchanged" for f in result["files"])
    for name in TARGETS:
        assert snapshot(tmp_path)[name] == before[name]


def test_line_ending_conversion_is_not_a_local_content_conflict(tmp_path, capsys, monkeypatch):
    invoke(tmp_path, capsys)
    for name in TARGETS:
        path = tmp_path / name
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))
    invoke(tmp_path, capsys, "--check")
    upgrade(monkeypatch)
    invoke(tmp_path, capsys)
    for name in TARGETS:
        data = (tmp_path / name).read_bytes()
        assert data.startswith(b"\xef\xbb\xbf") and b"\n" not in data.replace(b"\r\n", b"")


def test_sync_ignores_broken_yaml_and_app_roots(project, capsys, monkeypatch):
    (project / "devkit/electroerp.yaml").write_text("apps: [invalid", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("Sync must not index source or parse YAML")

    monkeypatch.setattr(Project, "__init__", forbidden)
    monkeypatch.setattr(SourceIndex, "__init__", forbidden)
    monkeypatch.setenv("DEVKIT_CONFIG_DIR", str(project / "not-created"))
    result = invoke(
        project, capsys, "--config", str(project / "missing.yaml"), "--app", "manager", "--no-cache"
    )
    assert result["app"] == "all" and result["warnings"]
    assert (project / "AGENTS.md").exists()
    assert not (project / "not-created").exists()
    assert not (project / "devkit/.ignore").exists()


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"[]", b'{"version": 999}', b'{"version": 1, "devkit_version": "0.4.0", "files": {}}'],
)
def test_corrupt_state_is_not_reset(tmp_path, capsys, payload):
    write(tmp_path, STATE, payload)
    before = snapshot(tmp_path)
    result = invoke(tmp_path, capsys, "--force", status=2)
    assert "state" in result["error"]["message"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "relative", [".agents", ".agents/rules", ".agents/rules/devkit-rules.md", "AGENTS.md", INTERNAL, STATE]
)
def test_symlink_paths_are_rejected_without_touching_destination(tmp_path, capsys, relative):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    is_file = relative.endswith((".md", ".json"))
    destination = write(outside, "target.txt", "Keep me") if is_file else outside
    try:
        target.symlink_to(destination, target_is_directory=not is_file)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink creation privilege")
        raise
    result = invoke(tmp_path, capsys, "--force", status=2)
    assert "symlink/junction" in result["error"]["message"]
    assert sorted(p.name for p in outside.iterdir()) == (["target.txt"] if is_file else [])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_non_regular_target_is_rejected(tmp_path, capsys):
    os.mkfifo(tmp_path / "AGENTS.md")
    result = invoke(tmp_path, capsys, "--force", status=2)
    assert "regular" in result["error"]["message"]


def test_invalid_utf8_and_oversized_files_are_rejected(tmp_path, capsys):
    write(tmp_path, "AGENTS.md", b"\xff")
    assert "UTF-8" in invoke(tmp_path, capsys, "--force", status=2)["error"]["message"]
    write(tmp_path, "AGENTS.md", b"x" * (2 * 1024 * 1024 + 1))
    assert "2 MiB" in invoke(tmp_path, capsys, "--force", status=2)["error"]["message"]


def test_existing_lock_is_not_removed_or_overridden(tmp_path, capsys):
    lock = write(tmp_path, INTERNAL + "/sync.lock", "another writer")
    result = invoke(tmp_path, capsys, status=2)
    assert "lock exists" in result["error"]["message"]
    assert lock.read_text() == "another writer" and not (tmp_path / "AGENTS.md").exists()


@pytest.mark.parametrize("relative", list(TARGETS))
def test_cleared_managed_content_requires_force(tmp_path, capsys, relative):
    invoke(tmp_path, capsys)
    write(tmp_path, relative, "# Keep general rules\n" if relative == "AGENTS.md" else "")
    before = snapshot(tmp_path)
    invoke(tmp_path, capsys, status=2)
    assert snapshot(tmp_path) == before
    invoke(tmp_path, capsys, "--force")
    if relative == "AGENTS.md":
        assert (tmp_path / relative).read_text().startswith("# Keep general rules\n")


def test_recovery_does_not_overwrite_concurrent_editor_changes(tmp_path, capsys, monkeypatch):
    invoke(tmp_path, capsys)
    upgrade(monkeypatch)
    replace = AgentSync.replace
    first, second = list(TARGETS)[:2]

    def fail_after_external_edit(self, relative, data, expected):
        if relative == second:
            write(tmp_path, first, "Concurrent editor content\n")
            raise PermissionError("second target failed")
        return replace(self, relative, data, expected)

    monkeypatch.setattr(AgentSync, "replace", fail_after_external_edit)
    result = invoke(tmp_path, capsys, status=2)
    assert "Manual recovery required" in result["error"]["message"]
    assert (tmp_path / first).read_text() == "Concurrent editor content\n"


def test_change_between_snapshot_and_replace_is_not_clobbered(tmp_path, capsys, monkeypatch):
    invoke(tmp_path, capsys)
    upgrade(monkeypatch)
    replace = AgentSync.replace
    first = next(iter(TARGETS))

    def change_first(self, relative, data, expected):
        if relative == first:
            write(tmp_path, first, "Concurrent change\n")
        return replace(self, relative, data, expected)

    monkeypatch.setattr(AgentSync, "replace", change_first)
    result = invoke(tmp_path, capsys, status=2)
    assert "File changed during agent sync" in result["error"]["message"]
    assert (tmp_path / first).read_text() == "Concurrent change\n"


def test_hardlinked_target_is_not_replaced(tmp_path, capsys):
    original = write(tmp_path, "original.md", "Retain this")
    try:
        os.link(original, tmp_path / "AGENTS.md")
    except OSError:
        pytest.skip("Hardlinks unavailable on this filesystem")
    result = invoke(tmp_path, capsys, "--force", status=2)
    assert "hardlinked" in result["error"]["message"]
    assert original.read_text() == "Retain this"


def test_custom_storage_ignore_is_preserved_and_blocks_write(tmp_path, capsys):
    write(tmp_path, INTERNAL + "/.gitignore", "# Customized\n")
    before = snapshot(tmp_path)
    invoke(tmp_path, capsys, status=2)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("installed,fail_at", [(False, 2), (True, 2), (True, 4)])
def test_write_failure_rolls_back_targets_and_keeps_backups(
    tmp_path, capsys, monkeypatch, installed, fail_at
):
    if installed:
        invoke(tmp_path, capsys)
        upgrade(monkeypatch)
    originals = {
        p: (tmp_path / p).read_bytes() if (tmp_path / p).exists() else None for p in [*TARGETS, STATE]
    }
    replace = AgentSync.replace
    calls = 0

    def fail_once(self, *args):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise PermissionError("simulated Windows sharing violation")
        return replace(self, *args)

    monkeypatch.setattr(AgentSync, "replace", fail_once)
    result = invoke(tmp_path, capsys, status=2)
    assert "Original targets restored" in result["error"]["message"]
    for name, original in originals.items():
        path = tmp_path / name
        assert (path.read_bytes() if path.exists() else None) == original
    if installed:
        assert list((tmp_path / INTERNAL / "backups").iterdir())
    assert not (tmp_path / INTERNAL / "sync.lock").exists()
    assert not list(tmp_path.rglob(".devkit-sync-*"))


def test_backup_failure_never_changes_targets(tmp_path, capsys, monkeypatch):
    invoke(tmp_path, capsys)
    before = snapshot(tmp_path)
    upgrade(monkeypatch)
    write_new = AgentSync.write_new

    def deny_backup(self, relative, data):
        if "/backups/" in relative:
            raise PermissionError("backup denied")
        return write_new(self, relative, data)

    monkeypatch.setattr(AgentSync, "write_new", deny_backup)
    invoke(tmp_path, capsys, status=2)
    assert snapshot(tmp_path) == before


def test_sync_internal_files_are_git_ignored_and_excluded_from_source(project, capsys):
    invoke(project, capsys)
    write(project, INTERNAL + "/backups/test/old-rules.md", "private-sync-backup-needle")
    assert (project / INTERNAL / ".gitignore").read_text() == IGNORE
    p = Project(project)
    assert not any(".devkit-sync" in path.parts for path in p.files())
    assert not p.entries(INTERNAL)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    assert (
        subprocess.run(
            ["git", "-C", str(project), "check-ignore", "--", STATE], check=False, capture_output=True
        ).returncode
        == 0
    )


def test_template_examples_and_frontmatter_are_current():
    source = templates()
    assert "trigger: always_on" in source[".agents/rules/devkit-rules.md"]
    assert "name: devkit" in source[".agents/skills/devkit/SKILL.md"]
    assert "feedback add" in source[".agents/skills/devkit/SKILL.md"]
    assert "--max-output-chars" in source[".agents/skills/devkit/SKILL.md"]
    assert "Autonomous Agent Directives" not in source["AGENTS.md"]
    for name, text in source.items():
        assert text == (Path(__file__).parents[1] / "docs/agents" / TARGETS[name]).read_text(encoding="utf-8")


def test_mutually_exclusive_modes_and_bounded_preview(tmp_path, capsys):
    invoke(tmp_path, capsys, "--dry-run", "--check", status=2)
    result = invoke(tmp_path, capsys, "--dry-run", "--max-output-chars", "2048")
    assert result["truncated"]
    assert not list(tmp_path.iterdir())
