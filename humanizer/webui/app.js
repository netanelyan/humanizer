/* humanizer web ui
 *
 * Everything runs here. No server, no network, no dependencies: the engine in
 * ./engine is the same set of rules the Python command line applies, with the
 * lexicons generated from the Python source and a parity test asserting the two
 * produce identical output from the same seed.
 *
 * Highlighting uses the span list the engine returns rather than diffing two
 * strings, so every mark is exactly one rule firing, with its kind and the
 * original text attached.
 */

import {
  applyEditsWithSpans, humanizeDocx, Humanizer,
} from './engine/index.js';

const $ = (id) => document.getElementById(id);

const el = {
  level: $('level'), levelValue: $('level-value'), levelCaption: $('level-caption'),
  typos: $('typos'), typosValue: $('typos-value'),
  seed: $('seed'), reroll: $('reroll'), preserve: $('preserve'),
  input: $('input'), output: $('output'),
  inCount: $('in-count'), status: $('status'),
  highlight: $('highlight'), copy: $('copy'), download: $('download'),
  clear: $('clear'), sample: $('sample'), theme: $('theme'),
  stats: $('stats'), statsList: $('stats-list'),
  statsTotal: $('stats-total'), statsWords: $('stats-words'),
  docx: $('docx'), docxName: $('docx-name'), docxSummary: $('docx-summary'),
  docxRun: $('docx-run'), docxDrop: $('docx-drop-file'), docxChanges: $('docx-changes'),
  drop: $('drop'), toast: $('toast'),
};

const LEVEL_CAPTIONS = [
  'Nothing is changed.',
  'Em dashes out, quotes straightened, the worst filler cut.',
  'Em dashes out, quotes straightened, the worst filler cut.',
  'Plainer wording. <em>utilize</em> becomes <em>use</em>, contractions appear.',
  'Plainer wording, more of it. Casual synonyms throughout.',
  'Balanced. Reads noticeably less stiff but keeps your voice.',
  'Looser. <em>individuals</em> becomes <em>people</em>, sentences relax.',
  'Spoken register. Hedges and the occasional sentence opener.',
  'Strong. Most formal phrasing is replaced.',
  'Heavy. Very little formal wording survives.',
  'Maximum. Everything the lexicon knows, applied aggressively.',
];

const LABELS = {
  dash: 'em dashes removed', quote: 'quotes straightened', semicolon: 'semicolons split',
  phrase: 'phrases reworded', word: 'words swapped', contraction: 'contractions',
  hyphen: 'hyphens loosened', opener: 'sentence openers', hedge: 'hedges added',
  'typo:drop_letter': 'missing letters', 'typo:lowercase_start': 'lowercase sentence starts',
  'typo:double_space': 'double spaces', 'typo:key_slip': 'keyboard slips',
  'typo:swap_letters': 'swapped letters', 'typo:double_letter': 'doubled letters',
  'typo:lost_apostrophe': 'dropped apostrophes', 'typo:missing_space': 'run-together words',
  'typo:dropped_comma': 'dropped commas', 'typo:shift_held': 'shift held too long',
  'typo:homophone': 'homophone slips',
  'typo:he_final_form': 'final letters not final', 'typo:he_key_slip': 'Hebrew keyboard slips',
  'typo:he_ktiv': 'dropped vav or yod', 'typo:he_alef_he': 'alef/he endings',
  'typo:he_prefix_split': 'split prefixes',
};

/* The checkboxes carry the engine's Python-side names, which are also what the
 * documented HTTP API accepts. Two of them differ in the JavaScript engine. */
const SETTING_KEY = {
  em_dashes: 'emDashes',
  protect_first_sentence: 'protectFirstSentence',
};

/* Two samples, cycled by the Sample button. The Hebrew one is there because the
 * engine handles both, and it is the quickest way to see that. */
const SAMPLES = [
  `In today's fast-paced world, it is important to note that organisations \
must utilize a wide range of digital tools in order to facilitate collaboration across \
distributed teams — the long-term benefits are substantial. Furthermore, these solutions \
demonstrate a comprehensive approach that plays a crucial role in maintaining productivity.

One of the most significant obstacles is the potential for isolation. Therefore, it is \
essential to foster a culture that prioritizes regular check-ins and well-being.`,

  `בעולם המהיר של ימינו, יש לציין כי ארגונים נדרשים לעשות שימוש במגוון רחב של כלים \
דיגיטליים על מנת לאפשר שיתוף פעולה בין צוותים מרוחקים — היתרונות בטווח הארוך הם משמעותיים. \
יתרה מכך, פתרונות אלה מדגימים גישה מקיפה אשר ממלאת תפקיד מרכזי בשמירה על הפרודוקטיביות.

לסיכום, ניתן לומר כי מדובר באפשרות טובה עבור ארגונים רבים.`,
];
let sampleIndex = 0;

let timer = null;
let lastResult = '';
let docxFile = null;         // { name, bytes }

/* ------------------------------------------------------------ helpers */

