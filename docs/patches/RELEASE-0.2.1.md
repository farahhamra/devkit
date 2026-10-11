# Devkit 0.2.1

## Cross-module AI tool companions

The broad `ai` manifest owns `backend/ai`, including e-commerce RAG documentation. Plugin implementations live under `backend/src/plugins/e-commerce/ai`, owned by `e-commerce`. Checks previously searched only within the current module's owned files, incorrectly reporting six existing implementations as missing when checking `ai` or `all`.

Companion lookup now indexes the files allowed by the current app selection and `.ignore` once per check. It matches exact companion filenames within the same app, backend tree and concrete feature/plugin namespace, rather than sharing the current module's ownership list.

- Plugin docs in `backend/ai/rag/tools/plugin-<plugin>/` match implementations beneath `backend/src/plugins/<plugin>/`, including nested AI folders. Centralized `backend/src/core/features/ai/tools/plugin-<plugin>/` folders also carry a plugin namespace.
- Core docs in `backend/ai/rag/tools/<feature>/` match core feature tools in `backend/src/core/features/ai/tools/<feature>/` or beneath `backend/src/core/features/<feature>/`.
- Unambiguous manifest aliases resolve to their canonical module name. Core and plugin namespaces stay distinct, even if their names or manifest ownership overlap.
- A file in another app, another backend tree, another feature/plugin, or an ignored directory cannot satisfy a companion check.
- Zero candidates remains a failure. Multiple candidates within one namespace become an ambiguity warning, not an arbitrary successful match. The additive `companions` field lists the candidate paths.
- Unrecognized layouts or ambiguous aliases only match siblings in the same directory. Co-locate companion files for such custom layouts; assigning unrelated directories to one broad module does not establish a tool identity.

Ownership itself is unchanged: plugin docs can legitimately appear under both `ai` and `e-commerce`. Informational prose references still do not become owners. Parser warnings and output budgets are unchanged.

## Bills and outgoing payments

| CLI module | Prisma model | MySQL table |
| --- | --- | --- |
| `bills` | `Bill` | `bills` |
| `payments-out` | `PaymentOut` | `payments_out` |

`bill` aliases `bills`; `payment-out` and `payments_out` alias `payments-out`. Existing `Payment` ownership remains with `payments`.

No application schema, database table, generated Prisma client or migration is modified. Devkit reads the current schema, including `BillStatus`, the optional `PaymentOut.bill` relationship, physical column mappings, constraints and delete behavior. On an older schema these modules remain unavailable until their source/models exist; explicitly checking an absent model still reports it missing.

Project-local module overrides replace the corresponding built-in entry. If you already override either new module, update that override to include the intended model and aliases.

## Update and retest

From the Devkit checkout, pull `main` and reinstall using the installer you originally used:

```powershell
git pull --ff-only origin main
python -m pip install --upgrade .
# Or: pipx reinstall electroerp-devkit
# Or: uv tool install --force .
devkit --version
```

Keep your existing root/configuration flags for a nested `.agents/devkit` checkout. From the ElectroERP root:

```powershell
devkit module check ai --app erp --json
devkit module check all --app erp --json
devkit module inspect bills --app erp --json
devkit module inspect payments-out --app erp --json
devkit schema relations Bill --app erp --json
```

Regression tests exercise all six reported tool pairs, both companion directions, cold/warm/cache-bypassed checks, missing/ignored companions, cross-plugin/core/app/tree collisions, ambiguous matches, and the supplied payables schema slice. These fixtures do not substitute for a rerun on the private ElectroERP Windows checkout. The schema fixture deliberately uses `.prisma.txt` so it cannot be mistaken for a live schema when Devkit is nested in the app repository.
