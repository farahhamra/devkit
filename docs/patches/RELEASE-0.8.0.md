# Devkit 0.8.0

- Added `devkit query <pattern> [path]` using optional ast-grep 0.50.x for structural code matching, with `--lang auto|js|jsx|ts|tsx` and per-batch `--timeout`.
- Added normalized matches and named/variadic captures with original relative paths, exact line ranges, verified byte positions and source hashes. UTF-8/BOM/CRLF positions follow Devkit's conventions.
- Preserved Unicode source in JSON on legacy non-UTF-8 output streams using JSON escapes; UTF-8 output retains its existing format.
- Batched eligible temporary source snapshots by grammar, preserving Devkit's ignore/app/symlink policy and checking engine scan totals. No project ast-grep config, source execution or rewriting occurs.
- Preserved full scan summary counts and deterministic bounded matches under display limits. Query adds measured processing coverage and actionable missing-engine/version/pattern/timeout/result errors.
- Added the optional `[query]` installation extra, non-executing doctor availability checks, real-engine tests and installed-package checks. CI exercises the optional engine on Linux/Windows and retains base-package checks.
- Updated help, guides and agent command descriptions without prescribing tool preference. Other tools remain available without the optional engine; existing source-cache format is unchanged.

See [structural query installation and syntax](../guides/QUERY.md). Update the installed CLI with its `query` extra to include ast-grep, then explicitly run `devkit agents sync` for the updated command descriptions.
