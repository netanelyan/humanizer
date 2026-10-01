/* The slips real typing leaves behind, mirroring humanizer/typos.py.
 *
 * Each word is routed by script, so a mixed document gets the right slips in
 * the right places and never an English mistake on a Hebrew word.
 *
 * A note on the regexes here and in core.js: JavaScript's \b is ASCII-only, so
 * it would treat every Hebrew letter as a non-word character and match in the
 * middle of words. Anywhere a boundary matters these use \p{L} lookarounds with
 * the u flag instead, which is what WB_START and WB_END below are for.
 */

import { dedupe, edit } from './edit.js';
import { HE } from './lexicon.js';

const HEBREW_SKIP_WORDS = new Set(HE.skipWords);

export const HEBREW_RANGE = '\\u0590-\\u05FF';
export const HEBREW_LETTERS = '\\u05D0-\\u05EA';

/** Unicode-aware word boundaries. JS \b cannot do this. */
export const WB_START = '(?<![\\p{L}\\p{N}_])';
export const WB_END = '(?![\\p{L}\\p{N}_])';

// QWERTY neighbours, for the English fat-finger substitutions.
const NEIGHBOURS = {
  a: 'qwsz', b: 'vghn', c: 'xdfv', d: 'serfcx', e: 'wsdr',
  f: 'drtgvc', g: 'ftyhbv', h: 'gyujnb', i: 'ujko', j: 'huikmn',
  k: 'jiolm', l: 'kop;', m: 'njk', n: 'bhjm', o: 'iklp',
  p: 'ol;[', q: 'wa', r: 'edft', s: 'awedxz', t: 'rfgy',
  u: 'yhji', v: 'cfgb', w: 'qase', x: 'zsdc', y: 'tghu',
  z: 'asx',
};

// The Israeli standard layout, by physical key position:
//   ק ר א ט ו ן ם פ
//   ש ד ג כ ע י ח ל ך ף
//   ז ס ב ה נ מ צ ת ץ
const HE_NEIGHBOURS = {
  'ק': 'רדג', 'ר': 'קאגכ', 'א': 'רטכע', 'ט': 'אויע', 'ו': 'טןיח',
  'ן': 'ומחל', 'ם': 'ןפלך', 'פ': 'םךף',
  'ש': 'דזס', 'ד': 'שגקזס', 'ג': 'דכרסב', 'כ': 'גערבה', 'ע': 'כיטהנ',
  'י': 'עחונמ', 'ח': 'ילןמצ', 'ל': 'חךםצת', 'ך': 'לףפתץ', 'ף': 'ךץפ',
  'ז': 'שסד', 'ס': 'זבשדג', 'ב': 'סהדגכ', 'ה': 'בנגכע', 'נ': 'המכעי',
  'מ': 'נצעיח', 'צ': 'מתיחל', 'ת': 'צץחלך', 'ץ': 'תלךף',
};

const FINAL_TO_PLAIN = { 'ם': 'מ', 'ן': 'נ', 'ץ': 'צ', 'ף': 'פ', 'ך': 'כ' };
const HE_PREFIXES = 'ובלכמשה';

const HOMOPHONES = {
  its: ["it's"], "it's": ['its'],
  your: ["you're"], "you're": ['your'],
  their: ['there'], there: ['their'], "they're": ['their'],
  then: ['than'], than: ['then'],
  too: ['to'],
  lose: ['loose'], loose: ['lose'],
  affect: ['effect'], effect: ['affect'],
  "who's": ['whose'], whose: ["who's"],
  alot: ['a lot'],
};

const APOSTROPHE_RE = new RegExp(
  `${WB_START}([A-Za-z]+)['’](ll|re|ve|nt|t|s|d|m)${WB_END}`, 'gu');
const WORD_RE = new RegExp(
  `[A-Za-z]+(?:['’][A-Za-z]+)?|[${HEBREW_RANGE}]+`, 'gu');
const HEBREW_RE = new RegExp(`[${HEBREW_LETTERS}]`, 'u');
export const SENTENCE_START_RE = new RegExp(
  '(?:^|(?<=[.!?]["\'”’)\\]])\\s+|(?<=[.!?])\\s+)(?=\\p{L})', 'gu');

