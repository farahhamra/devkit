# Devkit 0.12.0

Four offline performance commands are added together:

- `performance runtime --input <capture>`: native V8 CPU profiles, sampled self/inclusive costs, Node event-loop distributions and GC interval unions.
- `performance memory --input <capture>`: process-memory trends, bounded V8 heap graphs, dominator retention estimates and retaining paths without raw string/property values.
- `performance cache --input <capture.json>`: epoch-aware counter deltas, hit ratios, observed latency distributions, storage growth and exported capacity/TTL context.
- `performance compare --before <capture> --after <capture>`: compatible database/runtime/memory/cache metrics, normalized windows, explicit unmatched evidence and optional relative regression gates.

Capture contract 2.0 adds runtime/memory/cache wrappers. Database 1.0 remains compatible, with optional environment/scope/collector metadata for comparison. Native profiles work without wrappers for partial inspection. No live profiling, instrumentation, app execution, network/database/cache connection, fixes or uploads occur. Captures require a separate authorized collection workflow. Browser/.NET Manager profiling is not included.

`--strict` gates incomplete evidence; compare also offers `--regression-percent` (default 10) and `--fail-on-regression`. Missing metrics are unknown rather than zero. Candidates do not prove bottlenecks, leaks, statistical significance or causality. Source attribution is optional, explicitly supplied and content-hash/range checked.

Help, bundled agent templates, synthetic captures, coverage and installed-package checks include all four commands. Details and limits: [runtime/memory/cache/comparison guide](../guides/PERFORMANCE-RUNTIME.md).

From inside the Devkit checkout (for example `<ElectroERP>/.agents/devkit`):

```powershell
git pull --ff-only origin main
python -m pip install --upgrade ".[query]"
devkit --version
devkit agents sync --root "C:\path\to\ElectroERP"
```

Use the actual project root for template sync and capture analysis. Inside the Devkit checkout the install target is `".[query]"`, not `"./.agents/devkit[query]"`.
