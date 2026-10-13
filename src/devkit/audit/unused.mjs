// Trusted bridge: load installed engines only, never inspected-project configuration.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const [eslintRoot, configPath] = process.argv.slice(2);
const requireEngine = createRequire(path.join(eslintRoot, 'package.json'));
const { ESLint } = requireEngine('./lib/api.js');
const parser = requireEngine('@typescript-eslint/parser');
const plugin = requireEngine('@typescript-eslint/eslint-plugin');
const parserVersion = requireEngine('@typescript-eslint/parser/package.json').version;
const pluginVersion = requireEngine('@typescript-eslint/eslint-plugin/package.json').version;
if (!parserVersion.startsWith('8.') || !pluginVersion.startsWith('8.')) {
  throw new Error('Supported TypeScript ESLint parser/plugin major: 8');
}
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
const engine = new ESLint({
  cwd: config.root,
  overrideConfigFile: true,
  ignore: false,
  overrideConfig: [{
    files: ['**/*.{js,jsx,mjs,cjs,ts,tsx,mts,cts}'],
    languageOptions: { parser, ecmaVersion: 'latest', sourceType: 'module', parserOptions: { ecmaFeatures: { jsx: true } } },
    linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
    plugins: { '@typescript-eslint': plugin },
    rules: { '@typescript-eslint/no-unused-vars': ['warn', { args: 'none', caughtErrors: 'none', ignoreRestSiblings: true }] }
  }]
});
const results = await engine.lintFiles(config.files);
process.stdout.write(JSON.stringify({ parserVersion, pluginVersion, results }));