function toast(message, tone) {
  el.toast.textContent = message;
  el.toast.dataset.tone = tone || 'ok';
  el.toast.hidden = false;
  clearTimeout(toast.handle);
  toast.handle = setTimeout(() => { el.toast.hidden = true; }, 2600);
}

const setStatus = (text) => { el.status.textContent = text; };

function countWords(text) {
  const found = text.match(/[A-Za-z']+|[֐-׿]+/gu);
  return found ? found.length : 0;
}

/* ------------------------------------------------------------ settings */

function readSettings() {
  const config = { level: Number(el.level.value) };

  const typos = Number(el.typos.value);
  if (typos >= 0) config.typoRate = typos;

  if (el.seed.value !== '') config.seed = Number(el.seed.value);

  document.querySelectorAll('[data-setting]').forEach((box) => {
    const name = box.dataset.setting;
    config[SETTING_KEY[name] || name] = box.checked;
  });

  const preserve = el.preserve.value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
  if (preserve.length) config.preserve = preserve;

  return config;
}

function paintSliders() {
  const level = Number(el.level.value);
  el.levelValue.textContent = level;
  el.levelCaption.innerHTML = LEVEL_CAPTIONS[level] || '';

  const typos = Number(el.typos.value);
  el.typosValue.textContent = typos < 0 ? 'auto' : (typos === 0 ? 'none' : `${typos} / 1k`);
}

/* ------------------------------------------------------------ rendering */

function render(text, spans, stats, words) {
  lastResult = text;
  const fragment = document.createDocumentFragment();
  let cursor = 0;

  for (const span of spans) {
    if (span.start > cursor) fragment.append(text.slice(cursor, span.start));
    if (span.end > span.start) {
      const mark = document.createElement('mark');
      mark.dataset.kind = span.kind;
      mark.title = describe(span);
      mark.textContent = text.slice(span.start, span.end);
      fragment.append(mark);
    } else if (span.old) {
      // a deletion: nothing left to highlight, so leave a marker
      const gone = document.createElement('del');
      gone.title = describe(span);
      fragment.append(gone);
    }
    cursor = Math.max(cursor, span.end);
  }
  fragment.append(text.slice(cursor));

  el.output.replaceChildren(fragment);
  renderStats(stats, words);
}

function describe(span) {
  const label = LABELS[span.kind] || span.kind;
  const old = (span.old || '').trim();
  if (span.end === span.start) return `removed “${old}”  ·  ${label}`;
  if (old) return `was “${old}”  ·  ${label}`;
  return `added  ·  ${label}`;
}

function renderStats(stats, words) {
  const entries = Object.entries(stats || {}).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);

  el.stats.hidden = total === 0;
  el.statsTotal.textContent = `${total} ${total === 1 ? 'edit' : 'edits'}`;
  el.statsWords.textContent = words ? `${words} words in` : '';

  el.statsList.replaceChildren(...entries.map(([kind, count]) => {
    const li = document.createElement('li');
    const b = document.createElement('b');
    b.textContent = count;
    li.append(b, LABELS[kind] || kind);
    return li;
  }));
}

/* ------------------------------------------------------------ the work */

function run() {
  const text = el.input.value;
  el.inCount.textContent = `${countWords(text)} words`;

  if (!text.trim()) {
    el.output.replaceChildren();
    el.stats.hidden = true;
    lastResult = '';
    setStatus('ready');
    return;
  }

  try {
    const humanizer = new Humanizer(readSettings());
    const edits = humanizer.plan(text);
    const result = applyEditsWithSpans(text, edits);
    render(result.text, result.spans, humanizer.stats, countWords(text));
    setStatus(`level ${Number(el.level.value)}`);
  } catch (error) {
    setStatus('error');
    toast(error.message, 'bad');
    throw error;
  }
}

function schedule(delay = 200) {
  clearTimeout(timer);
  timer = setTimeout(run, delay);
}

/* ------------------------------------------------------------ files */

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function outputName(name) {
  const base = (name || 'document.docx').replace(/^.*[\\/]/, '');
  const dot = base.lastIndexOf('.');
  const stem = dot > 0 ? base.slice(0, dot) : base;
  const extension = dot > 0 ? base.slice(dot) : '.docx';
  return `${stem}.humanized${/\.docm?$/i.test(extension) ? extension : '.docx'}`;
}

async function takeFile(file) {
  const name = file.name || 'document';
  if (/\.docx$|\.docm$/i.test(name)) {
    docxFile = { name, bytes: new Uint8Array(await file.arrayBuffer()) };
    el.docxName.textContent = name;
    el.docxSummary.textContent = `${(docxFile.bytes.length / 1024).toFixed(0)} KB`;
    el.docxChanges.replaceChildren();
    el.docx.hidden = false;
    toast('Document loaded. Set the level, then humanize it.');
    return;
  }
  if (/\.doc$/i.test(name)) {
    toast('Old .doc files are not supported, save it as .docx first', 'bad');
    return;
  }
  el.input.value = await file.text();
  schedule(0);
}

async function runDocx() {
  if (!docxFile) return;
  el.docxRun.disabled = true;
  setStatus('rewriting document');
  try {
    const humanizer = new Humanizer(readSettings());
    const { file, report } = await humanizeDocx(docxFile.bytes, humanizer);

    saveBlob(new Blob([file], {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    }), outputName(docxFile.name));

    el.docxSummary.textContent =
      `${report.rewritten} of ${report.paragraphs} paragraphs touched, ${report.words} words`;
    renderStats(report.stats, report.words);
    el.stats.hidden = false;
    renderDocxChanges(report);
    setStatus('saved');
    toast(`Saved ${outputName(docxFile.name)}`);
  } catch (error) {
    setStatus('error');
    toast(error.message, 'bad');
  } finally {
    el.docxRun.disabled = false;
  }
}

function renderDocxChanges(report) {
  const shown = report.changes.slice(0, 60);
  const nodes = shown.map((change) => {
    const box = document.createElement('div');
    box.className = 'docx-change';
    const before = document.createElement('div');
    before.className = 'before';
    before.textContent = change.before;
    const after = document.createElement('div');
    after.className = 'after';
    after.textContent = change.after;
    box.append(before, after);
    return box;
  });
  if (report.changes.length > shown.length) {
    const more = document.createElement('p');
    more.className = 'caption';
    more.textContent = 'Showing the first 60 changed paragraphs.';
    nodes.push(more);
  }
  el.docxChanges.replaceChildren(...nodes);
}

/* ------------------------------------------------------------ wiring */

el.level.addEventListener('input', () => { paintSliders(); schedule(80); });
el.typos.addEventListener('input', () => { paintSliders(); schedule(80); });
el.seed.addEventListener('input', () => schedule(250));
el.preserve.addEventListener('input', () => schedule(400));
el.input.addEventListener('input', () => schedule());

document.querySelectorAll('[data-setting]').forEach((box) => {
  box.addEventListener('change', () => schedule(0));
});

el.reroll.addEventListener('click', () => {
  el.seed.value = Math.floor(Math.random() * 100000);
  schedule(0);
});

el.highlight.addEventListener('change', () => {
  document.body.classList.toggle('plain', !el.highlight.checked);
});

el.copy.addEventListener('click', async () => {
  if (!lastResult) return;
  try {
    await navigator.clipboard.writeText(lastResult);
    toast('Copied');
  } catch (error) {
    toast('Could not copy, select the text instead', 'bad');
  }
});

el.download.addEventListener('click', () => {
  if (!lastResult) return;
  saveBlob(new Blob([lastResult], { type: 'text/plain;charset=utf-8' }), 'humanized.txt');
});

el.clear.addEventListener('click', () => {
  el.input.value = '';
  el.input.focus();
  schedule(0);
});

el.sample.addEventListener('click', () => {
  el.input.value = SAMPLES[sampleIndex % SAMPLES.length];
  sampleIndex++;
  schedule(0);
});

el.docxRun.addEventListener('click', runDocx);
el.docxDrop.addEventListener('click', () => {
  docxFile = null;
  el.docx.hidden = true;
  el.docxChanges.replaceChildren();
});

/* theme */
const STORED = 'humanizer-theme';
const savedTheme = localStorage.getItem(STORED);
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
el.theme.addEventListener('click', () => {
  const order = ['auto', 'light', 'dark'];
  const next = order[(order.indexOf(document.documentElement.dataset.theme) + 1) % 3];
  document.documentElement.dataset.theme = next;
  localStorage.setItem(STORED, next);
  toast(`Theme: ${next}`);
});

/* drag and drop anywhere on the page */
let dragDepth = 0;
window.addEventListener('dragenter', (event) => {
  if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes('Files')) return;
  dragDepth++;
  el.drop.hidden = false;
});
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('dragleave', () => {
  if (--dragDepth <= 0) { dragDepth = 0; el.drop.hidden = true; }
});
window.addEventListener('drop', (event) => {
  event.preventDefault();
  dragDepth = 0;
  el.drop.hidden = true;
  const file = event.dataTransfer && event.dataTransfer.files[0];
  if (file) takeFile(file);
});

/* keyboard: left/right nudge the level from anywhere outside a field */
window.addEventListener('keydown', (event) => {
  const typing = /^(INPUT|TEXTAREA)$/.test(event.target.tagName);
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    const step = event.key === 'ArrowRight' ? 1 : -1;
    el.level.value = Math.max(0, Math.min(10, Number(el.level.value) + step));
    paintSliders();
    schedule(80);
    event.preventDefault();
  }
});

/* A seed is filled in at the start rather than left empty. Without one, every
 * keystroke re-rolls every random choice and the whole output churns while you
 * type; with one, only what you edited changes. Re-roll is how you ask for a
 * different set of choices at the same level. */
el.seed.value = Math.floor(Math.random() * 100000);

paintSliders();
el.input.focus();
