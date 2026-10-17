"""Opt-in synthetic search benchmark, not an ElectroERP performance claim.

Run with the same interpreter/checkout before and after changes:
  python tests/benchmark_search.py --files 1200 --lines 1000 --runs 5
Fixtures live in a temporary directory; no cache flushing or ignore changes.
"""

import argparse
import cProfile
import io
import json
import os
import pstats
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1200)
    parser.add_argument("--lines", type=int, default=1000)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    if min(args.files, args.lines, args.runs) < 1:
        parser.error("counts must be positive")
    with tempfile.TemporaryDirectory(prefix="devkit-search-") as directory:
        root = Path(directory)
        config = root / "devkit"
        config.mkdir()
        (config / "electroerp.yaml").write_text("{}", encoding="utf-8")
        (config / ".ignore").write_text("node_modules/\n.env\n", encoding="utf-8")
        content = "export const value = { name: 'widget', count: 42 };\n" * args.lines
        for index in range(args.files):
            file = root / "backend/src" / f"module{index % 30}" / f"file{index:05}.ts"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "DEVKIT_CONFIG_DIR"}
        command = ["search", "absent-needle", "backend", "--root", str(root), "--json"]
        durations = []
        for _ in range(args.runs):
            start = time.perf_counter()
            run = subprocess.run(
                [sys.executable, "-m", "devkit", *command],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            durations.append(time.perf_counter() - start)
            result = json.loads(run.stdout)
            assert result["data"] == [] and result["coverage"]["counts"]["text_read"] == args.files
        print(
            json.dumps(
                {
                    "synthetic": True,
                    "files": args.files,
                    "bytes": len(content.encode()) * args.files,
                    "seconds": durations,
                    "first_seconds": durations[0],
                    "repeat_median_seconds": statistics.median(durations[1:] or durations),
                    "coverage": result["coverage"]["counts"],
                },
                indent=2,
            )
        )
        if args.profile:
            from contextlib import redirect_stdout

            from devkit.cli import main as cli

            with cProfile.Profile() as profile, redirect_stdout(io.StringIO()):
                cli(command)
            pstats.Stats(profile).strip_dirs().sort_stats("cumulative").print_stats(25)


if __name__ == "__main__":
    main()
