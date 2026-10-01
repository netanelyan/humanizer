/* Rewrite a .docx with the JavaScript engine.
 *
 * Driven by tests/test_parity.py:
 *   node parity_docx.mjs <in.docx> <out.docx> <settings-json>
 * Prints the report as JSON on stdout.
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const engineDir = join(here, '..', 'humanizer', 'webui', 'engine');
const { Humanizer } = await import(pathToFileURL(join(engineDir, 'core.js')).href);
const { humanizeDocx, extractDocxText } = await import(
  pathToFileURL(join(engineDir, 'docx.js')).href);

const [source, destination, settingsJson] = process.argv.slice(2);
const bytes = new Uint8Array(readFileSync(source));

if (destination === '--text') {
  process.stdout.write(JSON.stringify({ text: await extractDocxText(bytes) }));
} else {
  const humanizer = new Humanizer(JSON.parse(settingsJson || '{}'));
  const { file, report } = await humanizeDocx(bytes, humanizer);
  writeFileSync(destination, file);
  process.stdout.write(JSON.stringify(report));
}
