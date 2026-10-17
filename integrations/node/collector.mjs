// Explicit application-owned collection only; importing this module starts nothing.
import { Session } from 'node:inspector';
import { performance, monitorEventLoopDelay, PerformanceObserver, constants } from 'node:perf_hooks';
import { AsyncLocalStorage } from 'node:async_hooks';
import { getHeapSnapshot } from 'node:v8';
import fs from 'node:fs';
import path from 'node:path';
import { sessionDirectory, writeJson, withinStructureBudget } from './storage.mjs';
import { safeId, finite, count, operationShape, isCacheProbe } from './adapters.mjs';
export { createCacheProbe } from './adapters.mjs';

const ACTIVE = Symbol.for('electroerp.devkit.collector.active');
const VERSION = '0.13.0';
const VERSION_RE = /^[0-9][A-Za-z0-9_.+-]{0,63}$/;
const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_$]{0,127}$/;
const context = new AsyncLocalStorage();
const noop = status => ({ status, directory: null, stop: async () => ({ status }),
  expressMiddleware: (_req, _res, next) => next(), prismaMiddleware: (params, next) => next(params) });
const post = (session, method, parameters = {}) => new Promise((resolve, reject) =>
  session.post(method, parameters, (error, value) => error ? reject(error) : resolve(value)));

function configuration(options) {
  if (!['erp', 'hub'].includes(options.app) || !['local', 'staging'].includes(options.environment) ||
      !safeId(options.workload) || !safeId(options.processScope) || !safeId(options.environmentId) ||
      !safeId(`${options.environment}.${options.environmentId}`)) throw Error('invalid_config');
  const config = { durationMs: 60000, sampleIntervalMs: 1000, maxSamples: 2000,
    maxGroups: 500, maxBytes: 12 * 1024 * 1024, cpu: false, caches: [], ...options };
  for (const [key, low, high] of [['durationMs', 50, 300000], ['sampleIntervalMs', 10, 60000],
    ['maxSamples', 2, 20000], ['maxGroups', 1, 2000], ['maxBytes', 1024, 16 * 1024 * 1024]]) {
    if (!count(config[key]) || config[key] < low || config[key] > high) throw Error('invalid_limit');
  }
  if (typeof config.cpu !== 'boolean' || !Array.isArray(config.caches) || config.caches.length > 100 ||
      config.caches.some(c => !isCacheProbe(c)) ||
      new Set(config.caches.map(c => c.id)).size !== config.caches.length) throw Error('invalid_config');
  if (config.concurrency !== undefined && (!count(config.concurrency) || config.concurrency < 1)) throw Error('invalid_config');
  if (config.warmupMs !== undefined && !finite(config.warmupMs)) throw Error('invalid_config');
  if (config.sourceRevision !== undefined && !/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(config.sourceRevision)) throw Error('invalid_config');
  if (config.database) {
    const db = config.database;
    if (typeof db.prismaVersion !== 'string' || !VERSION_RE.test(db.prismaVersion) || !db.models || typeof db.models !== 'object' ||
        !['unknown', 'mysql', 'mariadb'].includes(db.engine ?? 'unknown') ||
        (db.version != null && (typeof db.version !== 'string' || !VERSION_RE.test(db.version)))) throw Error('invalid_database');
    for (const [model, spec] of Object.entries(db.models)) {
      if (!IDENTIFIER.test(model) || !Array.isArray(spec.fields) || !Array.isArray(spec.tables) ||
          [...spec.fields, ...spec.tables].some(v => typeof v !== 'string' || !IDENTIFIER.test(v)) ||
          new Set(spec.tables).size !== spec.tables.length) throw Error('invalid_database');
    }
    config.database = structuredClone(db);
  }
  return config;
}

