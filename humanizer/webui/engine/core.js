/* The rewrite engine, mirroring humanizer/core.py.
 *
 * Passes run in this order, each over the output of the last:
 *
 *   1. punctuation   em dashes out, ellipses and smart quotes normalised
 *   2. lexical       phrase and word swaps, contractions
 *   3. hyphens       "long-term" -> "long term", "e-mail" -> "email"
 *   4. seasoning     hedges, sentence openers (level >= 6 only)
 *   5. typos         see typos.js
 *
 * Later passes see the text the earlier ones produced, so a word that only
 * exists because of a phrase swap can still pick up a typo. EditBuffer tracks
 * the composition and collapses the chain back to edits against the original.
 *
 * tests/test_parity.py runs this and the Python engine over the same inputs and
 * seeds and asserts identical output, so any divergence here is a test failure
 * rather than a surprise in the browser.
 */

import { applyEdits, dedupe, edit, EditBuffer } from './edit.js';
import { EN, HE } from './lexicon.js';
import { Rng } from './rng.js';
import {
  blocked, findAll, HEBREW_LETTERS, isHebrew, TypoEngine, WB_END, WB_START,
} from './typos.js';

const APOSTROPHES = "'’ʼ";

/* ------------------------------------------------------------- settings */

const BOOLEAN_FIELDS = [
  'emDashes', 'hyphens', 'contractions', 'lexical', 'seasoning', 'typos',
  'quotes', 'semicolons', 'protectFirstSentence',
];

export function settings(overrides = {}) {
  return {
    level: 5,
    typoRate: null,       // typos per 1000 words; null follows the level
    seed: null,
    emDashes: true,
    hyphens: true,
    contractions: true,
    lexical: true,
    seasoning: true,
    typos: true,
    quotes: true,
    semicolons: true,
    protectFirstSentence: true,
    preserve: [],
    ...overrides,
  };
}

const clampLevel = (level) => Math.max(0, Math.min(10, Number(level) || 0));

function effectiveTypoRate(config) {
  if (config.typoRate !== null && config.typoRate !== undefined) {
    return Math.max(0, Number(config.typoRate));
  }
  const level = clampLevel(config.level);
  if (level <= 0) return 0;
  // ~1.5 slips per 1000 words at level 1, ~11 at level 10
  return 0.4 + 1.05 * level;
}

const probability = (base, slope, level, cap = 1.0) =>
  Math.max(0, Math.min(cap, base + slope * level));

/* ------------------------------------------------- pattern helpers */

