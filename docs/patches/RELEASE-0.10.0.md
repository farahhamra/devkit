# v0.10.0 — offline database performance evidence

- Added `devkit performance database --input <capture.json>` with a versioned, bounded JSON contract. No database connection, SQL execution, capture instrumentation or load generation.
- Separate Prisma/SQL timing streams, request samples and detached work; explicit correlation only. Repeated queries remain candidates, not proven N+1.
- MySQL 8.0/8.4 JSON-plan estimates, exact table/model/module mappings and source ranges verified against capture-supplied file hashes. Unsupported plans and stale/missing evidence stay unknown.
- Distinguish Prisma declarations, migration declarations and exported database index inventories. Functional/unique/constraint indexes are protected; no index migrations or drop instructions are generated.
- Added performance coverage, `--strict`, full summary counts under output limits, a [synthetic capture](../../examples/performance-capture.json), [format and opt-in integration guide](../guides/PERFORMANCE.md), regression tests and installed-package smoke coverage.
- Updated bundled agent templates with command functionality and limits. CPU, memory, cache, browser/.NET and comparisons remain deferred.

After updating the installed package, `devkit agents sync --root <ElectroERP-root>` synchronizes the bundled instructions. A nested `.agents/devkit` checkout does not change which app root should receive them.
