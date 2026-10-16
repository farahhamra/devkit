"""Counter-epoch-aware cache windows; no cache keys or values are accepted."""

from itertools import pairwise

from .capture import choice, invalid, number, obj, string
from .evidence import Report, array, distribution, sample_time, window

COUNTERS = ("hits", "misses", "sets", "evictions", "expirations", "errors")


def analyze(project, capture, prefix=""):
    report = Report(project, capture, "cache", prefix)
    caches, seen = [], set()
    duration = report.meta["workload"]["duration_ms"] if report.meta else None
    for entry in array(capture.get("caches", []), "caches"):
        obj(entry, "caches[]", ("id", "backend", "scope", "samples"), ("limits", "latency"))
        identifier = string(entry["id"], "caches.id")
        if identifier in seen:
            invalid("caches", "duplicate cache IDs")
        seen.add(identifier)
        choice(entry["backend"], "caches.backend", {"redis", "memory", "other"})
        choice(entry["scope"], "caches.scope", {"process", "shared"})
        samples = array(entry["samples"], "caches.samples")
        previous = -1
        for row in samples:
            obj(
                row,
                "caches.samples[]",
                ("at_ms", "epoch", "hits", "misses", "entries", "bytes"),
                COUNTERS[2:],
            )
            previous = sample_time(row, "caches.samples", previous, duration)
            string(row["epoch"], "caches.samples.epoch")
            for field in (*COUNTERS, "entries", "bytes"):
                if field in {"hits", "misses"} or row.get(field) is not None:
                    number(row[field], "caches.samples." + field, integer=True)
        limits = entry.get("limits")
        if limits is not None:
            obj(limits, "caches.limits", ("max_entries", "max_bytes", "ttl_ms"))
            for field, value in limits.items():
                if value is not None:
                    number(value, "caches.limits." + field, integer=field != "ttl_ms", minimum=1)
        else:
            report.gap(
                "cache_limits_unknown",
                identifier,
                "Capacity/TTL policy was not exported; boundedness is unknown.",
            )
        known = {field for field in COUNTERS if all(row.get(field) is not None for row in samples)}
        deltas = {field: 0 if field in known else None for field in COUNTERS}
        if len(known) != len(COUNTERS):
            report.gap(
                "cache_counters_missing",
                identifier,
                "Some secondary counters were not measured; omitted/null counters stay unknown.",
            )
        observed_ms, resets, intervals = 0, 0, []
        for before, after in pairwise(samples):
            elapsed = after["at_ms"] - before["at_ms"]
            if before["epoch"] != after["epoch"] or any(after[f] < before[f] for f in known):
                resets += 1
                intervals.append(
                    {"start_ms": before["at_ms"], "end_ms": after["at_ms"], "status": "reset_or_epoch_change"}
                )
                continue
            observed_ms += elapsed
            difference = {field: after[field] - before[field] for field in known}
            for field, value in difference.items():
                deltas[field] += value
            intervals.append(
                {"start_ms": before["at_ms"], "end_ms": after["at_ms"], "status": "observed", **difference}
            )
        if resets:
            report.gap(
                "cache_counter_reset",
                identifier,
                "Reset/epoch-change intervals are excluded rather than treated as negative or zero activity.",
            )
        if len(samples) < 2:
            report.gap(
                "cache_samples_insufficient",
                identifier,
                "Two counter snapshots are required for an interval; cumulative counters are not window totals.",
            )
        window(report, samples, identifier)
        complete_window = bool(
            samples
            and duration is not None
            and samples[0]["at_ms"] == 0
            and samples[-1]["at_ms"] == duration
            and not resets
            and len(samples) >= 2
        )
        if not observed_ms:
            deltas = dict.fromkeys(COUNTERS)
        lookups = deltas["hits"] + deltas["misses"] if observed_ms else None
        ratio = deltas["hits"] / lookups if lookups else None
        if not lookups:
            report.gap(
                "cache_no_lookups",
                identifier,
                "No observed lookup denominator; hit rate is unknown, not 100%.",
            )
        row = {
            "id": identifier,
            "backend": entry["backend"],
            "scope": entry["scope"],
            "limits": limits,
            "sample_count": len(samples),
            "observed_ms": observed_ms,
            "excluded_intervals": resets,
            "counters": deltas,
            "hit_rate": ratio,
            "lookup_count": lookups,
            "lookups_per_second": lookups * 1000 / observed_ms if observed_ms else None,
            "intervals": intervals,
            "source": report.location(identifier),
        }
        signature = {"backend": entry["backend"], "scope": entry["scope"]}
        extras = {"cache_id": identifier, "signature": signature, "comparable": complete_window}
        report.metric("cache.hit_rate:" + identifier, ratio, "ratio", "higher", **extras)
        report.metric(
            "cache.lookup_rate:" + identifier,
            row["lookups_per_second"],
            "lookups/second",
            "neutral",
            **extras,
        )
        if samples:
            storage = {
                field: [s[field] for s in samples if s[field] is not None] for field in ("entries", "bytes")
            }
            if any(len(values) != len(samples) for values in storage.values()):
                report.gap(
                    "cache_storage_missing",
                    identifier,
                    "Some cache size observations were not measured; missing byte/entry counts stay unknown.",
                )
            row["storage"] = {
                "initial_entries": samples[0]["entries"],
                "final_entries": samples[-1]["entries"],
                "initial_bytes": samples[0]["bytes"],
                "final_bytes": samples[-1]["bytes"],
                "peak_bytes": max(storage["bytes"], default=None),
                "entry_growth": samples[-1]["entries"] - samples[0]["entries"]
                if samples[-1]["entries"] is not None and samples[0]["entries"] is not None
                else None,
                "byte_growth": samples[-1]["bytes"] - samples[0]["bytes"]
                if samples[-1]["bytes"] is not None and samples[0]["bytes"] is not None
                else None,
            }
            report.metric(
                "cache.final_bytes:" + identifier, samples[-1]["bytes"], "bytes", basis="endpoint", **extras
            )
            if limits:
                for maximum, field in (("max_entries", "entries"), ("max_bytes", "bytes")):
                    if (
                        limits[maximum] is not None
                        and storage[field]
                        and max(storage[field]) > limits[maximum]
                    ):
                        report.finding(
                            "cache_capacity_exceeded",
                            "Observed storage exceeds the exported capacity; confirm that measurements and policy cover the same cache scope.",
                            cache_id=identifier,
                            limit=maximum,
                        )
                if (
                    all(v is None for v in limits.values())
                    and len(samples) >= 2
                    and (row["storage"]["byte_growth"] or 0) > 0
                ):
                    report.finding(
                        "cache_unbounded_growth",
                        "Storage grew while the exporter declared no capacity or TTL bound; review retention policy. Growth alone is not a leak.",
                        cache_id=identifier,
                    )
        if "latency" in entry:
            latency = entry["latency"]
            if not isinstance(latency, dict):
                invalid("caches.latency", "expected an object")
            scope = choice(latency.get("scope"), "caches.latency.scope", {"lookup", "all_operations"})
            measured = distribution({k: v for k, v in latency.items() if k != "scope"}, "caches.latency")
            row["latency"] = {"scope": scope, **measured}
            population = scope != "lookup" or measured["count"] == lookups
            if not population:
                report.gap(
                    "cache_latency_population",
                    identifier,
                    "Latency and observed lookup counts differ; timings may represent a subset or a different interval.",
                )
            for field in ("mean_ms", "max_ms", "p50_ms", "p95_ms", "p99_ms"):
                report.metric(
                    "cache.latency." + field + ":" + identifier,
                    measured.get(field),
                    "ms",
                    cache_id=identifier,
                    signature={**signature, "latency_scope": scope},
                    comparable=complete_window and population,
                )
        else:
            report.gap(
                "cache_latency_missing", identifier, "Counter hits/misses do not measure cache latency."
            )
        report.unit(identifier)
        caches.append(row)
    return report.finish(caches=caches, summary={"cache_count": len(caches)})
