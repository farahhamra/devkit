# Runtime, memory, cache and comparison evidence

```powershell
devkit performance runtime --input diagnostics/performance/runtime.json --app erp --json
devkit performance runtime --input diagnostics/performance/cpu.cpuprofile --app erp --json
devkit performance memory --input diagnostics/performance/memory.json --app erp --json
devkit performance memory --input diagnostics/performance/heap.heapsnapshot --app erp --json
devkit performance cache --input diagnostics/performance/cache.json --app erp --json
devkit performance compare --before diagnostics/performance/before.json --after diagnostics/performance/after.json --app erp --json
devkit performance compare --before diagnostics/performance/before.json --after diagnostics/performance/after.json --regression-percent 10 --fail-on-regression --strict --json
```

These four commands analyze **already exported measurements**, offline. They do not run or instrument application code, attach debuggers, collect profiles, send network requests, connect to Redis/MySQL, generate workloads or apply fixes. No optional engine/package is required. Collection is a separate, authorized application task. Static source alone cannot establish measured latency, retention or cache effectiveness. Browser and .NET Manager captures are outside this contract.

## Inputs, safety and limits

Paths are relative to the project root, not the current directory. A nested `.agents/devkit` installation can use explicit `--root <ElectroERP>` and `--config .agents/devkit/electroerp.yaml`. Inputs may be outside app source folders but must be inside the project, readable regular files, and allowed by Devkit's `.ignore`. Explicit arguments do not bypass exclusions. Symlinks/junctions, escapes, devices and internal Devkit storage are refused. JSON captures specify `erp` or `hub`; an explicit `--app` must match. Native V8 files contain no app/workload metadata and are reported as partial evidence.

Keep captures local and Git-ignored in a dedicated directory that is **not** excluded by Devkit's ignore policy. Heap snapshots can contain passwords, tokens and customer data, including environment variables. Raw capture safety is the collector's responsibility. Devkit omits heap string/regexp contents, property names and CPU script URLs from output; only restricted symbolic names, numeric evidence and hashes are projected. This is not a guarantee that permitted names or metadata contain no sensitive information. Do not upload raw captures automatically. Devkit never follows capture URLs, script paths, external source maps or referenced capture files.

Extended/native inputs: at most 64 MiB UTF-8 JSON (BOM accepted), four million JSON values, nesting depth 64. CPU: 100,000 nodes and one million samples. Heap: 100,000 nodes, 500,000 edges, one million strings and ten million dominator work steps. Other arrays: 20,000 entries. Referenced source verification: 4 MiB/file, 16 MiB total. Oversized/invalid structures fail with exit `2`. Exceeding the dominator work budget preserves shallow-size evidence but omits retained estimates and marks coverage partial. Existing [database 1.0 inputs](PERFORMANCE.md) keep their smaller limits, including when compared.

Large production heaps may exceed these limits. Do not slice flat node/edge arrays or invent partial graphs to make them fit. A process-counter-only memory capture can still report trends (with a missing-heap gap); use a dedicated heap explorer for oversized native snapshots.

## Capture contract 2.0

The output envelope remains `schema_version: "1.0"`. Input 2.0 is separate from the unchanged database 1.0 contract. The examples below are **synthetic fixtures, not measurements of ElectroERP**:

- [Runtime example](../../examples/performance-runtime.json)
- [Memory example](../../examples/performance-memory.json)
- [Cache example](../../examples/performance-cache.json)

The wrapper contains `capture_version: "2.0"`, `kind: "runtime" | "memory" | "cache"`, `metadata`, kind-specific fields described below, and optional `sources`. Unknown wrapper fields, duplicate JSON keys, nonfinite values, inconsistent distributions and invalid references fail validation without echoing submitted values. Native snapshot documents may contain additional V8 fields; those are not blindly returned.

Required metadata:

| Field | Contract |
| --- | --- |
| `app` | `erp` or `hub` |
| `captured_at` | ISO timestamp with timezone |
| `runtime` | Exactly `{ "engine": "node", "version": "22.14.0" }`, using the actual measured version |
| `workload` | Required non-sensitive `name` and positive `duration_ms`; optional positive integer `concurrency`, nonnegative `warmup_ms`, nonnegative integer `operations` |

