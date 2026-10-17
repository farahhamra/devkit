import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { startCollector, createCacheProbe, captureHeap } from './collector.mjs';
import { operationShape } from './adapters.mjs';
import { sessionDirectory, writeJson, withinStructureBudget } from './storage.mjs';

const database = { prismaVersion: '5.22.0', models: { Product: { fields: ['id', 'name'], tables: ['products'] } } };
const params = { model: 'Product', action: 'findMany', args: { where: { name: 'PRIVATE_CUSTOMER' } } };
function fixture(t, extra = {}) {
  const workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'devkit-node-'));
  t.after(() => fs.rmSync(workspaceRoot, { recursive: true, force: true }));
  return { enabled: true, workspaceRoot, app: 'erp', environment: 'local', environmentId: 'fixture',
    processScope: 'backend', workload: 'synthetic', durationMs: 1000, sampleIntervalMs: 20, ...extra };
}
const load = (session, name) => JSON.parse(fs.readFileSync(path.join(session.directory, name), 'utf8'));

test('disabled by default and heap requires distinct approval', async () => {
  assert.equal((await startCollector()).status, 'disabled');
  assert.equal((await captureHeap({ enabled: true })).status, 'disabled');
  const off = await startCollector();
  const marker = {};
  assert.equal(off.prismaMiddleware({}, () => marker), marker);
});

test('structure budgets reject oversized or nonfinite documents without slicing', () => {
  assert.equal(withinStructureBudget({ a: [1, 2] }, 4), true);
  assert.equal(withinStructureBudget({ a: [1, 2] }, 3), false);
  assert.equal(withinStructureBudget({ a: Infinity }, 4), false);
});

test('local only, explicit metadata and bounded configuration', async t => {
  const config = fixture(t);
  for (const bad of [{ environment: 'production' }, { durationMs: 0 }, { durationMs: Infinity },
    { maxSamples: 30000 }, { workload: 'private raw text' }, { processScope: undefined }]) {
    assert.equal((await startCollector({ ...config, ...bad })).status, 'unavailable');
  }
  assert.deepEqual(fs.readdirSync(config.workspaceRoot), []);
});

test('runtime/memory are real, bounded, local and stop is idempotent', async t => {
  const session = await startCollector(fixture(t, { cpu: true }));
  t.after(() => session.stop());
  assert.equal(session.status, 'recording');
  assert.equal((await startCollector({ enabled: true })).status, 'already_active');
  await delay(70);
  const result = await session.stop();
  assert.equal(await session.stop(), result);
  assert.equal(result.status, 'completed');
  const runtime = load(session, 'runtime.json'), memory = load(session, 'memory.json');
  assert.equal(runtime.metadata.runtime.version, process.versions.node);
  assert.equal(runtime.capture_version, '2.1');
  assert.ok(runtime.cpu_profile.nodes.length > 0);
  assert.ok(runtime.event_loop.delay.count > 0);
  assert.ok(memory.process_samples.length >= 2);
  assert.ok(memory.process_samples[0].at_ms > 0); // actual offsets, never invented zero
  assert.ok(memory.process_samples.every(s => s.post_gc === false));
  assert.equal(load(session, 'session-complete.json').cache, 'not_configured');
  assert.equal(fs.existsSync(path.join(session.directory, 'cache.json')), false);
  assert.equal(fs.readFileSync(path.join(session.directory, '..', '.gitignore'), 'utf8'), '*\n');
});

test('automatic stop and limits remain explicit', async t => {
  const session = await startCollector(fixture(t, { durationMs: 70, maxSamples: 2, sampleIntervalMs: 10 }));
  t.after(() => session.stop());
  await delay(120);
  assert.equal(session.status, 'stopped');
  const result = await session.stop();
  assert.equal(result.status, 'limited');
  assert.ok(result.collection.dropped_samples > 0);
  assert.equal(load(session, 'memory.json').process_samples.length, 2);
});

test('Prisma observes operations, preserves results/errors, and excludes values', async t => {
  const session = await startCollector(fixture(t, { database }));
  t.after(() => session.stop());
  const response = new EventEmitter(), output = { secret: 'PRIVATE_RESULT' };
  await session.expressMiddleware({}, response, async () => {
    assert.equal(await session.prismaMiddleware(params, async () => output), output);
    response.emit('finish'); response.emit('close');
    await session.prismaMiddleware(params, async () => output); // detached
  });
  const error = new Error('PRIVATE_ERROR');
  await assert.rejects(session.prismaMiddleware(params, async () => { throw error; }), e => e === error);
  await delay(30); await session.stop();
  const data = load(session, 'database.json');
  assert.equal(data.queries.length, 1);
  assert.equal(data.queries[0].timing.count, 3);
  assert.deepEqual(data.queries[0].samples.map(s => s.phase), ['request', 'detached', 'unknown']);
  assert.equal(data.requests.length, 1);
  assert.equal(data.requests[0].outcome, 'finished');
  assert.equal(response.listenerCount('finish'), 0);
  assert.equal(response.listenerCount('close'), 0);
  assert.ok(!JSON.stringify(data).includes('PRIVATE_'));
});

