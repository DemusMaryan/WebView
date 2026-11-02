import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const modules = process.env.MTW_UI_MODULES;
if (!modules || !process.env.MTW_UI_DIST) throw new Error('Use bash scripts/build-ui (native dependencies)');
export default {
  root,
  cacheDir: process.env.MTW_UI_CACHE,
  resolve: { alias: Object.fromEntries(['react', 'react-dom', 'three'].map(name => [name, path.join(modules, name)])) },
  build: { outDir: process.env.MTW_UI_DIST, emptyOutDir: true, chunkSizeWarningLimit: 650 },
};