Optional metadata: `environment`, `process_scope`, `collector` (non-sensitive identifiers), `source_revision` (40/64 lowercase hex SHA). For meaningful comparison, export environment/hardware identity, process/worker scope, collector version/boundary, concurrency and warmup explicitly. These are exporter declarations, not facts Devkit can independently verify. The revision and capture timestamp may differ between runs.

IDs/names use ASCII alphanumeric characters plus `_ . : -`, start with an alphanumeric character and are limited to 128 characters. Versions begin with a digit and are limited to 64 characters. General numbers are finite and bounded to 10^15 unless a smaller field-specific limit applies. Missing optional fields are omitted, not `null`, unless explicitly nullable below. Relative timestamps are monotonic milliseconds from the declared window start, not wall-clock timestamps.

An optional `sources` array contains `{ "id": "2", "location": { "path": "backend/src/example.ts", "sha256": "<64 lowercase hex>", "line": 10, "end_line": 90 } }`. IDs match CPU **node IDs** (as strings), heap **object IDs** (as strings), or cache IDs. IDs are unique within a capture. Paths are project-relative and app/ignore-checked; ranges are inclusive, 1-based and verified against the SHA-256 of raw source bytes. Only content-verified ranges are emitted. Stale, excluded, inaccessible or out-of-range supplied references produce gaps, not guessed locations. No attribution is required when the collector cannot establish it; native generated-JavaScript positions are not fabricated as original TypeScript ranges. Matching a hash proves file content, not execution attribution.

## Runtime

Runtime captures may contain `cpu_profile`, `event_loop` and `gc`. Missing components stay explicit gaps. A supplied empty `gc: []` means the exporter observed zero events, unlike omitting the field.

### CPU sampling

`cpu_profile` is a native V8 CPU profile with `nodes`, `startTime`, `endTime` and optional `samples`, `timeDeltas`. A raw `.cpuprofile` or equivalent JSON is also accepted without a wrapper. Nodes contain `id`, `callFrame`, optional `children`, `hitCount`, `positionTicks`, `deoptReason`. Frame fields are `functionName`, `scriptId`, `url`, `lineNumber`, `columnNumber`. Node IDs/children must form one connected acyclic tree. Sample IDs must exist; if supplied, one nonnegative microsecond delta is required per sample and their sum cannot exceed the profile window.

Self cost attributes each time delta to its sampled node. Inclusive cost adds descendants bottom-up; it overlaps between ancestors and must not be summed. Recursive instances remain separate call-tree nodes; function groups aggregate **self time only**. Missing time deltas produce counts, not invented milliseconds. Observed duration and the unsampled tail are separate. Results are sampling estimates, not instrumented function elapsed times or exact CPU utilization. Script URLs and frame positions form a stable hash for comparison but are not printed as source locations. Moving/rebuilding code can change these identities and yield unmatched frames.

### Event-loop and GC evidence

`event_loop` requires positive `window_ms`, positive `resolution_ms`, and `delay`. A distribution requires `count` (positive integer), `total_ms`, `min_ms`, `max_ms`, with optional `p50_ms`, `p95_ms`, `p99_ms`. Totals, bounds and supplied percentile order must agree. Devkit derives only the mean; it never invents percentiles from averages. Optional `utilization` has `active_ms` and `idle_ms`; the derived ratio is neutral in regression gating because higher utilization is not inherently worse. Active plus idle must fit the measurement window.

`gc` is an array of `{ "start_ms": 5, "duration_ms": 4, "kind": "minor" }`; kind is `minor|major|incremental|weakcb|unknown`. Events must fit the workload window. Overlapping intervals are unioned, not summed. The union is observed GC event time, **not** a claim of stop-the-world pause duration. Missing profiles/histograms/events and window mismatches remain gaps. CPU/event-loop metrics whose windows differ from declared workloads are not compared.

## Memory

