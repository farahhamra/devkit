"""Private, local-only browser artifacts and cross-process command locking."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from ..project import DevkitError, detect_root
from .policy import MAX_MESSAGE, name

INTERNAL = ".agents/.devkit-browser"
IGNORE = (
    b"# Private local Devkit browser artifacts; never commit authentication or captures.\n*\n!.gitignore\n"
)


def safe(path: Path) -> Path:
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DevkitError("Browser storage refuses symlinks and Windows junctions/reparse points.")
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise DevkitError("Browser storage parent must be a directory.")
    return path


@lru_cache(maxsize=1)
def windows_sid():
    import re
    import subprocess

    result = subprocess.run(
        [str(Path(os.environ["SystemRoot"]) / "System32/whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        check=True,
        timeout=10,
    )
    matches = re.findall(rb"S-1-[0-9-]+", result.stdout)
    if len(matches) != 1:
        raise DevkitError("Cannot determine the Windows account SID for private browser storage.")
    return matches[0].decode("ascii")


def private(path: Path, *, directory=False):
    safe(path)
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    # Exact protected owner-only DACL, not chmod's ineffective Windows permission bits.
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    setter = advapi.SetFileSecurityW
    setter.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    setter.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    sddl = f"D:P(A;{'OICI' if directory else ''};FA;;;{windows_sid()})"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise DevkitError("Cannot construct private Windows browser-storage permissions.")
    try:
        if not setter(str(path), 0x80000004, descriptor):
            raise DevkitError("Cannot protect Windows browser storage; no credentials will be saved.")
    finally:
        kernel.LocalFree(descriptor)


def directory(path: Path):
    safe(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    private(path, directory=True)


@contextmanager
def opened(path: Path):
    safe(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise DevkitError("Browser storage requires regular, non-hardlinked files.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        current = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_nlink) != (
            current.st_dev,
            current.st_ino,
            current.st_nlink,
        ):
            raise DevkitError("Browser storage changed during access.")
        yield handle


def read(path: Path, maximum=MAX_MESSAGE) -> bytes | None:
    try:
        with opened(path) as handle:
            content = handle.read(maximum + 1)
    except FileNotFoundError:
        return None
    if len(content) > maximum:
        raise DevkitError("Browser storage file exceeds its size limit.")
    return content


def read_json(path: Path):
    data = read(path)
    if data is None:
        return None
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise DevkitError("Invalid browser state file; contents omitted.") from None


def write(path: Path, data: bytes, *, replace=False):
    safe(path)
    directory(path.parent)
    if path.exists():
        if not replace:
            raise DevkitError("Browser artifact already exists; choose a new output/auth name.")
        read(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temp = Path(temporary)
    try:
        private(temp)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            safe(path)
            if path.exists():
                read(path)
            os.replace(temp, path)
        else:
            # Exclusive publication. Unsupported filesystems fail rather than overwrite.
            os.link(temp, path)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temp.unlink(missing_ok=True)


def write_json(path: Path, value, **kwargs):
    write(path, json.dumps(value, ensure_ascii=True, allow_nan=False).encode(), **kwargs)


@contextmanager
def locked(path: Path):
    safe(path)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, b"0")
    except FileExistsError:
        read(path, 1)
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r+b") as handle:
        private(path)
        handle.seek(0)  # A newly created lock's initial write leaves the cursor at byte 1.
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DevkitError(
                "Another command is using this browser session; wait for it to finish."
            ) from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BrowserContext:
    def __init__(self, root=None, config=None, app="all"):
        self.root = safe(Path(root).expanduser().absolute()) if root else detect_root(Path.cwd())
        if not self.root.is_dir():
            raise DevkitError("Browser project root must be an existing directory.")
        if app not in {"all", "erp"}:
            raise DevkitError("The browser addon supports ERP only, not Hub or Manager.")
        self.app = "erp"
        self.warnings = []
        self.base = safe(self.root / INTERNAL)

    def initialize(self):
        self.check_tracked()
        directory(self.base)
        ignore = self.base / ".gitignore"
        current = read(ignore)
        if current is None:
            try:
                write(ignore, IGNORE)
            except (DevkitError, FileExistsError):
                # Different session names can initialize their common storage concurrently.
                if read(ignore) != IGNORE:
                    raise
        elif current != IGNORE:
            raise DevkitError(
                "Conflicting browser .gitignore; preserve its contents and restore the documented private-storage rules."
            )
        for part in ("sessions", "auth", "screenshots"):
            directory(self.base / part)

    def check_tracked(self):
        try:
            result = subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "-c",
                    "core.fsmonitor=false",
                    "ls-files",
                    "-z",
                    "--",
                    INTERNAL,
                ],
                cwd=self.root,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except FileNotFoundError:
            return  # Fresh non-Git projects still get exclusions before any artifact.
        except (OSError, subprocess.TimeoutExpired):
            raise DevkitError(
                "Could not check whether private browser artifacts are already tracked by Git."
            ) from None
        if result.returncode == 0 and any(
            value and value != (INTERNAL + "/.gitignore").encode() for value in result.stdout.split(b"\0")
        ):
            raise DevkitError(
                "Private browser artifacts are already tracked by Git. Remove them from tracking and investigate exposed credentials before continuing; .gitignore cannot untrack them."
            )
        if result.returncode != 0 and any(
            (parent / ".git").is_file() or (parent / ".git/HEAD").is_file()
            for parent in (self.root, *self.root.parents)
        ):
            raise DevkitError(
                "Git tracking inspection failed in this workspace; private browser storage was not initialized."
            )

    def session(self, label):
        return safe(self.base / "sessions" / (name(label) + ".json"))

    def auth(self, label):
        return safe(self.base / "auth" / (name(label) + ".json"))