export async function startCollector(options = {}) {
  if (options?.enabled !== true) return noop('disabled');
  if (globalThis[ACTIVE]) return noop('already_active');
  let config, directory;
  try { config = configuration(options); directory = sessionDirectory(config.workspaceRoot); }
  catch { return noop('unavailable'); }
  const token = {}; globalThis[ACTIVE] = token;
  const start = performance.now(), capturedAt = new Date().toISOString();
  let recording = false, stopping, timer, sampler, observer, histogram, session, cpuStart, cpuEnd, gcStart;
  let inflight = 0, dropped = 0, eventCount = 0, cacheSampleCount = 0, nextRequest = 0, nextEvent = 0, queryStart;
  const issues = new Set(), memory = [], gc = [], groups = new Map(), requests = [], pendingRequests = new Set();
  const windows = [], cacheRows = new Map();
  const now = () => performance.now() - start;
  const issue = (code, n = 0) => { issues.add(code); dropped += n; };
  const guarded = fn => { try { return fn(); } catch { issue('collector_error'); return undefined; } };
  const append = (array, value) => {
    if (array.length < config.maxSamples) array.push(value); else issue('sample_limit', 1);
  };
  const boundary = (stream, begin, end, type = 'observed') => {
    if (end > begin) windows.push({ stream, start_ms: begin, end_ms: end, boundary: type });
  };
  const collectGc = entries => {
    for (const entry of entries) {
      const timestamp = entry.startTime - start;
      if (timestamp < gcStart) continue;
      const kind = new Map([[constants.NODE_PERFORMANCE_GC_MINOR, 'minor'], [constants.NODE_PERFORMANCE_GC_MAJOR, 'major'],
        [constants.NODE_PERFORMANCE_GC_INCREMENTAL, 'incremental'], [constants.NODE_PERFORMANCE_GC_WEAKCB, 'weakcb']]).get(entry.detail?.kind) ?? 'unknown';
      append(gc, { start_ms: timestamp, duration_ms: entry.duration, kind });
    }
  };
  const sample = () => guarded(() => {
    const m = process.memoryUsage(), stamp = now();
    append(memory, { at_ms: stamp, heap_used_bytes: m.heapUsed, heap_total_bytes: m.heapTotal,
      rss_bytes: m.rss, external_bytes: m.external, array_buffers_bytes: m.arrayBuffers, post_gc: false });
    for (const probe of config.caches) {
      const row = cacheRows.get(probe.id);
      if (row) {
        if (cacheSampleCount >= config.maxSamples) issue('sample_limit', 1);
        else { append(row.samples, { ...probe.snapshot(), at_ms: now() }); cacheSampleCount++; }
      }
    }
  });
  const meta = duration => ({ app: config.app, captured_at: capturedAt,
    runtime: { engine: 'node', version: process.versions.node },
    environment: `${config.environment}.${config.environmentId}`, process_scope: config.processScope,
    collector: `devkit-node-${VERSION}.cpu-${config.cpu ? 'on' : 'off'}.sample-${config.sampleIntervalMs}.db-${config.database ? 'on' : 'off'}.caches-${config.caches.length}`,
    ...(config.sourceRevision ? { source_revision: config.sourceRevision } : {}),
    workload: { name: config.workload, duration_ms: duration,
      ...(config.concurrency === undefined ? {} : { concurrency: config.concurrency }),
      ...(config.warmupMs === undefined ? {} : { warmup_ms: config.warmupMs }) } });

  async function finish({ interrupted = false } = {}) {
    recording = false; clearTimeout(timer); clearInterval(sampler);
    const files = [], startStop = now();
    if (interrupted) issue('interrupted');
    if (inflight) issue('operation_unfinished', inflight);
    if (pendingRequests.size) issue('request_unfinished', pendingRequests.size);
    // Remove only our listeners; never change application response/error listeners.
    for (const pending of pendingRequests) pending.cleanup();
    pendingRequests.clear();
    const runtime = {};
    guarded(() => {
      if (!histogram) return;
      histogram.disable();
      const usage = performance.eventLoopUtilization(histogram.usage), eventEnd = now();
      const count = histogram.count;
      if (count > 0 && Number.isFinite(histogram.mean)) {
        runtime.event_loop = { window_ms: eventEnd - histogram.started, resolution_ms: 10,
          delay: { count, total_ms: histogram.mean * count / 1e6, min_ms: histogram.min / 1e6,
            max_ms: histogram.max / 1e6, p50_ms: histogram.percentile(50) / 1e6,
            p95_ms: histogram.percentile(95) / 1e6, p99_ms: histogram.percentile(99) / 1e6 },
          utilization: { active_ms: usage.active, idle_ms: usage.idle } };
        boundary('event_loop', histogram.started, eventEnd);
      } else issue('event_loop_unavailable');
    });
    if (observer) guarded(() => { collectGc(observer.takeRecords()); observer.disconnect(); runtime.gc = gc; boundary('gc', gcStart, now()); });
    sample();
    if (memory.length >= 2) boundary('process_samples', memory[0].at_ms, memory.at(-1).at_ms);
    for (const probe of config.caches) guarded(() => {
      const row = cacheRows.get(probe.id);
      if (!row) return;
      if (!probe.valid()) issue('collector_error');
      const latency = probe.distribution(); if (latency) row.latency = latency;
    });
    if (config.caches.length) boundary('caches', cacheRows.values().next().value?.samples[0]?.at_ms ?? 0, now());
    if (config.database) boundary('queries', queryStart, startStop);
    if (session) {
      try {
        const { profile } = await post(session, 'Profiler.stop'); cpuEnd = now();
        if (profile.nodes.length > 100000 || (profile.samples?.length ?? 0) > 1000000) issue('sample_limit', 1);
        else runtime.cpu_profile = profile;
        boundary('cpu_profile', cpuStart, cpuEnd, 'control_bounds');
      } catch { issue('cpu_unavailable'); }
      finally { session.disconnect(); }
    }
    const duration = now(), metadata = meta(duration);
    const quality = () => ({ status: interrupted ? 'interrupted' : issues.size || dropped ? 'limited' : 'completed',
      dropped_samples: dropped, issues: [...issues].sort(), windows });
    const captures = [
      ['runtime.json', { capture_version: '2.1', kind: 'runtime', metadata, ...runtime }],
      ['memory.json', { capture_version: '2.1', kind: 'memory', metadata, process_samples: memory }],
    ];
    if (cacheRows.size) captures.push(['cache.json', { capture_version: '2.1', kind: 'cache', metadata, caches: [...cacheRows.values()] }]);
    if (config.database) captures.push(['database.json', { capture_version: '1.1',
      metadata: { ...metadata, runtime: { node: process.versions.node, prisma_client: config.database.prismaVersion },
        database: { engine: config.database.engine ?? 'unknown', version: config.database.version ?? null } },
      queries: [...groups.values()].filter(group => group.timing.count > 0), requests }]);
    // Preflight ALL serialized sizes before publishing so a rejected component marks the others partial.
    const accepted = captures.filter(([, capture]) => {
      const payload = { ...capture, collection: quality() };
      const valueLimit = capture.capture_version === '1.1' ? 250000 : 4000000;
      if (!withinStructureBudget(payload, valueLimit - 2048) || Buffer.byteLength(JSON.stringify(payload)) > config.maxBytes - 4096) {
        issue('size_limit', 1); return false;
      }
      return true;
    });
    try {
      for (const [name, capture] of accepted) files.push(writeJson(directory, name, { ...capture, collection: quality() }, config.maxBytes));
      writeJson(directory, 'session-complete.json', { status: quality().status, files: files.map(f => path.basename(f)),
        cache: config.caches.length ? 'configured' : 'not_configured', collection: quality() }, config.maxBytes);
    } catch { issue('collector_error'); }
    finally { if (globalThis[ACTIVE] === token) delete globalThis[ACTIVE]; }
    return { status: quality().status, directory, files, collection: quality() };
  }
  const stop = options => stopping ??= finish(options).catch(() => {
    recording = false; clearTimeout(timer); clearInterval(sampler);
    try { histogram?.disable(); observer?.disconnect(); session?.disconnect(); } catch { /* best effort */ }
    if (globalThis[ACTIVE] === token) delete globalThis[ACTIVE];
    return { status: 'limited', directory, files: [] };
  });
  try {
    writeJson(directory, 'session-start.json', { status: 'recording', app: config.app, collector: VERSION }, config.maxBytes);
    for (const probe of config.caches) {
      probe.begin();
      cacheRows.set(probe.id, { id: probe.id, backend: probe.backend, scope: probe.scope,
        ...(probe.limits ? { limits: probe.limits } : {}), samples: [] });
    }
    try {
      observer = new PerformanceObserver(list => guarded(() => collectGc(list.getEntries())));
      gcStart = now();
      observer.observe({ entryTypes: ['gc'] });
    } catch { observer = null; issue('gc_unavailable'); }
    try {
      histogram = monitorEventLoopDelay({ resolution: 10 }); histogram.started = now();
      histogram.usage = performance.eventLoopUtilization(); histogram.enable();
    }
    catch { histogram = null; issue('event_loop_unavailable'); }
    if (config.cpu) {
      try {
        session = new Session(); session.connect();
        await post(session, 'Profiler.enable'); await post(session, 'Profiler.setSamplingInterval', { interval: 1000 });
        cpuStart = now(); await post(session, 'Profiler.start');
      } catch { session?.disconnect(); session = null; issue('cpu_unavailable'); }
    }
    sample(); queryStart = now(); recording = true;
    sampler = setInterval(sample, config.sampleIntervalMs); sampler.unref();
    timer = setTimeout(() => { void stop(); }, config.durationMs); timer.unref();
  } catch { issue('collector_error'); await stop(); return noop('unavailable'); }

  return {
    get status() { return recording ? 'recording' : 'stopped'; }, directory, stop,
    expressMiddleware(_req, res, next) {
      if (!recording || !config.database) return next();
      if (pendingRequests.size + requests.length >= config.maxSamples) { issue('sample_limit', 1); return next(); }
      const request = { id: `r${++nextRequest}`, finished: false, session: token }, began = now();
      const cleanup = () => { res.removeListener('finish', onFinish); res.removeListener('close', onClose); };
      const pending = { cleanup };
      function end(outcome) {
        if (request.finished) return;
        request.finished = true; cleanup(); pendingRequests.delete(pending);
        if (recording) append(requests, { id: request.id, duration_ms: now() - began, outcome });
      }
      function onFinish() { end('finished'); }
      function onClose() { end('aborted'); }
      try { res.once('finish', onFinish); res.once('close', onClose); pendingRequests.add(pending); }
      catch { cleanup(); issue('collector_error'); return next(); }
      return context.run(request, next);
    },
    async prismaMiddleware(params, next) {
      if (!recording || !config.database) return next(params);
      let identity;
      try { identity = operationShape(params, config.database); }
      catch (error) { issue(error?.message === 'sample_limit' ? 'sample_limit' : 'operation_unsupported', 1); return next(params); }
      let group = groups.get(identity.fingerprint);
      if (!group) {
        if (groups.size >= config.maxGroups) { issue('sample_limit', 1); return next(params); }
        group = { id: `q${groups.size + 1}`, kind: 'prisma', ...identity,
          timing: { source: 'prisma_operation', count: 0, total_ms: 0, min_ms: Infinity, max_ms: 0 }, samples: [] };
        groups.set(identity.fingerprint, group);
      }
      const began = now(), stored = context.getStore(), request = stored?.session === token ? stored : undefined;
      const startedDetached = request?.finished === true;
      inflight++;
      try { return await next(params); }
      finally {
        inflight--;
        if (recording) guarded(() => {
          const elapsed = now() - began, timing = group.timing;
          timing.count++; timing.total_ms += elapsed;
          timing.min_ms = Math.min(timing.min_ms, elapsed); timing.max_ms = Math.max(timing.max_ms, elapsed);
          if (eventCount >= config.maxSamples) { issue('sample_limit', 1); return; }
          eventCount++;
          group.samples.push({ id: `e${++nextEvent}`, duration_ms: elapsed,
            phase: request ? startedDetached ? 'detached' : request.finished ? 'unknown' : 'request' : 'unknown',
            correlation: request ? 'explicit' : 'unknown', ...(request ? { request_id: request.id } : {}) });
        });
      }
    },
  };
}