Memory captures may contain `process_samples`, `heap_snapshot`, or both. A raw native `.heapsnapshot` or equivalent JSON is accepted. A single snapshot cannot show growth over time; counters alone cannot identify retained objects.

Each process sample requires `at_ms`, `heap_used_bytes`, `heap_total_bytes`, `rss_bytes`, `external_bytes`, `post_gc` (boolean); optional `array_buffers_bytes`. Byte counts are nonnegative integers; used heap cannot exceed total heap, and array-buffer bytes cannot exceed external bytes. Timestamps must strictly increase within the declared window. Samples not spanning zero to `duration_ms` remain partial; their process metrics are not used in comparison.

Reports include first/last/peak values, deltas and least-squares slopes in bytes/second, separately per metric. RSS, heap, external memory and array buffers **overlap and are not additive**. A post-GC growth trend requires at least three exporter-labelled post-GC samples. Positive growth is a review candidate, not proof of a leak: warmup, intentional caches and live data also grow. `post_gc` labels are collector claims, not inferred from a drop in RSS.

Heap parsing uses the V8 field metadata, flat node/edge arrays and string table. Counts, IDs, offsets and type references are checked; field names define the stride even when V8 retains extra trailing type descriptors. Reports contain constructor/type groups (count and shallow bytes), top 20 non-synthetic retained-object candidates, immediate dominator IDs and shortest strong retaining paths of at most 32 nodes. Truncated paths are marked. Weak edges and non-root shortcuts are excluded. Dominators estimate retention **in that selected strong-edge graph**; conditional/ephemeron GC semantics and live-runtime reachability are not proven. Unknown edge semantics disable retained estimates. Shared objects/cycles are handled without treating every parent as retaining the full shared subtree. Overlapping retained sizes are never summed into group totals. External allocations are not claimed to be completely represented.

## Cache

Cache captures contain `caches`, an array of entries requiring unique `id`, `backend` (`redis|memory|other`), `scope` (`process|shared`), and `samples`.

Samples require `at_ms`, `epoch`, cumulative `hits`, cumulative `misses`, `entries`, `bytes`. Hits/misses are nonnegative integers; entries/bytes are integers or `null` when unmeasured. Optional cumulative `sets`, `evictions`, `expirations`, `errors` are integers or `null`; omitted/null counters stay unknown, never zero. Timestamps strictly increase within the declared window. `epoch` identifies a continuous counter lifetime, not a cache key.

Devkit calculates deltas only between adjacent snapshots in the same epoch with no observed counter decrease. Reset intervals are excluded. Hit rate is `hits / (hits + misses)` for observed intervals, not hits divided by sets; rate is unknown for a zero denominator. Lookup throughput uses only observed time. With no observed interval, counts and rates are unknown, not invented zero activity. Resets/partial windows prevent metric comparison. No Redis global counter is attributed to one logical cache unless the exporter established that scope.

Optional `limits` contains all three of `max_entries`, `max_bytes`, `ttl_ms` (positive values or `null`). Here `null` explicitly declares **no bound of that kind**; omit `limits` if policy is unknown. Exceeding declared capacity or growth with explicitly no capacity/TTL bound yields candidates. TTL alone does not prove eviction enforcement or memory boundedness.

Optional `latency` contains `scope: "lookup" | "all_operations"` and the distribution fields described above. Export the distribution for the same interval/cache scope. Lookup count mismatch produces a gap and prevents latency comparison. Cache hits/misses do not measure latency; absent latency is unknown. Cache keys, values, customer identifiers and raw operation arguments are not accepted by the contract.

## Compare

`--before` and `--after` must have the same kind. Supported: versioned database 1.0, runtime, memory and cache captures. Reports are built from original captures, not truncated Devkit output. Native files without metadata cannot establish a valid comparison; wrap them with actual measured metadata first.

Numeric comparisons require matching, present app/environment/process scope/collector/runtime and workload name/concurrency/warmup. Database comparisons also require matching known database engine/version and actual Node version. Missing/different metadata yields `comparison_status: incompatible`, explicit issues and no numeric comparisons. This is conservative: the command does not assert that two identical labels guarantee reproducible conditions.

