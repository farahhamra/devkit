"""Bounded V8 heap graph inspection; string/property values never reach output."""

from collections import deque

from .capture import invalid, number
from .evidence import array, key, label

MAX_NODES = 100_000
MAX_EDGES = 500_000
DOMINATOR_BUDGET = 10_000_000
EDGE_TYPES = {"context", "element", "property", "internal", "hidden", "shortcut", "weak"}


def dominators(graph, predecessors, order):
    """Cooper fixed-point dominators with an explicit work bound (no recursion)."""
    rank = {node: i for i, node in enumerate(order)}
    immediate = {order[0]: order[0]}
    work, changed = 0, True
    while changed:
        changed = False
        for node in order[1:]:
            candidate = None
            for parent in predecessors[node]:
                work += 1
                if work > DOMINATOR_BUDGET:
                    return None
                if parent not in immediate:
                    continue
                if candidate is None:
                    candidate = parent
                    continue
                left, right = candidate, parent
                while left != right:
                    work += 1
                    if work > DOMINATOR_BUDGET:
                        return None
                    if rank[left] > rank[right]:
                        left = immediate[left]
                    else:
                        right = immediate[right]
                candidate = left
            if candidate is not None and immediate.get(node) != candidate:
                immediate[node] = candidate
                changed = True
    return immediate


