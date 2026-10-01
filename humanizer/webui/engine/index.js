/* The engine, re-exported from one place.
 *
 * This is the whole humanizer running in the browser: no server, no network,
 * no dependencies. The rules are the same ones the Python engine applies —
 * lexicon.js is generated from the Python tables, and tests/test_parity.py
 * asserts the two produce identical output from the same seed.
 */

export { applyEdits, applyEditsWithSpans, dedupe, edit, EditBuffer } from './edit.js';
export { EN, HE } from './lexicon.js';
export { Rng } from './rng.js';
export { HEBREW_KINDS, isHebrew, TypoEngine } from './typos.js';
export {
  BOOLEAN_FIELDS, clampLevel, effectiveTypoRate, endsWithClitic, Humanizer,
  protectedSpans, sentenceStarts, settings,
} from './core.js';
export { humanizeDocx, extractDocxText } from './docx.js';