function escapeRegex(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Escape one word, tolerating either apostrophe shape. */
function tokenPattern(token) {
  let out = '';
  for (const char of token) {
    out += APOSTROPHES.includes(char) ? `[${escapeRegex(APOSTROPHES)}]` : escapeRegex(char);
  }
  return out;
}

const isWordChar = (c) => /[\p{L}\p{N}_]/u.test(c);

/**
 * Whitespace-flexible, boundary-anchored pattern for a lexicon key.
 *
 * Spaces and tabs only, never a newline: matching across a line break would let
 * the replacement collapse it and silently reflow hard-wrapped text.
 */
function keyPattern(key) {
  const body = key.split(' ').filter(Boolean).map(tokenPattern).join('[ \\t]+');
  const prefix = isWordChar(key[0]) ? WB_START : '';
  const suffix = isWordChar(key[key.length - 1]) ? WB_END : '';
  return prefix + body + suffix;
}

const WS_RE = /\s+/gu;

function normkey(matched) {
  let text = matched.trim().replace(WS_RE, ' ').toLowerCase();
  for (const apostrophe of APOSTROPHES.slice(1)) text = text.split(apostrophe).join("'");
  return text;
}

const isUpper = (c) => c !== c.toLowerCase() && c === c.toUpperCase();

/** Carry the source's capitalisation over to the replacement. */
function matchCase(source, replacement) {
  if (!source || !replacement) return replacement;
  const letters = [...source].filter((c) => /\p{L}/u.test(c));
  if (letters.length > 1 && letters.every(isUpper)) return replacement.toUpperCase();
  if (isUpper(source[0]) && replacement[0] === replacement[0].toLowerCase()
      && replacement[0] !== replacement[0].toUpperCase()) {
    return replacement[0].toUpperCase() + replacement.slice(1);
  }
  return replacement;
}

/* Hebrew attaches ‏ו ה ב ל כ מ ש‎ directly to the word that follows, with no
 * space. A replacement ending in one of them has to close up against the next
 * word or you get ‏אפשר לומר ש מדובר‎, which no one writes. */
const HE_CLITIC_END_RE = /(?:^|\s)([ושבלכמה]{1,2})$/u;
const HE_CLITIC_WORDS = new Set(
  'לו לה לי מה מי בו בה כה שב של כל הם הן שו מו הו וו'.split(' '));

export function endsWithClitic(text) {
  const match = HE_CLITIC_END_RE.exec(text);
  return Boolean(match) && !HE_CLITIC_WORDS.has(match[1]);
}

/* Spans the rewriter must not touch. */
const PROTECT_SOURCES = [
  'https?://\\S+',
  'www\\.[^\\s,;]+',
  `${WB_START}[\\w.+-]+@[\\w-]+\\.[\\w.]+${WB_END}`,
  '```[\\s\\S]*?```',
  '`[^`\\n]+`',
  '\\[[\\d\\s,;–-]+\\]',
  '\\{\\{?[^{}\\n]{1,60}\\}?\\}',
  '%[sdifr]\\b',
  `${WB_START}[A-Z][A-Za-z]*(?:\\.[A-Za-z_]\\w*)+\\(?`,
];
const PROTECT_RE = new RegExp(PROTECT_SOURCES.map((p) => `(?:${p})`).join('|'), 'gsu');

export function protectedSpans(text, extra = []) {
  const spans = findAll(PROTECT_RE, text).map((m) => [m.index, m.index + m[0].length]);
  for (const pattern of extra) {
    try {
      const compiled = new RegExp(pattern, 'gu');
      for (const match of findAll(compiled, text)) {
        spans.push([match.index, match.index + match[0].length]);
      }
    } catch (error) {
      /* an unusable pattern protects nothing rather than breaking the run */
    }
  }
  spans.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return spans;
}

const SENTENCE_START_RE = new RegExp(
  '(?:^|(?<=[.!?]["\'”’)\\]])\\s+|(?<=[.!?])\\s+)(?=\\p{L})', 'gu');

/** The pattern consumes the gap between sentences, so the first letter of the
 *  new sentence is at the end of the match, not its start. */
export function sentenceStarts(text) {
  return findAll(SENTENCE_START_RE, text).map((m) => m.index + m[0].length);
}

/* ------------------------------------------------------------ the engine */

// Words that almost always open a clause rather than an afterthought. A dash in
// front of one is joining two sentences, so it becomes a full stop instead of a
// comma, which would leave a splice.
const CLAUSE_STARTERS = new Set([
  ...'the it this that they we you there these those he she i and but'.split(' '),
  ...'זה זו זאת הם הן הוא היא אני אנחנו אתה יש אין כל וזה אבל וגם'.split(' '),
]);

/** level|lexical|contractions -> {table, regex}. See buildTable. */
const TABLE_CACHE = new Map();

export class Humanizer {
  constructor(config = {}) {
    this.settings = settings(config);
    this.rng = new Rng(this.settings.seed);
    this.typoEngine = new TypoEngine(this.rng);
    this.lastOpener = '';
    this.recent = [];     // spans paragraphs; .docx plans each alone
    this.stats = {};
    this.buildTable();
  }

  /* -- table ---------------------------------------------------------- */

  /**
   * Both languages go in one table. Hebrew and Latin keys cannot collide — the
   * scripts share no letters — so there is no language to detect and a document
   * that mixes the two gets each phrase handled by its own script's rules.
   */
  buildTable() {
    const config = this.settings;
    const level = clampLevel(config.level);

    // Compiling ~950 alternatives is the expensive part of construction, and
    // the page builds a Humanizer on every keystroke. Only these three inputs
    // change the table, so cache on them.
    const cacheKey = `${level}|${config.lexical}|${config.contractions}`;
    const cached = TABLE_CACHE.get(cacheKey);
    if (cached) {
      this.table = cached.table;
      this.regex = cached.regex;
      return;
    }

    const table = new Map();

    const add = (key, options, kind, overwrite = true) => {
      // An option identical to the key would be a no-op edit; drop it rather
      // than let it burn a probability roll.
      const choices = options.filter((option) => option !== key);
      if (!choices.length) return;
      if (overwrite || !table.has(key)) table.set(key, { options: choices, kind });
    };

    const forLevel = (tiers) => {
      const merged = new Map();
      for (const [minLevel, pairs] of tiers) {
        if (level < minLevel) continue;
        for (const [key, options] of pairs) merged.set(key, options);
      }
      return merged;
    };

    if (config.lexical) {
      for (const source of [EN, HE]) {
        for (const [key, options] of forLevel(source.phraseTiers)) add(key, options, 'phrase');
        for (const [key, options] of forLevel(source.wordTiers)) add(key, options, 'word', false);
      }
    }
    if (config.contractions && level >= 2) {
      for (const [key, value] of EN.contractions) add(key, [value], 'contraction');
    }

    this.table = table;
    if (!table.size) {
      this.regex = null;
    } else {
      const keys = [...table.keys()].sort(
        (a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0));
      this.regex = new RegExp(keys.map((k) => `(?:${keyPattern(k)})`).join('|'), 'giu');
    }
    TABLE_CACHE.set(cacheKey, { table: this.table, regex: this.regex });
  }

  /* -- punctuation ---------------------------------------------------- */

  passPunctuation(text, spans) {
    const config = this.settings;
    const edits = [];

    if (config.emDashes) edits.push(...this.dashEdits(text, spans));

    if (config.quotes) {
      const straight = {
        '“': '"', '”': '"', '‘': "'",
        '’': "'", '′': "'", '«': '"', '»': '"',
      };
      for (let index = 0; index < text.length; index++) {
        const char = text[index];
        if (char in straight && !blocked(spans, index, index + 1)) {
          edits.push(edit(index, index + 1, straight[char], 'quote', char));
        }
      }
      for (const match of findAll(/…/gu, text)) {
        if (!blocked(spans, match.index, match.index + 1)) {
          edits.push(edit(match.index, match.index + 1, '...', 'quote', '…'));
        }
      }
    }

    if (config.semicolons) {
      const chance = probability(0.10, 0.045, clampLevel(config.level), 0.55);
      for (const match of findAll(/;[ \t]+([a-z])/gu, text)) {
        const start = match.index;
        const end = start + match[0].length;
        if (blocked(spans, start, end)) continue;
        if (this.rng.random() < chance) {
          edits.push(edit(start, end, `. ${match[1].toUpperCase()}`, 'semicolon', match[0]));
        }
      }
    }

    return dedupe(edits);
  }

  /**
   * Take every em dash out without breaking the sentence around it.
   *
   * A pair of dashes bracketing an aside has to be treated as a pair. Turning
   * the first into a comma and the second into a full stop is how you end up
   * with a stranded fragment, so pairs are resolved together.
   */
  dashEdits(text, spans) {
    // [ \t] rather than \s so a dash at the end of a line does not take the
    // line break with it and join two wrapped lines together
    const matches = findAll(/[ \t]*(?:[—–]|--)[ \t]*/gu, text)
      .filter((m) => !blocked(spans, m.index, m.index + m[0].length));
    if (!matches.length) return [];

    const bounds = [...sentenceStarts(text), text.length];
    const pairs = [];
    for (let index = 0; index < bounds.length - 1; index++) {
      const low = bounds[index];
      const high = bounds[index + 1];
      const inside = matches.filter((m) => m.index >= low && m.index < high);
      for (let position = 0; position + 1 < inside.length; position += 2) {
        const opening = inside[position];
        const closing = inside[position + 1];
        // both must have text on the far side to be a real parenthetical
        const after = text.slice(closing.index + closing[0].length, high);
        if (opening.index > low && after.trim()) {
          pairs.push([opening.index, closing.index]);
        }
      }
    }

    const bracketed = new Map();
    for (const [opening, closing] of pairs) {
      const style = this.rng.random() < 0.75 ? ', ' : 'parens';
      bracketed.set(opening, [style, true]);
      bracketed.set(closing, [style, false]);
    }

    const edits = [];
    for (const match of matches) {
      const start = match.index;
      const end = start + match[0].length;
      const whole = match[0];
      const before = text.slice(0, start).replace(/\s+$/u, '');
      const after = text.slice(end);

      // A dash between two numbers is a range, leave it as a hyphen.
      const beforeChar = before.slice(-1);
      if (/\d/.test(beforeChar) && /\d/.test(after.slice(0, 1))) {
        if (whole !== '-') edits.push(edit(start, end, '-', 'dash', whole));
        continue;
      }
      if (!after.trim()) {
        edits.push(edit(start, end, '', 'dash', whole));
        continue;
      }

      if (bracketed.has(start)) {
        const [style, opening] = bracketed.get(start);
        const replacement = style === 'parens' ? (opening ? ' (' : ') ') : ', ';
        edits.push(edit(start, end, replacement, 'dash', whole));
        continue;
      }

      const word = new RegExp(`^([A-Za-z']+|[${HEBREW_LETTERS}]+)`, 'u').exec(after);
      const clause = Boolean(word) && CLAUSE_STARTERS.has(word[1].toLowerCase());
      const roll = this.rng.random();
      if (clause && roll < 0.70 && /\p{L}/u.test(after[0])) {
        // two independent clauses: a comma here would be a splice
        edits.push(edit(start, end + 1, `. ${after[0].toUpperCase()}`,
          'dash', text.slice(start, end + 1)));
      } else if (roll < 0.80) {
        edits.push(edit(start, end, ', ', 'dash', whole));
      } else if (roll < 0.92) {
        edits.push(edit(start, end, ' ', 'dash', whole));
      } else {
        edits.push(edit(start, end, ': ', 'dash', whole));
      }
    }
    return edits;
  }

  /* -- lexical -------------------------------------------------------- */

  passLexical(text, spans) {
    if (!this.regex) return [];
    const level = clampLevel(this.settings.level);
    const rates = {
      phrase: probability(0.34, 0.068, level),
      word: probability(0.20, 0.070, level),
      contraction: probability(0.28, 0.065, level),
    };

    const starts = new Set(sentenceStarts(text));
    const edits = [];
    const used = new Map();

    for (const match of findAll(this.regex, text)) {
      const start = match.index;
      const end = start + match[0].length;
      if (blocked(spans, start, end)) continue;
      const key = normkey(match[0]);
      const entry = this.table.get(key);
      if (!entry) continue;
      if (this.rng.random() >= rates[entry.kind]) continue;

      // Prefer a different wording than last time this key came up, so a
      // repeated word does not get the same substitute every paragraph.
      let choices = entry.options.filter((option) => option !== used.get(key));
      if (!choices.length) choices = entry.options;
      // And avoid whatever was used very recently for *any* key, or two
      // different phrases both land on "in short" in adjacent sentences.
      let fresh = choices.filter((option) => !this.recent.includes(option));
      if (!fresh.length) fresh = choices;
      const replacement = this.rng.choice(fresh);
      used.set(key, replacement);
      this.recent.push(replacement);
      if (this.recent.length > 6) this.recent.splice(0, this.recent.length - 6);

      let item;
      if (!replacement) {
        item = this.deletion(text, start, end, starts, entry.kind);
      } else {
        const replaced = matchCase(match[0], replacement);
        if (replaced === match[0] || this.doublesAWord(text, end, replaced)) continue;
        let stop = end;
        if (endsWithClitic(replaced) && text[end] === ' ') stop += 1;
        item = edit(start, stop, replaced, entry.kind, text.slice(start, stop));
      }
      if (item) edits.push(item);
    }
    return dedupe(edits);
  }

  /**
   * Cut a phrase out without leaving a hole where it stood.
   *
   * Deleting "it is important to note that " has to take the space after it as
   * well, and if the phrase opened the sentence the next word has to pick up
   * the capital it was carrying.
   */
  deletion(text, start, end, starts, kind) {
    let stop = end;
    let from = start;
    while (stop < text.length && text[stop] === ' ') stop += 1;
    let replacement = '';
    if (starts.has(start) && stop < text.length && /\p{L}/u.test(text[stop])) {
      replacement = text[stop].toUpperCase();
      stop += 1;
    }
    if (stop >= text.length && !replacement) {
      // nothing follows; drop the space in front of the phrase instead
      while (from > 0 && text[from - 1] === ' ') from -= 1;
    }
    if (from === end && end === stop) return null;
    return edit(from, stop, replacement, kind, text.slice(from, stop));
  }

  /** Guard against "many of" -> "a lot of" landing in front of "of". */
  doublesAWord(text, end, replacement) {
    const parts = replacement.split(' ');
    const tail = parts[parts.length - 1].toLowerCase();
    if (!tail) return false;
    const following = /^\s+([A-Za-z']+|[\p{L}]+)/u.exec(text.slice(end));
    return Boolean(following) && following[1].toLowerCase() === tail;
  }

  /* -- hyphens -------------------------------------------------------- */

  passHyphens(text, spans) {
    if (!this.settings.hyphens) return [];
    const level = clampLevel(this.settings.level);
    const splitChance = probability(0.04, 0.030, level, 0.34);
    const joinChance = probability(0.25, 0.045, level, 0.70);
    const keepEn = new Set(EN.hyphenKeep);
    const joinEn = new Map(EN.hyphenJoin);
    const keepHe = new Set(HE.hyphenKeep);
    const edits = [];

    // Hebrew joins compounds with a maqaf as well as a plain hyphen, and
    // loosening those reads exactly the same way: ‏בית-ספר‎ -> ‏בית ספר‎.
    const pattern = new RegExp(
      `${WB_START}([A-Za-z]{1,20})-([A-Za-z]{2,20})${WB_END}`
      + `|([${HEBREW_LETTERS}]{2,20})[-־]([${HEBREW_LETTERS}]{2,20})`, 'gu');

    for (const match of findAll(pattern, text)) {
      const start = match.index;
      const end = start + match[0].length;
      if (blocked(spans, start, end)) continue;
      const whole = match[0];
      const lowered = whole.toLowerCase();

      if (match[3]) {              // Hebrew: no prefix closing, always a space
        if (keepHe.has(lowered)) continue;
        if (this.rng.random() < splitChance) {
          edits.push(edit(start, end, `${match[3]} ${match[4]}`, 'hyphen', whole));
        }
        continue;
      }

      // "check-ins" is the same compound as "check-in"
      const singular = lowered.endsWith('s') ? lowered.slice(0, -1) : null;
      if (keepEn.has(lowered) || (singular && keepEn.has(singular))) continue;
      let closed = joinEn.get(lowered);
      if (closed === undefined && singular && joinEn.has(singular)) {
        closed = `${joinEn.get(singular)}s`;
      }
      if (closed !== undefined) {
        if (this.rng.random() < joinChance) {
          edits.push(edit(start, end, matchCase(whole, closed), 'hyphen', whole));
        }
        continue;
      }
      if (this.rng.random() >= splitChance) continue;
      const left = match[1];
      // "e-mail" shape: a single-letter prefix closes up, it does not become
      // two words
      const replacement = left.length === 1 ? left + match[2] : `${left} ${match[2]}`;
      edits.push(edit(start, end, replacement, 'hyphen', whole));
    }
    return dedupe(edits);
  }

  /* -- seasoning ------------------------------------------------------ */

  passSeasoning(text, spans) {
    const level = clampLevel(this.settings.level);
    if (!this.settings.seasoning || level < 6) return [];
    const openerChance = probability(-0.34, 0.070, level, 0.28);
    const hedgeChance = probability(-0.30, 0.060, level, 0.30);
    const edits = [];

    const starts = sentenceStarts(text);
    let lastOpenerIndex = -99;
    for (let index = 0; index < starts.length; index++) {
      if (index === 0) continue;             // never reword the opening sentence
      if (index - lastOpenerIndex < 3) continue;  // keep them scattered
      const position = starts[index];
      if (blocked(spans, position, position + 1)) continue;

      const stop = index + 1 < starts.length ? starts[index + 1] : text.length;
      const sentence = text.slice(position, stop);
      const hebrew = isHebrew(sentence.slice(0, 60));
      const source = hebrew ? HE : EN;

      const head = sentence.slice(0, 40).toLowerCase();
      // already has an opener, do not stack one
      if (source.connectiveStarts.some((start) => head.startsWith(start))) continue;
      if (sentence.split(/\s+/u).filter(Boolean).length < 6) continue;
      if (this.rng.random() >= openerChance) continue;

      const match = new RegExp(`^(?:[A-Za-z]+|[${HEBREW_LETTERS}]+)`, 'u').exec(sentence);
      if (!match) continue;
      const first = match[0];
      // "I", acronyms and proper nouns keep their case
      if (!hebrew && (first === 'I' || (first.length > 1 && isUpper(first[1])))) continue;

      // kept on the instance so the same opener does not turn up again in the
      // next paragraph of a .docx, which is planned separately
      let choices = source.openers.filter((option) => option !== this.lastOpener);
      if (!choices.length) choices = source.openers;
      const opener = this.rng.choice(choices);
      this.lastOpener = opener;
      // Hebrew has no capitals, so there is nothing to fold down
      const replacement = opener
        + (hebrew ? first : first[0].toLowerCase() + first.slice(1));
      edits.push(edit(position, position + first.length, replacement, 'opener', first));
      lastOpenerIndex = index;
    }

    // The d flag gives match.indices, the equivalent of Python's match.span(1),
    // so the hedge goes exactly in front of the adjective rather than at an
    // offset worked out by hand.
    const english = new RegExp(
      `${WB_START}(?:is|are|was|were|seems?|looks?|feels?)\\s+([a-z]{4,})${WB_END}`, 'gdu');
    // Hebrew has no copula, so the slot is after the pronoun instead:
    // ‏זה חשוב‎ -> ‏זה די חשוב‎
    const hebrewPattern = new RegExp(
      `${WB_START}(?:הוא|היא|זה|זו|הם|הן|היה|הייתה)\\s+`
      + `([${HEBREW_LETTERS}]{4,})${WB_END}`, 'gdu');

    for (const pattern of [english, hebrewPattern]) {
      for (const match of findAll(pattern, text)) {
        const [start, end] = match.indices[1];
        if (blocked(spans, start, end)) continue;
        // "are honestly primarily carried out" — one qualifier is enough
        if (match[1].endsWith('ly')) continue;
        if (this.rng.random() >= hedgeChance) continue;
        const source = isHebrew(match[1]) ? HE : EN;
        const hedge = this.rng.choice(source.hedges);
        edits.push(edit(start, start, `${hedge} `, 'hedge', ''));
      }
    }

    return dedupe(edits);
  }

  /* -- entry points --------------------------------------------------- */

  countEdits(edits) {
    for (const item of edits) {
      this.stats[item.kind] = (this.stats[item.kind] || 0) + 1;
    }
  }

  /** Edits that turn `text` into its humanized form. */
  plan(text) {
    this.stats = {};
    if (!text.trim()) return [];
    const level = clampLevel(this.settings.level);
    if (level <= 0 && !this.settings.typos) return [];

    const buffer = new EditBuffer(text);
    const extra = this.settings.preserve || [];

    if (level > 0) {
      const passes = [
        (t, s) => this.passPunctuation(t, s),
        (t, s) => this.passLexical(t, s),
        (t, s) => this.passHyphens(t, s),
        (t, s) => this.passSeasoning(t, s),
      ];
      for (const pass of passes) {
        const current = buffer.text();
        const edits = pass(current, protectedSpans(current, extra));
        if (edits.length) {
          this.countEdits(edits);
          buffer.apply(edits);
        }
      }
    }

    if (this.settings.typos) {
      const current = buffer.text();
      const edits = this.typoEngine.plan(
        current,
        effectiveTypoRate(this.settings),
        protectedSpans(current, extra),
        this.settings.protectFirstSentence,
      );
      if (edits.length) {
        this.countEdits(edits);
        buffer.apply(edits);
      }
    }

    return buffer.edits();
  }

  run(text) {
    return applyEdits(text, this.plan(text));
  }
}

export { BOOLEAN_FIELDS, clampLevel, effectiveTypoRate };
