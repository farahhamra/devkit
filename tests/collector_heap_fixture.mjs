// Executed only in a subprocess with a scrubbed environment and synthetic state.
import { captureHeap } from '../integrations/node/collector.mjs';
const result = await captureHeap({ enabled: true, authorized: true, sanitizedData: true,
  workspaceRoot: process.argv[2], app: 'erp', environment: 'local', environmentId: 'test',
  processScope: 'fixture', workload: 'synthetic-heap', maxBytes: Number(process.argv[3]) });
process.stdout.write(JSON.stringify(result));
