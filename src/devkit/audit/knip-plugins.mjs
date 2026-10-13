// Enumerate the installed engine's runtime catalog; published JSON schemas can lag releases.
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const root = process.argv[2];
const { Plugins } = await import(pathToFileURL(path.join(root, 'dist/plugins/index.js')).href);
const { pluginsSchema } = await import(pathToFileURL(path.join(root, 'dist/schema/plugins.js')).href);
const names = Object.keys(Plugins);
if (!names.length || names.some(name => !(name in pluginsSchema.shape))) {
  throw new Error('Cannot disable every installed Knip plugin');
}
process.stdout.write(JSON.stringify(names));
