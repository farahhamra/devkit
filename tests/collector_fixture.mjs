// Synthetic local workload for Python/Node interoperability tests; never imports an app.
import { setTimeout as delay } from 'node:timers/promises';
import { startCollector, createCacheProbe } from '../integrations/node/collector.mjs';
const cache = createCacheProbe({ id: 'synthetic', limits: { max_entries: 10, max_bytes: null, ttl_ms: 1000 } });
const collector = await startCollector({
  enabled: true, workspaceRoot: process.argv[2], app: 'erp', environment: 'local', environmentId: 'test',
  processScope: 'backend', workload: 'synthetic', durationMs: 1000, sampleIntervalMs: 20, cpu: true,
  concurrency: 1, warmupMs: 0, caches: [cache],
  database: { prismaVersion: '5.22.0', models: { Product: { fields: ['id'], tables: ['products'] } } },
});
if (collector.status !== 'recording') throw Error('collector did not start');
try {
  cache.lookup(true, 1); cache.lookup(false, 2);
  await collector.prismaMiddleware({ model: 'Product', action: 'findMany', args: { where: { id: 999 } } }, async () => []);
  await delay(80);
} finally {
  process.stdout.write(JSON.stringify(await collector.stop()));
}