// Separate disruptive operation. Never enabled by startCollector, and never concurrent with it.
export async function captureHeap(options = {}) {
  if (options?.enabled !== true || options.authorized !== true || options.sanitizedData !== true) return { status: 'disabled' };
  if (globalThis[ACTIVE]) return { status: 'already_active' };
  let directory, stream, fd;
  const token = {};
  try {
    const config = configuration(options);
    directory = sessionDirectory(config.workspaceRoot); globalThis[ACTIVE] = token;
    writeJson(directory, 'session-start.json', { status: 'recording', kind: 'heap', app: config.app }, config.maxBytes);
    const temporary = path.join(directory, 'heap.heapsnapshot.partial');
    fd = fs.openSync(temporary, 'wx', 0o600);
    // V8 can pause/allocate before emitting chunks; the disk budget is NOT a process-memory bound.
    stream = getHeapSnapshot(); let bytes = 0;
    for await (const chunk of stream) {
      bytes += chunk.length;
      if (bytes > config.maxBytes) throw Error('size_limit');
      let offset = 0;
      while (offset < chunk.length) {
        const written = fs.writeSync(fd, chunk, offset, chunk.length - offset);
        if (written <= 0) throw Error('write_failed');
        offset += written;
      }
    }
    fs.closeSync(fd); fd = undefined;
    const destination = path.join(directory, 'heap.heapsnapshot');
    fs.linkSync(temporary, destination); fs.unlinkSync(temporary);
    writeJson(directory, 'session-complete.json', { status: 'completed', files: ['heap.heapsnapshot'] }, config.maxBytes);
    return { status: 'completed', directory, files: [destination] };
  } catch { stream?.destroy(); return { status: 'limited', directory, files: [] }; }
  finally {
    try { if (fd !== undefined) fs.closeSync(fd); } catch { /* Already reported as limited. */ }
    if (globalThis[ACTIVE] === token) delete globalThis[ACTIVE];
  }
}
