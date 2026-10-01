/* Run the JavaScript engine over cases fed in on stdin and print the results.
 *
 * Driven by tests/test_parity.py, which runs the same cases through the Python
 * engine and compares. Input and output are JSON, one object:
 *
 *   {"cases": [{"text": "...", "settings": {...}}, ...]}
 *   -> {"results": [{"text": "...", "stats": {...}, "spans": [...]}, ...]}
 */

import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

// core.js and edit.js directly rather than index.js: the harness has no use for
// docx.js, which needs browser streams that node does not expose the same way.
const here = dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const engineDir = join(here, '..', 'humanizer', 'webui', 'engine');
const { Humanizer } = await import(pathToFileURL(join(engineDir, 'core.js')).href);
const { applyEditsWithSpans } = await import(pathToFileURL(join(engineDir, 'edit.js')).href);

const payload = JSON.parse(readFileSync(0, 'utf8'));
const results = [];

for (const testCase of payload.cases) {
  const humanizer = new Humanizer(testCase.settings || {});
  const edits = humanizer.plan(testCase.text);
  const { text, spans } = applyEditsWithSpans(testCase.text, edits);
  results.push({ text, stats: humanizer.stats, spans });
}

process.stdout.write(JSON.stringify({ results }));