Metrics match by query kind/timing source/fingerprint, CPU frame hash, heap type/constructor group hash, or cache ID, as appropriate. Different distribution resolutions, measurement scopes or incomplete windows are not compared. Missing metrics are unmatched/unknown, **not zero**. Window totals (CPU sampled time, GC event time/count, database time/count) normalize per second; endpoint storage metrics require equal workload duration or the same positive operation count. Means/distributions stay in their measured units. A larger cache/heap or busier CPU may be intentional; directional changes are candidates, not automatic defects.

Each comparable metric includes before/after/delta, unit, direction, percent change and `regression_candidate`. Default relative threshold is `--regression-percent 10`; it must be finite and within 0–1,000,000. Lower latency/cost/size and higher cache hit ratio are directional; throughput, counts and event-loop utilization are neutral. A worse change meeting the threshold is a candidate. An unequal zero baseline has no defined percentage; it reports null and can still be a candidate. Two zeros are unchanged. Numeric overflow produces null percentage, not infinity.

A single before/after pair does **not** establish statistical significance or causality. Use controlled workloads, repeated independent runs and external statistical review for decisions. The command does not aggregate multiple runs or authorize optimizations.

## Output and gates

Reports use the existing envelope: `data`, warnings, `coverage`, `truncated`, and truncation details. Data contains projected evidence, metrics, findings, unknowns, limitations and untruncated summary counts. `coverage.stages.performance` measures processed capture units; runtime/memory/cache and comparison do not claim schema/source/dependency scans. Optional source hash checks are not AST analysis. Native input is partial because metadata and companion measurements are absent.

- Exit `0`: report generated, possibly partial or with candidates.
- Exit `1`: `--strict` found incomplete coverage, or compare's explicit `--fail-on-regression` gate found a candidate.
- Exit `2`: invalid/unsafe capture, incompatible capture **kind**, configuration or I/O failure.

Incompatible metadata alone does not error unless `--strict` is selected. `--fail-on-regression` alone does not fail unknown comparisons; combine the two gates when incompleteness must also fail. `--max-results` / `--max-output-chars` bound presentation, not computation. Summaries and coverage survive truncation; detailed metrics/paths may be omitted. Capture evidence is always read live, never persisted in source caches, and `--no-cache` does not change its results.

## Separate, opt-in collection guidance

No collector is installed into ElectroERP by this release. An application agent implementing a separately authorized exporter should:

1. Use local/staging controlled data, a fixed workload, actual versions/hardware identity, explicit warmup/concurrency, and per-process/worker scope. Keep CPU/profile/counter boundaries aligned. Never silently profile production.
2. Export native Node/V8 CPU profiles through an explicitly enabled Node profiler/inspector session. Measure event-loop delay separately using `monitorEventLoopDelay`; its nanoseconds need division by 1,000,000 for milliseconds. Record histogram count/mean-derived total/bounds and genuinely observed percentiles, actual resolution/window, and deltas from `eventLoopUtilization` at matching boundaries. Empty histograms are unknown, not fabricated zero measurements.
3. If GC evidence is needed, collect `perf_hooks` GC entries only within the declared window and preserve their kind/duration. Do not interpret all GC event time as main-thread pauses. Align `process.memoryUsage()` timestamps; external/array-buffer sizes overlap. Label post-GC samples only when the collector established that boundary.
4. Treat heap capture as potentially disruptive: it can pause the process, require substantial extra memory and expose secrets. Obtain authorization for that process/window; use sanitized test data. Do not automatically force GC or attach to arbitrary processes. Keep snapshots within analysis limits or inspect them separately.
5. Export scoped cache counter deltas from continuous cumulative snapshots, restart epochs, actual policy and matching operation-latency distributions. Preserve unmeasured byte sizes/counters as unknown. Do not record keys/values or assume server-wide Redis stats identify one application cache.
6. Bound capture buffers and stop collection cleanly; do not affect application outcomes when export fails. Preserve accurate missing-data signals. Do not substitute synthetic examples for actual measurements. Review/sanitize files, keep them out of Git, then run Devkit separately.