test('operation fingerprints exclude values and limit shape traversal', () => {
  assert.equal(operationShape(params, database).fingerprint,
    operationShape({ ...params, args: { where: { name: 'DIFFERENT_SECRET' } } }, database).fingerprint);
  assert.throws(() => operationShape({ ...params, args: Array(101).fill(1) }, database), /sample_limit/);
  assert.throws(() => operationShape({ ...params, action: 'queryRaw' }, database), /operation_unsupported/);
});

test('an operation straddling response completion is not labelled wholly detached', async t => {
  const session = await startCollector(fixture(t, { database }));
  t.after(() => session.stop());
  const response = new EventEmitter();
  await session.expressMiddleware({}, response, async () => {
    await session.prismaMiddleware(params, async () => { response.emit('finish'); return 1; });
  });
  await session.stop();
  assert.equal(load(session, 'database.json').queries[0].samples[0].phase, 'unknown');
});

test('interruption, outstanding work and unsupported operations are not clean baselines', async t => {
  const session = await startCollector(fixture(t, { database }));
  t.after(() => session.stop());
  let complete;
  const pending = session.prismaMiddleware(params, () => new Promise(resolve => { complete = resolve; }));
  const response = new EventEmitter(); session.expressMiddleware({}, response, () => {});
  assert.equal(await session.prismaMiddleware({ action: 'queryRaw' }, () => 7), 7);
  const result = await session.stop({ interrupted: true }); complete('unchanged'); await pending;
  assert.equal(result.status, 'interrupted');
  assert.ok(result.collection.issues.includes('operation_unfinished'));
  assert.ok(result.collection.issues.includes('request_unfinished'));
  assert.ok(result.collection.issues.includes('operation_unsupported'));
  assert.equal(load(session, 'database.json').queries.length, 0);
  assert.equal(response.listenerCount('finish'), 0);
});

test('cache probes record explicit counters only and preserve unknown sizes', async t => {
  const probe = createCacheProbe({ id: 'catalog', limits: { max_entries: 100, max_bytes: null, ttl_ms: 1000 } });
  probe.lookup(true, 3); // before this window, not part of the recorded latency distribution
  const session = await startCollector(fixture(t, { caches: [probe] }));
  t.after(() => session.stop());
  probe.lookup(true, 1); probe.lookup(false, 2);
  await delay(40); await session.stop();
  const cache = load(session, 'cache.json').caches[0];
  assert.equal(cache.samples[0].hits, 1);
  assert.equal(cache.samples.at(-1).hits, 2);
  assert.equal(cache.samples.at(-1).misses, 1);
  assert.equal(cache.samples.at(-1).bytes, null);
  assert.equal(cache.latency.count, 2);
  assert.equal(cache.latency.total_ms, 3);
});

test('storage never overwrites captures and rejects symlink destinations', t => {
  const config = fixture(t), directory = sessionDirectory(config.workspaceRoot);
  writeJson(directory, 'test.json', { first: true }, 1024);
  assert.throws(() => writeJson(directory, 'test.json', { first: false }, 1024));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(directory, 'test.json'))), { first: true });
  assert.throws(() => writeJson(directory, '../outside.json', {}, 1024));
  const other = fixture(t);
  try { fs.symlinkSync(directory, path.join(other.workspaceRoot, 'diagnostics'), 'junction'); }
  catch (error) { if (error.code === 'EPERM') return t.skip('Windows symlink privilege unavailable'); throw error; }
  assert.throws(() => sessionDirectory(other.workspaceRoot), /unsafe_directory/);
});

test('size limits do not publish truncated JSON', async t => {
  const session = await startCollector(fixture(t, { maxBytes: 1024 }));
  t.after(() => session.stop());
  await delay(30);
  const result = await session.stop();
  assert.equal(result.status, 'limited');
  assert.ok(result.collection.issues.includes('size_limit'));
  assert.deepEqual(result.files, []);
});

test('export failures preserve existing files, never throw, and release the active guard', async t => {
  const config = fixture(t), session = await startCollector(config);
  t.after(() => session.stop());
  fs.writeFileSync(path.join(session.directory, 'runtime.json'), 'existing', { flag: 'wx' });
  await delay(25);
  const result = await session.stop();
  assert.equal(result.status, 'limited');
  assert.equal(fs.readFileSync(path.join(session.directory, 'runtime.json'), 'utf8'), 'existing');
  assert.equal(fs.existsSync(path.join(session.directory, 'session-complete.json')), false);
  const next = await startCollector(config);
  assert.equal(next.status, 'recording');
  await next.stop();
});

test('bad observation input does not replace the application result', async t => {
  const session = await startCollector(fixture(t, { database }));
  t.after(() => session.stop());
  const broken = { get model() { throw null; } };
  assert.equal(await session.prismaMiddleware(broken, () => 123), 123);
  const result = await session.stop();
  assert.equal(result.status, 'limited');
});