def analyze(report, snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("snapshot"), dict):
        invalid("heap_snapshot", "expected a V8 heap snapshot")
    header = snapshot["snapshot"]
    meta = header.get("meta")
    if not isinstance(meta, dict):
        invalid("heap_snapshot.meta", "missing field metadata")
    fields, edge_fields = meta.get("node_fields"), meta.get("edge_fields")
    for value, required, where in (
        (fields, {"type", "name", "id", "self_size", "edge_count"}, "node_fields"),
        (edge_fields, {"type", "name_or_index", "to_node"}, "edge_fields"),
    ):
        array(value, "heap_snapshot." + where, 32)
        if (
            not all(isinstance(v, str) for v in value)
            or len(set(value)) != len(value)
            or not required <= set(value)
        ):
            invalid("heap_snapshot." + where, "unsupported or duplicate field layout")
    stride, edge_stride = len(fields), len(edge_fields)
    node_types, edge_types = meta.get("node_types"), meta.get("edge_types")
    for types, width in ((node_types, stride), (edge_types, edge_stride)):
        # V8 can retain unused trailing type descriptors (e.g. Node 24 omits
        # trace_node_id from node_fields but retains its numeric descriptor).
        # The field names, not the descriptor count, define the flat stride.
        if not isinstance(types, list) or not width <= len(types) <= 32:
            invalid("heap_snapshot.meta", "type metadata does not match field layout")
    ntype, etype = node_types[fields.index("type")], edge_types[edge_fields.index("type")]
    for types in (ntype, etype):
        if not isinstance(types, list) or not types or not all(isinstance(t, str) for t in types):
            invalid("heap_snapshot.meta", "expected type-name arrays")
    count = number(
        header.get("node_count"), "heap_snapshot.node_count", integer=True, minimum=1, maximum=MAX_NODES
    )
    edge_count = number(header.get("edge_count"), "heap_snapshot.edge_count", integer=True, maximum=MAX_EDGES)
    flat = array(snapshot.get("nodes"), "heap_snapshot.nodes", MAX_NODES * 32)
    edges = array(snapshot.get("edges"), "heap_snapshot.edges", MAX_EDGES * 32)
    strings = array(snapshot.get("strings"), "heap_snapshot.strings", 1_000_000)
    if not all(isinstance(s, str) for s in strings):
        invalid("heap_snapshot.strings", "expected strings")
    if len(flat) != count * stride or len(edges) != edge_count * edge_stride:
        invalid("heap_snapshot", "flat arrays do not match declared counts")
    offsets = {name: fields.index(name) for name in ("type", "name", "id", "self_size", "edge_count")}
    eoffsets = {name: edge_fields.index(name) for name in ("type", "name_or_index", "to_node")}
    nodes, ids = [], set()
    for offset in range(0, len(flat), stride):
        values = {name: flat[offset + index] for name, index in offsets.items()}
        for value in values.values():
            number(value, "heap_snapshot.node", integer=True, maximum=2**53 - 1)
        if values["id"] in ids or values["type"] >= len(ntype) or values["name"] >= len(strings):
            invalid("heap_snapshot.nodes", "duplicate ID or invalid type/string reference")
        ids.add(values["id"])
        kind = ntype[values["type"]]
        # String contents and regexp sources are data, not constructor/function names.
        name = (
            label(strings[values["name"]])
            if kind in {"object", "closure", "native"}
            else "("
            + (
                kind
                if kind
                in {
                    "array",
                    "string",
                    "code",
                    "synthetic",
                    "number",
                    "regexp",
                    "symbol",
                    "bigint",
                    "concatenated string",
                    "sliced string",
                    "hidden",
                    "object shape",
                }
                else "unknown"
            )
            + ")"
        )
        nodes.append(
            {
                "id": values["id"],
                "type": kind
                if kind
                in {
                    "object",
                    "closure",
                    "native",
                    "array",
                    "string",
                    "code",
                    "synthetic",
                    "number",
                    "regexp",
                    "symbol",
                    "bigint",
                    "concatenated string",
                    "sliced string",
                    "hidden",
                    "object shape",
                }
                else "unknown",
                "name": name,
                "self_bytes": values["self_size"],
                "edges": values["edge_count"],
            }
        )
    if nodes[0]["type"] != "synthetic" or sum(n["edges"] for n in nodes) != edge_count:
        invalid("heap_snapshot", "expected synthetic root at node zero and consistent edge counts")
    graph, predecessors = [[] for _ in nodes], [[] for _ in nodes]
    skipped, unknown, cursor = 0, False, 0
    for origin, node in enumerate(nodes):
        for _ in range(node["edges"]):
            values = {name: edges[cursor + index] for name, index in eoffsets.items()}
            cursor += edge_stride
            for value in values.values():
                number(value, "heap_snapshot.edge", integer=True, maximum=2**53 - 1)
            target, code = values["to_node"], values["type"]
            if target % stride or target >= len(flat) or code >= len(etype):
                invalid("heap_snapshot.edges", "invalid target offset or edge type")
            kind = etype[code]
            if kind not in {"element", "hidden"} and values["name_or_index"] >= len(strings):
                invalid("heap_snapshot.edges", "invalid property-name reference")
            if kind not in EDGE_TYPES:
                unknown = True
                skipped += 1
                continue
            if kind == "weak" or (kind == "shortcut" and origin != 0):
                skipped += 1
                continue
            target //= stride
            graph[origin].append((target, kind))
            predecessors[target].append(origin)
    # Iterative DFS postorder is also safe for long object-retention chains.
    seen, postorder, stack = {0}, [], [(0, iter(graph[0]))]
    while stack:
        node, children = stack[-1]
        edge = next(children, None)
        if edge is None:
            postorder.append(node)
            stack.pop()
        elif edge[0] not in seen:
            seen.add(edge[0])
            stack.append((edge[0], iter(graph[edge[0]])))
    order = list(reversed(postorder))
    immediate = dominators(graph, predecessors, order) if not unknown else None
    if immediate is None:
        report.gap(
            "heap_dominators_unavailable",
            "heap",
            "Unknown edge semantics or dominator work budget exceeded; retained-byte estimates are omitted.",
        )
    retained = [n["self_bytes"] for n in nodes] if immediate is not None else None
    if retained is not None:
        for node in reversed(order[1:]):
            retained[immediate[node]] += retained[node]
    groups = {}
    for i, node in enumerate(nodes):
        identity = key(node["type"], node["name"])
        group = groups.setdefault(
            identity,
            {
                "id": identity,
                "type": node["type"],
                "name": node["name"],
                "count": 0,
                "self_bytes": 0,
                "reachable_count": 0,
            },
        )
        group["count"] += 1
        group["self_bytes"] += node["self_bytes"]
        group["reachable_count"] += i in seen
        report.unit("heap:" + str(node["id"]))
    groups = sorted(groups.values(), key=lambda row: (-row["self_bytes"], row["id"]))
    for group in groups:
        report.metric(
            "heap.group.self_bytes:" + group["id"],
            group["self_bytes"],
            "bytes",
            name=group["name"],
            basis="endpoint",
        )
        report.metric(
            "heap.group.count:" + group["id"],
            group["count"],
            "objects",
            "neutral",
            name=group["name"],
            basis="endpoint",
        )
    parent = {0: None}
    pending = deque([0])
    while pending:
        origin = pending.popleft()
        for target, kind in graph[origin]:
            if target not in parent:
                parent[target] = origin, kind
                pending.append(target)
    candidates = sorted(
        (i for i in seen if nodes[i]["type"] != "synthetic"),
        key=lambda i: (-(retained[i] if retained is not None else nodes[i]["self_bytes"]), nodes[i]["id"]),
    )
    top = []
    for index in candidates[:20]:
        node = nodes[index]
        path, current = [], index
        while current is not None and len(path) < 32:
            predecessor = parent[current]
            path.append(
                {
                    "node_id": nodes[current]["id"],
                    "type": nodes[current]["type"],
                    "name": nodes[current]["name"],
                    "via": predecessor[1] if predecessor else None,
                }
            )
            current = predecessor[0] if predecessor else None
        top.append(
            {
                "node_id": node["id"],
                "type": node["type"],
                "name": node["name"],
                "self_bytes": node["self_bytes"],
                "retained_bytes_estimate": retained[index] if retained is not None else None,
                "immediate_dominator_id": nodes[immediate[index]]["id"] if immediate is not None else None,
                "retaining_path": list(reversed(path)),
                "path_truncated": current is not None,
                "source": report.location(node["id"]),
            }
        )
    total = sum(n["self_bytes"] for n in nodes)
    report.metric("heap.total_self_bytes", total, "bytes", basis="endpoint")
    return {
        "node_count": count,
        "edge_count": edge_count,
        "self_bytes": total,
        "strong_reachable_nodes": len(seen),
        "unreachable_nodes": count - len(seen),
        "excluded_or_unsupported_edges": skipped,
        "retained_status": "estimated" if retained is not None else "unavailable",
        "groups": groups,
        "top_retained": top,
        "retaining_paths_limit": 20,
        "note": "Dominators estimate retention in the captured strong-edge graph; weak and non-root shortcut edges are excluded. Conditional/ephemeron GC semantics, external allocations and runtime liveness are not proven. Retained sizes overlap and must not be summed. String values and property names are omitted.",
    }
