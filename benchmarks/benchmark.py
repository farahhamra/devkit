"""Reproducible synthetic benchmark, not a claim about the private ElectroERP tree.

Run with the installed development interpreter: python benchmarks/benchmark.py.
Optional --root reuses a generated fixture for comparing revisions via PYTHONPATH.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import yaml

from devkit.modules import Modules
from devkit.project import Project, default_config
from devkit.schema import Schema
from devkit.source import SourceIndex


def generate(root: Path) -> None:
    modules = list(default_config()["modules"])
    (root / "devkit").mkdir(parents=True, exist_ok=True)
    config = {"modules": {name: {"models": [f"Item{i}"]} for i, name in enumerate(modules)}}
    (root / "devkit/electroerp.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    schema = root / "backend/prisma/schema.prisma"
    schema.parent.mkdir(parents=True, exist_ok=True)
    schema.write_text(
        "\n".join(
            f'model Item{i} {{ id Int @id name String @@map("items_{i}") }}' for i in range(len(modules))
        ),
        encoding="utf-8",
    )
    for i in range(1047):
        module = modules[i % len(modules)]
        path = root / f"backend/src/core/features/{module}/file{i}.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        imports = (
            f"import {{ fn{i - len(modules)}_0 }} from './file{i - len(modules)}';\n"
            if i >= len(modules)
            else ""
        )
        path.write_text(
            imports
            + "\n".join(
                f"export function fn{i}_{j}(value: number) {{ return Math.max(value, {j}); }}"
                for j in range(10)
            ),
            encoding="utf-8",
        )
    for i in range(1857):
        path = root / f"backend/ai/rag/features/{modules[i % len(modules)]}/guide{i}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            (f"References Item{i % len(modules)} and Item{(i + 1) % len(modules)}.\n") * 20, encoding="utf-8"
        )


def run(root: Path) -> None:
    report = {}
    for label in ("source_first", "source_repeat"):
        start = time.perf_counter()
        index = SourceIndex(Project(root))
        report[label] = {
            "seconds": round(time.perf_counter() - start, 3),
            "files": len(index.files),
            "symbols": len(index.symbols),
            **getattr(index, "stats", {}),
        }
    start = time.perf_counter()
    project = Project(root)
    rows = Modules(project, Schema(project)).catalog()
    report["module_catalog"] = {
        "seconds": round(time.perf_counter() - start, 3),
        "modules": len(rows),
        "files": len(project.files()),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.root:
        if not (args.root / "devkit/electroerp.yaml").exists():
            if args.root.exists() and any(args.root.iterdir()):
                parser.error("Fixture generation requires an empty directory")
            generate(args.root)
        run(args.root)
    else:
        with tempfile.TemporaryDirectory(prefix="devkit-benchmark-") as directory:
            root = Path(directory)
            generate(root)
            run(root)
