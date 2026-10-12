# Devkit 0.7.0

- Added `devkit analyze <file>` with numbered source, structured syntax evidence and explicit lexical parent/child links, in the existing text/JSON output styles.
- Added exact inclusive line ranges and byte-column/offset spans for source-backed evidence: declarations, imports/exports, calls, references, local-route/Prisma candidates and diagnostics—not just functions.
- Added exact `--symbol` selection with ambiguity errors, ancestor/descendant metadata and focused excerpts. Same-line nesting uses syntax-tree ancestry, not overlapping line ranges.
- Kept default parsing single-file. Opt-in `--with-context` adds selected-app-wide import consumers, module ownership and schema candidates, with explicit scope/coverage.
- Preserved truthful source/structure display-completeness flags and original summary counts under result and character limits. Source text remains data; no source execution, LLM reasoning or type-checker guarantees.
- Updated help, bundled agent rules/skill/AGENTS fragment, documentation, regression tests and installed-package smoke checks. Disposable cache format is now `3`; existing summaries rebuild automatically. Feedback and agent-sync state are unchanged.
- Organized user documentation under `docs/guides/`, alongside `docs/agents/` and `docs/patches/`, with no files directly in `docs/`. Agent-template additions describe the command and its functionality without prescribing tool selection.

See [source briefings](../guides/ANALYZE.md). After updating the CLI, explicitly run `devkit agents sync` to update bundled guidance in your project; no automatic sync or force overwrite is performed.
