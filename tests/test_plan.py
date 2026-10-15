"""Archive integration and data-loss regressions; all brain files are synthetic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from devkit.cli import main
from devkit.plan import ARCHIVE, INTERNAL, NAMES, PlanArchive, PlanContext

ID = "f00984c3-1234-5678-9012-abcdef123456"
DOCUMENTS = {
    "plan": b"\xef\xbb\xbf# Plan\r\n\r\nPreserve exact bytes.\r\n",
    "task": "# Tasks\n- [x] café 界\n".encode(),
}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "Electro ERP"
    root.mkdir()
    profile = tmp_path / "User profile"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: profile))
    for key, value in DOCUMENTS.items():
        write(profile / ".gemini/antigravity/brain" / ID / NAMES[key], value)
    return PlanArchive(PlanContext(root), ID)


def invoke(archive, capsys, *args, status=0, identifier=ID):
    actual = main(["plan", "archive", identifier, "--root", str(archive.root), "--json", *args])
    captured = capsys.readouterr()
    assert actual == status, captured.out + captured.err
    assert captured.err == ""
    data = json.loads(captured.out)
    assert "coverage" not in data
    assert "Preserve exact bytes" not in captured.out and "café" not in captured.out
    return data.get("data", data)


def sources(archive):
    return {key: (archive.source / name).read_bytes() for key, name in NAMES.items()}


def snapshot(path):
    return {
        str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in path.rglob("*")
        if p.is_file()
    }


def test_preview_help_no_storage_or_yaml(archive, capsys):
    before = snapshot(archive.root.parent)
    result = invoke(archive, capsys, "--dry-run", "--config", "missing.yaml", "--app", "hub", "--no-cache")
    assert result["status"] == "would_archive" and result["index"] == "001"
    assert all(row["source_status"] == "pending" and not row["verified"] for row in result["files"].values())
    assert main(["help", "plan", "archive", "--json"]) == 0
    help_row = json.loads(capsys.readouterr().out)["data"][0]
    assert "clears" in help_row["scope"] and "incomplete clearing" in help_row["exit_codes"]["2"]
    assert main(["plan", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["data"]) == 2
    assert snapshot(archive.root.parent) == before


def test_archive_exact_bytes_empty_in_place_and_repeat(archive, capsys):
    identities = {key: (archive.source / name).stat().st_ino for key, name in NAMES.items()}
    result = invoke(archive, capsys)
    assert result["status"] == "archived" and result["phase"] == "complete"
    assert sources(archive) == dict.fromkeys(NAMES, b"")
    for key, row in result["files"].items():
        assert (archive.root / row["archive"]).read_bytes() == DOCUMENTS[key]
        assert row["verified"] and row["source_status"] == "empty"
        assert (archive.source / NAMES[key]).stat().st_ino == identities[key]
    record = json.loads((archive.root / result["record"]).read_bytes())
    assert record["conversation_id"] == ID and record["phase"] == "complete"
    assert not list((archive.root / INTERNAL).glob("*.stage"))
    before = snapshot(archive.root.parent)
    assert invoke(archive, capsys)["status"] == "already_archived"
    assert invoke(archive, capsys, "--dry-run")["status"] == "already_archived"
    assert snapshot(archive.root.parent) == before


@pytest.mark.parametrize("prefix,expected", [("054", "055"), ("999", "1000"), ("7", "008")])
def test_shared_index_uses_both_folders_and_never_overwrites(archive, capsys, prefix, expected):
    old_plan = write(archive.root / ARCHIVE / "plans/002_custom_plan.md", b"Old plan")
    old_task = write(archive.root / ARCHIVE / f"tasks/{prefix}_custom_task.md", b"Old task")
    result = invoke(archive, capsys)
    assert result["index"] == expected
    assert old_plan.read_bytes() == b"Old plan" and old_task.read_bytes() == b"Old task"
    assert all(Path(row["archive"]).name.startswith(expected + "_") for row in result["files"].values())


def test_conversation_reuse_and_record_indexes_survive_removed_copies(archive, capsys):
    first = invoke(archive, capsys)
    for key, row in first["files"].items():
        (archive.root / row["archive"]).unlink()
        write(archive.source / NAMES[key], DOCUMENTS[key] + b"New work\n")
    result = invoke(archive, capsys)
    assert result["status"] == "archived" and result["index"] == "002"
    assert all(
        (archive.root / row["archive"]).read_bytes().endswith(b"New work\n")
        for row in result["files"].values()
    )


def test_local_git_exclusions_preserve_existing_rules(archive, capsys):
    subprocess.run(["git", "init", "-q", str(archive.root)], check=True)
    ignore = write(archive.root / ARCHIVE / ".gitignore", b"# Custom exclusions\r\n*.tmp\r\n!plans/\r\n")
    before = ignore.read_bytes()
    result = invoke(archive, capsys)
    assert ignore.read_bytes().startswith(before)
    paths = [row["archive"] for row in result["files"].values()] + [result["record"]]
    check = subprocess.run(
        ["git", "check-ignore", *paths], cwd=archive.root, text=True, capture_output=True, check=False
    )
    assert check.returncode == 0 and set(check.stdout.splitlines()) == set(paths)
    assert not (archive.root / "devkit").exists()


@pytest.mark.parametrize(
    "value", ["../other", "..", "a/b", "a\\b", "C:\\Users\\x", "id:stream", "NUL", "CON", "a" * 129]
)
def test_invalid_ids_never_touch_sources(archive, capsys, value):
    before = snapshot(archive.root.parent)
    invoke(archive, capsys, status=2, identifier=value)
    assert snapshot(archive.root.parent) == before


@pytest.mark.parametrize("value", [None, b"", b" \r\n", b"\xef\xbb\xbf \n", b"\xff"])
def test_missing_empty_or_invalid_sources_leave_other_file_intact(archive, capsys, value):
    task = archive.source / "task.md"
    task.unlink() if value is None else task.write_bytes(value)
    before = snapshot(archive.root.parent)
    invoke(archive, capsys, status=2)
    assert snapshot(archive.root.parent) == before


def test_copy_failure_resumes_same_index_before_any_clearing(archive, capsys, monkeypatch):
    copy = PlanArchive.copy

    def fail_second(self, kind, data):
        if kind == "task":
            raise OSError("Synthetic disk write failure")
        return copy(self, kind, data)

    with monkeypatch.context() as patch:
        patch.setattr(PlanArchive, "copy", fail_second)
        result = invoke(archive, capsys, status=2)
    assert result["phase"] == "copying" and sources(archive) == DOCUMENTS
    assert result["files"]["plan"]["verified"] and not result["files"]["task"]["verified"]
    preview = invoke(archive, capsys, "--dry-run")
    assert preview["status"] == "would_resume" and preview["index"] == "001"
    result = invoke(archive, capsys)
    assert result["status"] == "resumed" and result["index"] == "001"
    assert sources(archive) == dict.fromkeys(NAMES, b"")


def test_partial_clearing_recovery_and_new_content_preservation(archive, capsys, monkeypatch):
    clear = PlanArchive.clear

    def fail_task(self, kind):
        if kind == "task":
            raise PermissionError("Synthetic Windows sharing violation")
        clear(self, kind)

    with monkeypatch.context() as patch:
        patch.setattr(PlanArchive, "clear", fail_task)
        failed = invoke(archive, capsys, status=2)
    assert failed["phase"] == "clearing"
    assert failed["files"]["plan"]["source_status"] == "empty"
    assert failed["files"]["task"]["source_status"] == "pending"
    assert all(row["verified"] for row in failed["files"].values())
    write(archive.source / "task.md", b"New task must survive")
    changed = invoke(archive, capsys, status=2)
    assert changed["files"]["task"]["source_status"] == "changed"
    assert (archive.source / "task.md").read_bytes() == b"New task must survive"
    write(archive.source / "task.md", DOCUMENTS["task"])
    assert invoke(archive, capsys)["status"] == "resumed"


@pytest.mark.parametrize("corrupt", [False, True])
def test_interrupted_staging_is_validated_before_resume(archive, capsys, monkeypatch, corrupt):
    def partial_copy(self, kind, data):
        write(self.stage(kind), b"Unexpected bytes" if corrupt else data[:5])
        raise OSError("Interrupted staging")

    with monkeypatch.context() as patch:
        patch.setattr(PlanArchive, "copy", partial_copy)
        invoke(archive, capsys, status=2)
    assert sources(archive) == DOCUMENTS
    before = snapshot(archive.root.parent)
    invoke(archive, capsys, "--dry-run", status=2 if corrupt else 0)
    assert snapshot(archive.root.parent) == before
    result = invoke(archive, capsys, status=2 if corrupt else 0)
    assert result["index"] == "001"
    assert sources(archive) == (DOCUMENTS if corrupt else dict.fromkeys(NAMES, b""))


def test_failed_completion_record_can_resume_empty_originals(archive, capsys, monkeypatch):
    save = PlanArchive.save_record

    def fail_complete(self):
        if self.record["phase"] == "complete":
            raise OSError("Synthetic journal failure")
        return save(self)

    with monkeypatch.context() as patch:
        patch.setattr(PlanArchive, "save_record", fail_complete)
        invoke(archive, capsys, status=2)
    assert sources(archive) == dict.fromkeys(NAMES, b"")
    assert invoke(archive, capsys)["status"] == "resumed"


def test_source_change_between_copy_and_clear_preserves_both_originals(archive, capsys, monkeypatch):
    copy = PlanArchive.copy

    def edit_source(self, kind, data):
        copy(self, kind, data)
        if kind == "task":
            (self.source / "task.md").write_bytes(b"Actively edited task")

    monkeypatch.setattr(PlanArchive, "copy", edit_source)
    result = invoke(archive, capsys, status=2)
    assert all(row["verified"] for row in result["files"].values())
    assert sources(archive) == {"plan": DOCUMENTS["plan"], "task": b"Actively edited task"}


def test_source_change_immediately_before_truncate_is_rejected(archive, capsys, monkeypatch):
    clear = PlanArchive.clear

    def edit_before_clear(self, kind):
        if kind == "plan":
            (self.source / "implementation_plan.md").write_bytes(b"New plan")
        return clear(self, kind)

    monkeypatch.setattr(PlanArchive, "clear", edit_before_clear)
    invoke(archive, capsys, status=2)
    assert sources(archive) == {"plan": b"New plan", "task": DOCUMENTS["task"]}


def test_destination_collision_and_corruption_never_overwritten(archive, capsys, monkeypatch):
    copy = PlanArchive.copy

    def conflict(self, kind, data):
        if kind == "task":
            write(self.destination(kind), b"Preexisting content")
        return copy(self, kind, data)

    with monkeypatch.context() as patch:
        patch.setattr(PlanArchive, "copy", conflict)
        result = invoke(archive, capsys, status=2)
    assert sources(archive) == DOCUMENTS
    assert (archive.root / result["files"]["task"]["archive"]).read_bytes() == b"Preexisting content"
    invoke(archive, capsys, "--dry-run", status=2)
    invoke(archive, capsys, status=2)
    assert sources(archive) == DOCUMENTS


def test_archive_corruption_prevents_recovery_clearing(archive, capsys, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(PlanArchive, "clear", lambda *_: (_ for _ in ()).throw(OSError("Cannot clear")))
        result = invoke(archive, capsys, status=2)
    write(archive.root / result["files"]["plan"]["archive"], b"Damaged archive")
    invoke(archive, capsys, status=2)
    assert sources(archive) == DOCUMENTS


def test_interrupted_exclusive_publication_recovers_temporary_link(archive, capsys, monkeypatch):
    unlink = Path.unlink

    def fail_stage(path, *args, **kwargs):
        if path.name == "001_implementation_plan.md.stage":
            raise OSError("Interrupted after link publication")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_stage)
        result = invoke(archive, capsys, status=2)
    assert sources(archive) == DOCUMENTS
    assert (archive.root / result["files"]["plan"]["archive"]).stat().st_nlink == 2
    assert invoke(archive, capsys)["status"] == "resumed"
    assert not list((archive.root / INTERNAL).glob("*.stage"))


def test_existing_lock_prevents_a_second_process_from_clearing(archive, capsys):
    archive.ignore(apply=True)
    with archive.locked():
        environment = {**os.environ, "HOME": str(Path.home()), "USERPROFILE": str(Path.home())}
        process = subprocess.run(
            [sys.executable, "-m", "devkit", "plan", "archive", ID, "--root", str(archive.root), "--json"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert process.returncode == 2 and "lock exists" in process.stdout
        assert sources(archive) == DOCUMENTS
    assert invoke(archive, capsys)["index"] == "001"


@pytest.mark.parametrize("where", ["source", "destination", "metadata"])
def test_symlinks_rejected_without_touching_their_targets(archive, capsys, tmp_path, where):
    target = write(tmp_path / "unrelated.md", b"Keep me")
    if where == "source":
        link = archive.source / "task.md"
        link.unlink()
    elif where == "destination":
        target = tmp_path / "unrelated-directory"
        target.mkdir()
        link = archive.root / ARCHIVE / "plans"
    else:
        target = tmp_path / "unrelated-metadata"
        target.mkdir()
        link = archive.root / INTERNAL
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"Symlink privilege unavailable: {exc}")
    before = snapshot(tmp_path)
    invoke(archive, capsys, status=2)
    assert snapshot(tmp_path) == before


def test_source_hardlink_rejected(archive, capsys, tmp_path):
    source = archive.source / "task.md"
    os.link(source, tmp_path / "other-task.md")
    invoke(archive, capsys, status=2)
    assert sources(archive) == DOCUMENTS


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_windows_junction_source_rejected(archive, capsys, tmp_path):
    original = archive.source
    moved = original.with_name("moved-conversation")
    original.rename(moved)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(original), str(moved)], check=True, capture_output=True)
    invoke(archive, capsys, status=2)
    assert (moved / "task.md").read_bytes() == DOCUMENTS["task"]


@pytest.mark.parametrize("raw", [b"{}", b"[]", b"not json"])
def test_corrupt_metadata_fails_without_source_changes(archive, capsys, raw):
    write(archive.root / INTERNAL / "001.json", raw)
    before = snapshot(archive.root.parent)
    invoke(archive, capsys, status=2)
    assert snapshot(archive.root.parent) == before