const SKIP_WORDS = new Set(
  ('a an i is it of or to in on at be as by we he do if so no up us my me').split(' '));

// relative weight per kind
const WEIGHTS = [
  // shared
  ['drop_letter', 20.0],
  ['double_space', 14.0],
  ['swap_letters', 9.0],
  ['double_letter', 7.0],
  ['missing_space', 5.0],
  ['dropped_comma', 4.0],
  // English
  ['lowercase_start', 16.0],
  ['key_slip', 12.0],
  ['lost_apostrophe', 7.0],
  ['shift_held', 3.0],
  ['homophone', 3.0],
  // Hebrew
  ['he_final_form', 20.0],
  ['he_key_slip', 14.0],
  ['he_ktiv', 12.0],
  ['he_alef_he', 5.0],
  ['he_prefix_split', 4.0],
];

export const HEBREW_KINDS = new Set(
  WEIGHTS.filter(([k]) => k.startsWith('he_')).map(([k]) => k));

export function isHebrew(text) {
  return HEBREW_RE.test(text);
}

/** Collect all matches of a global regex, as Python's finditer does. */
export function findAll(pattern, text) {
  pattern.lastIndex = 0;
  return [...text.matchAll(pattern)];
}

export function blocked(spans, start, end) {
  for (const [spanStart, spanEnd] of spans) {
    if (spanStart >= end) break;
    if (start < spanEnd && spanStart < end) return true;
  }
  return false;
}

const isUpper = (c) => c !== c.toLowerCase() && c === c.toUpperCase();
const isAlpha = (c) => /\p{L}/u.test(c);

export class TypoEngine {
  constructor(rng, kinds = null) {
    this.rng = rng;
    const allowed = kinds ? new Set(kinds) : null;
    this.weights = WEIGHTS.filter(([k]) => allowed === null || allowed.has(k));
  }

  // -- site collection -------------------------------------------------

  sites(text, blockedSpans, skipBefore) {
    const sites = {};
    for (const [kind] of this.weights) sites[kind] = [];
    const kinds = new Set(Object.keys(sites));

    // match end, not start: the pattern eats the gap before the sentence
    const starts = new Set(findAll(SENTENCE_START_RE, text).map((m) => m.index + m[0].length));

    for (const match of findAll(WORD_RE, text)) {
      const start = match.index;
      const end = start + match[0].length;
      if (start < skipBefore || blocked(blockedSpans, start, end)) continue;
      if (isHebrew(match[0])) {
        this.hebrewSites(sites, kinds, start, end, match[0]);
      } else {
        this.englishSites(sites, kinds, start, end, match[0], starts);
      }
    }

    if (kinds.has('lost_apostrophe')) {
      for (const match of findAll(APOSTROPHE_RE, text)) {
        const start = match.index;
        const end = start + match[0].length;
        if (start >= skipBefore && !blocked(blockedSpans, start, end)) {
          sites.lost_apostrophe.push([start, end, match[0]]);
        }
      }
    }

    if (kinds.has('double_space') || kinds.has('missing_space')) {
      const letter = `A-Za-z${HEBREW_RANGE}`;
      const pattern = new RegExp(`(?<=[${letter},.])( )(?=[${letter}])`, 'gu');
      const trailing = new RegExp(`[${letter}]+$`, 'u');
      const leading = new RegExp(`^[${letter}]+`, 'u');
      for (const match of findAll(pattern, text)) {
        const start = match.index;
        const end = start + 1;
        if (start < skipBefore || blocked(blockedSpans, start, end)) continue;
        if (kinds.has('double_space')) sites.double_space.push([start, end, ' ']);
        const left = trailing.exec(text.slice(0, start));
        const right = leading.exec(text.slice(end));
        if (kinds.has('missing_space') && left && right
            && left[0].length + right[0].length <= 14) {
          sites.missing_space.push([start, end, ' ']);
        }
      }
    }

    if (kinds.has('dropped_comma')) {
      const pattern = new RegExp(`,(?= [a-z${HEBREW_LETTERS}])`, 'gu');
      for (const match of findAll(pattern, text)) {
        const start = match.index;
        if (start >= skipBefore && !blocked(blockedSpans, start, start + 1)) {
          sites.dropped_comma.push([start, start + 1, ',']);
        }
      }
    }

    const out = {};
    for (const [kind, pool] of Object.entries(sites)) if (pool.length) out[kind] = pool;
    return out;
  }

