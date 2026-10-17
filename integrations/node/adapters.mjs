import { randomUUID, createHash } from 'node:crypto';

export const safeId = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
export const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1e15;
export const count = value => finite(value) && Number.isSafeInteger(value);
const probes = new WeakSet();
export const isCacheProbe = value => value !== null && typeof value === 'object' && probes.has(value);
const operations = new Set(('findUnique findUniqueOrThrow findFirst findFirstOrThrow findMany create createMany ' +
  'update updateMany delete deleteMany upsert count aggregate groupBy').split(' '));
const operators = new Set(('where data select include orderBy take skip cursor distinct AND OR NOT equals in notIn ' +
  'lt lte gt gte contains startsWith endsWith not some every none is isNot connect disconnect set increment ' +
  'decrement multiply divide create update delete upsert connectOrCreate _count _sum _avg _min _max having by').split(' '));

// A bounded, value-free operation shape. Unknown keys are not retained, even in hashes.
export function operationShape(params, database) {
  const model = params.model;
  if (!Object.hasOwn(database.models, model) || !operations.has(params.action)) throw Error('operation_unsupported');
  const fields = new Set(database.models[model].fields);
  let remaining = 400;
  function shape(value, depth = 0) {
    if (--remaining < 0 || depth > 8) throw Error('sample_limit');
    if (value === null) return 'null';
    if (Array.isArray(value)) {
      // Cardinality is deliberately not an identifier; deduplicate element shapes.
      if (value.length > 100) throw Error('sample_limit');
      return [...new Set(value.map(v => JSON.stringify(shape(v, depth + 1))))].sort();
    }
    if (value instanceof Date || Buffer.isBuffer(value)) return 'scalar';
    if (typeof value !== 'object') return typeof value;
    const keys = Object.keys(value);
    if (keys.length > 100) throw Error('sample_limit');
    return keys.sort().map(key => fields.has(key) || operators.has(key)
      ? [key, shape(value[key], depth + 1)] : ['unknown_field', 'uninspected']);
  }
  const fingerprint = createHash('sha256').update(JSON.stringify([model, params.action, shape(params.args)])).digest('hex');
  return { model, operation: params.action, fingerprint, tables: database.models[model].tables };
}

// App adapters explicitly report observations. This does not create a cache or inspect its keys/values.
export function createCacheProbe({ id, backend = 'memory', scope = 'process', limits } = {}) {
  if (!safeId(id) || !['memory', 'redis', 'other'].includes(backend) || !['process', 'shared'].includes(scope)) {
    throw Error('invalid_cache');
  }
  if (limits !== undefined && (Object.keys(limits).sort().join(',') !== 'max_bytes,max_entries,ttl_ms' ||
      Object.entries(limits).some(([k, v]) => v !== null && (!finite(v) || v < 1 || (k !== 'ttl_ms' && !count(v)))))) {
    throw Error('invalid_cache_limits');
  }
  let epoch, hits, misses, entries = null, bytes = null, latency, invalid = false;
  function begin() { latency = { count: 0, total_ms: 0, min_ms: Infinity, max_ms: 0 }; }
  function reset() { epoch = randomUUID(); hits = 0; misses = 0; entries = null; bytes = null; invalid = false; begin(); }
  reset();
  const probe = Object.freeze({
    id, backend, scope, ...(limits === undefined ? {} : { limits: Object.freeze({ ...limits }) }),
    lookup(hit, durationMs) {
      if (typeof hit !== 'boolean' || !finite(durationMs) || hits + misses >= 1e9 || latency.total_ms + durationMs > 1e15) {
        invalid = true; return;
      }
      if (hit) hits++; else misses++;
      latency.count++; latency.total_ms += durationMs;
      latency.min_ms = Math.min(latency.min_ms, durationMs); latency.max_ms = Math.max(latency.max_ms, durationMs);
    },
    size(nextEntries, nextBytes) {
      if ((nextEntries !== null && !count(nextEntries)) || (nextBytes !== null && !count(nextBytes))) { invalid = true; return; }
      entries = nextEntries; bytes = nextBytes;
    },
    reset,
    begin,
    snapshot: () => ({ epoch, hits, misses, entries, bytes }),
    distribution: () => latency.count ? { scope: 'lookup', ...latency } : undefined,
    valid: () => !invalid,
  });
  probes.add(probe);
  return probe;
}
