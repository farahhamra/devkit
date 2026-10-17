# 0.13.0 — Search diagnostics, find guidance and opt-in Node collection

- Search releases per-file text, caches lexical relative paths, and rejects nonmatching literal files before per-line matching. ASCII fast paths retain Unicode fallback; traversal, ignore rules, output and coverage semantics are unchanged.
- `search --timings` exposes setup/discovery/read/match timings and bytes read. See [search guidance and benchmark limits](../guides/SEARCH.md). Synthetic Linux results are not verification of the reported Windows delay.
- `find` help clarifies substring matching for files/directories. No-match wildcard-like literals get a hint; regex behavior, literal bracketed paths and exit codes remain unchanged.
- Adds an optional, disabled-by-default Node collector with bounded local runtime/memory captures, manual Prisma/request and cache adapters, and separately authorized heap capture. It is shipped in the checkout and wheel, not automatically installed into ElectroERP.
- Capture versions 1.1/2.1 add lifecycle/drop/window evidence. Incomplete collection blocks numeric comparison; legacy contracts remain accepted. Actual partial windows remain partial, not relabelled to pass gates.
- No live ElectroERP integration, database connection, production profiling or workload generation was performed. Read the [collector guide](../guides/PERFORMANCE-COLLECTOR.md) before a separately authorized app integration.