  englishSites(sites, kinds, start, end, word, starts) {
    const lowered = word.toLowerCase();
    const core = word.replace(/['’]/g, '');

    if (starts.has(start) && isUpper(word[0])) {
      if (word !== 'I' && !(word.length > 1 && isUpper(word[1]))) {
        if (kinds.has('lowercase_start')) sites.lowercase_start.push([start, end, word]);
        if (kinds.has('shift_held') && word.length >= 3 && isAlpha(word[1])) {
          sites.shift_held.push([start, end, word]);
        }
      }
    }

    if (lowered in HOMOPHONES && kinds.has('homophone')) {
      sites.homophone.push([start, end, word]);
    }

    if (SKIP_WORDS.has(lowered) || core.length < 4 || word === word.toUpperCase()) return;
    if (word.includes("'") || word.includes('’')) return;

    if (kinds.has('drop_letter') && word.length >= 5) sites.drop_letter.push([start, end, word]);
    if (kinds.has('key_slip')) sites.key_slip.push([start, end, word]);
    if (kinds.has('swap_letters') && word.length >= 5) sites.swap_letters.push([start, end, word]);
    if (kinds.has('double_letter')) sites.double_letter.push([start, end, word]);
  }

  hebrewSites(sites, kinds, start, end, word) {
    if (HEBREW_SKIP_WORDS.has(word) || word.length < 3) return;

    // ‏שלום‎ -> ‏שלומ‎: the single most recognisable Hebrew typing slip
    if (kinds.has('he_final_form') && word[word.length - 1] in FINAL_TO_PLAIN) {
      sites.he_final_form.push([start, end, word]);
    }

    if (kinds.has('he_key_slip')) sites.he_key_slip.push([start, end, word]);

    // a ‏ו‎ or ‏י‎ that full spelling put there and a fast typist leaves out
    if (kinds.has('he_ktiv') && word.length >= 4) {
      if ([...word.slice(1)].some((c) => c === 'ו' || c === 'י')) {
        sites.he_ktiv.push([start, end, word]);
      }
    }

    if (kinds.has('he_alef_he') && word.length >= 4
        && 'אה'.includes(word[word.length - 1])) {
      sites.he_alef_he.push([start, end, word]);
    }

    // ‏וכך‎ -> ‏ו כך‎: the prefix gets its own space by accident
    if (kinds.has('he_prefix_split') && word.length >= 5 && HE_PREFIXES.includes(word[0])) {
      sites.he_prefix_split.push([start, end, word]);
    }

    if (word.length < 4) return;
    if (kinds.has('drop_letter') && word.length >= 5) sites.drop_letter.push([start, end, word]);
    if (kinds.has('swap_letters') && word.length >= 5) sites.swap_letters.push([start, end, word]);
    if (kinds.has('double_letter')) sites.double_letter.push([start, end, word]);
  }

  // -- mutations -------------------------------------------------------

  mutate(kind, word) {
    const rng = this.rng;

    // -- shared --
    if (kind === 'drop_letter') {
      // never the first character: people rarely miss the letter they aimed at
      // first, they miss one in the middle of the run
      const index = rng.randrange(1, word.length - 1);
      return word.slice(0, index) + word.slice(index + 1);
    }

    if (kind === 'double_letter') {
      const index = rng.randrange(1, word.length);
      if (word[index] === word[index - 1]) return null;
      return word.slice(0, index + 1) + word[index] + word.slice(index + 1);
    }

    if (kind === 'swap_letters') {
      const index = rng.randrange(1, word.length - 2);
      if (word[index] === word[index + 1]) return null;
      return word.slice(0, index) + word[index + 1] + word[index] + word.slice(index + 2);
    }

    if (kind === 'double_space') return '  ';
    if (kind === 'missing_space' || kind === 'dropped_comma') return '';

    // -- English --
    if (kind === 'lowercase_start') return word[0].toLowerCase() + word.slice(1);
    if (kind === 'shift_held') return word[0] + word[1].toUpperCase() + word.slice(2);
    if (kind === 'key_slip') return this.slip(word, NEIGHBOURS, true);

    if (kind === 'lost_apostrophe') return word.replace(/['’]/g, '');

    if (kind === 'homophone') {
      const options = HOMOPHONES[word.toLowerCase()];
      if (!options) return null;
      const swap = rng.choice(options);
      return isUpper(word[0]) ? swap[0].toUpperCase() + swap.slice(1) : swap;
    }

    // -- Hebrew --
    if (kind === 'he_final_form') {
      return word.slice(0, -1) + FINAL_TO_PLAIN[word[word.length - 1]];
    }

    if (kind === 'he_key_slip') return this.slip(word, HE_NEIGHBOURS, false);

    if (kind === 'he_ktiv') {
      const options = [];
      for (let i = 1; i < word.length; i++) {
        if (word[i] === 'ו' || word[i] === 'י') options.push(i);
      }
      if (!options.length) return null;
      const index = rng.choice(options);
      const trimmed = word.slice(0, index) + word.slice(index + 1);
      // dropping the letter can strand a final form mid-word
      return trimmed.length >= 3 ? trimmed : null;
    }

    if (kind === 'he_alef_he') {
      return word.slice(0, -1) + (word[word.length - 1] === 'א' ? 'ה' : 'א');
    }

    if (kind === 'he_prefix_split') return `${word[0]} ${word.slice(1)}`;

    return null;
  }

  slip(word, table, cased) {
    const rng = this.rng;
    const options = [];
    for (let i = 1; i < word.length; i++) {
      if (word[i].toLowerCase() in table) options.push(i);
    }
    if (!options.length) return null;
    const index = rng.choice(options);
    const char = word[index];
    let hit = rng.choice([...table[char.toLowerCase()]]);
    if (!isAlpha(hit)) return null;
    if (cased && isUpper(char)) hit = hit.toUpperCase();
    return word.slice(0, index) + hit + word.slice(index + 1);
  }

  // -- planning --------------------------------------------------------

  /** Edits introducing roughly `rate` typos per 1000 words. */
  plan(text, rate, blockedSpans = [], skipFirstSentence = true) {
    if (rate <= 0 || !text.trim()) return [];

    const words = findAll(WORD_RE, text).length;
    if (words < 8) return [];

    const expected = (rate * words) / 1000.0;
    let count = Math.trunc(expected);
    if (this.rng.random() < expected - count) count += 1;
    if (count <= 0) return [];

    let skipBefore = 0;
    if (skipFirstSentence) {
      const starts = findAll(SENTENCE_START_RE, text).map((m) => m.index + m[0].length);
      skipBefore = starts.length > 1 ? starts[1] : 0;
    }

    const sites = this.sites(text, blockedSpans, skipBefore);
    if (!Object.keys(sites).length) return [];

    // Keep the slips apart so they read as scattered rather than a burst of
    // damage in one paragraph.
    const gap = Math.max(28, Math.trunc(text.length / Math.max(count * 1.6, 1)));

    const edits = [];
    const placed = [];
    const kinds = this.weights.filter(([k]) => k in sites).map(([k]) => k);
    const weights = this.weights.filter(([k]) => k in sites).map(([, w]) => w);
    let attempts = 0;
    const limit = count * 25;

    while (edits.length < count && attempts < limit) {
      attempts += 1;
      const kind = this.rng.choices(kinds, weights, 1)[0];
      const pool = sites[kind];
      if (!pool) continue;
      const [start, end, original] = pool[this.rng.randrange(pool.length)];
      if (placed.some((other) => Math.abs(start - other) < gap)) continue;
      const replacement = this.mutate(kind, original);
      if (replacement === null || replacement === original) continue;
      edits.push(edit(start, end, replacement, `typo:${kind}`, original));
      placed.push(start);
    }

    return dedupe(edits);
  }
}
