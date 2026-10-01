# humanizer

Rewrites text so it reads like a person typed it rather than a model generated it.
It de-bloats the wording, strips the punctuation habits that give models away, and
leaves behind the small mistakes real typing has.

It handles **English and Hebrew**, and edits `.docx` in place **without touching the
formatting** — styles, bold runs, tables, headers, hyperlinks, images and tracked
changes all come through unchanged.

No dependencies, nothing to install. Python 3.8+ for the command line; the web UI
is a static page that needs no server at all.

```
py -m humanizer essay.txt -l 7       # command line
py -m humanizer report.docx -i -l 5
py -m humanizer.web                  # web UI on localhost:8000
```

## Web UI

**The page needs no server.** The engine runs in the browser, so
`humanizer/webui` is a static site — host it on GitHub Pages or anywhere else and
it works, offline included. Nothing is uploaded; a `.docx` you drop on it is read,
rewritten and handed back without leaving your machine.

```
py -m humanizer.web            # serve it locally and open a browser
py -m humanizer --serve 9000   # same thing, another port
```

That command exists only to give the page an `http` origin, which ES modules
require and `file://` cannot provide. It also keeps a small JSON API for driving
the Python engine over HTTP, which the page does not use.

A slider you can actually drag, with the rewrite updating live as you move it.
Every change is highlighted in the output — hover one to see what it was and which
rule changed it — and the tally underneath breaks the edits down by kind. Drop a
`.docx` anywhere on the page and it comes back rewritten with the formatting
intact. `.txt` and `.md` load straight into the editor. Arrow keys nudge the level.
A seed is filled in at the start so the output only changes where you edit; the
re-roll button asks for a different set of choices at the same level.

### Two engines, kept in step

The rules exist twice, in `humanizer/*.py` and `humanizer/webui/engine/*.js`.
Two things stop them drifting:

- **The lexicons are generated, not ported.** `tools/build_lexicon.py` emits
  `engine/lexicon.js` from the Python tables, so the ~950 word and phrase entries
  have one source. A test fails if the generated file is stale.
- **A parity test.** `tests/test_parity.py` runs 126 cases — both languages, every
  level, several seeds, every switch — through both engines and asserts the output,
  the edit counts and the highlight offsets are identical, then does the same with
  a real `.docx`. That needs a shared generator, which is why `rng.py` specifies
  mulberry32 rather than using `random.Random`.

So the hand-ported part is the passes and the typo engine, and it is checked
against the original on every run. `node` on `PATH` is required for that test; it
fails loudly rather than skipping if node is missing.

The browser build reads and writes zip with `DecompressionStream` and
`CompressionStream`, which browsers provide natively, so it stays
dependency-free — no bundler, no npm, nothing to install.

## The slider

`--level` / `-l` takes 0–10 and drives how far the wording moves. Nothing else
about the tool changes with it.

| level | what it does |
|-------|--------------|
| 0 | nothing at all |
| 1–2 | em dashes out, straightened quotes, the worst filler cut (`in order to`, `due to the fact that`) |
| 3–5 | plainer register — `utilize` → `use`, `numerous` → `a lot of`, contractions, `however,` → `but` |
| 6–8 | voice shifts — `individuals` → `people`, hedges, the odd sentence opener |
| 9–10 | everything, aggressively |

Named presets work too: `-l light` (3), `-l medium` (5), `-l strong` (7),
`-l heavy` (9), `-l max` (10).

Not sure which you want? `--sweep` renders your opening at 2/4/6/8/10 and changes
nothing:

```
py -m humanizer essay.txt --sweep
```

## What it changes

**Punctuation.** Em dashes and en dashes are removed completely — they are the
single loudest tell. A dash joining two clauses becomes a full stop, a dash before
an afterthought becomes a comma, and a *pair* of dashes around an aside is always
resolved together so you never get a stranded fragment. Number ranges (`5–10`) stay
ranges. Curly quotes and ellipses are straightened; some semicolons become full stops.

**Wording.** About 400 phrase and word patterns, tiered by level. Each has several
possible replacements and the tool avoids reusing the same one twice in a row, so a
word that appears five times does not get the same substitute five times.

**Hyphens.** `long-term` → `long term`, `e-mail` → `email`. Compounds that would
change meaning if split (`x-ray`, `well-being`, `check-in`) are left alone.

**Typos.** Scattered, never clustered, and the opening sentence is skipped by
default because that is the part people reread:

- a letter missing from a word
- `a` instead of `A` at the start of a sentence
- two spaces instead of one
- a neighbouring key on the keyboard
- two letters transposed, or one typed twice
- `don't` → `dont`
- two words run together
- `THe` — shift held a beat too long
- its/it's, then/than, your/you're
- a comma that never got typed

Frequency follows the level, or set it yourself with `--typos N` (slips per 1000
words). `--typos 0` or `--no-typos` turns them off entirely.

## Hebrew

Hebrew works the same way, at the same levels, through the same commands. There is
no language flag: Hebrew and Latin share no letters, so both rule sets live in one
table and each phrase is handled by the rules for its own script. A document that
mixes the two gets both, per paragraph.

```
py -m humanizer מסמך.docx -i -l 6
```

The formal register is the tell, so that is what moves:

| | |
|---|---|
| `יש לציין כי` | `צריך להגיד ש` |
| `על מנת` | `כדי` |
| `הינו` | `הוא` |
| `מגוון רחב של` | `המון` |
| `באופן משמעותי` | `הרבה` |
| `ניתן לומר כי` | `אפשר לומר ש` |
| `אשר` | `ש` |
| `לסיכום,` | `בקיצור,` |

The typos are Hebrew ones. English has no equivalent for most of them, and Hebrew
has no capitalisation, so three of the English kinds disappear entirely:

- a final letter typed in its ordinary form — `שלום` → `שלומ`
- a `ו` or `י` dropped out of a full spelling — `אופן` → `אפן`
- a neighbouring key on the **Hebrew** layout — `כלים` → `כליף`
- `א` and `ה` swapped at the end of a word — `לקרוא` → `לקרוה`
- a prefix left floating — `וכאשר` → `ו כאשר`

Plus the language-neutral ones: a missing letter, two spaces, transposed letters,
words run together.

Two things make Hebrew harder than English, and both are handled explicitly:

**Prefixes attach.** `ו ה ב ל כ מ ש` join the following word with no space, so a
replacement ending in one has to close up: `אשר` → `ש` must give `שעובדים`, never
`ש עובדים`. The engine extends the edit to swallow the space, and a test asserts
no stranded prefix survives at any level or seed.

**Agreement.** A noun swap that changes gender or number breaks the verb and the
adjective around it — `פתרונות אלה מדגימים` would become `דרכים אלה מדגימים`. Every
pair in the table matches on both counts, and where no natural match existed the
entry was dropped rather than fudged. That is why `מהווה` is not in there: replacing
it needs the subject's gender, which a lookup table cannot know, and `עבודה ... הוא`
is a worse error than the stiffness it was meant to fix.

Right-to-left `.docx` files are safe by construction: `<w:bidi/>`, `<w:rtl/>` and
paragraph alignment live in properties the rewriter never touches. In the web UI
both panes use `unicode-bidi: plaintext`, so each paragraph takes its own direction.

## What it will not touch

URLs, email addresses, `code spans`, fenced code blocks, `[1]` citation markers,
`{placeholders}`, and dotted identifiers are detected and left alone. Add your own
with `--preserve REGEX` (repeatable), or in the web UI under *What to change*.

In a `.docx` it also skips field codes (`<w:instrText>`) and tracked deletions
(`<w:delText>`). Comments are skipped unless you pass `--comments`.

## Command line

```
humanize [INPUT] [-o OUTPUT] [-l LEVEL] [options]
```

| flag | |
|------|---|
| `-o`, `--output` | where to write; omit for stdout |
| `-i`, `--in-place` | overwrite the input file |
| `-l`, `--level N` | 0–10 or a preset name (default 5) |
| `--typos RATE` | slips per 1000 words; overrides the level |
| `--seed N` | fixed seed — same input, same output, every time |
| `--diff` | show what changed |
| `--stats` | summarise the edits by kind |
| `--sweep` | preview several levels, write nothing |
| `--dry-run` | change nothing on disk |
| `--serve [PORT]` | open the web UI instead |

Switches for each family of change: `--no-typos`, `--no-rewrite`,
`--no-contractions`, `--no-seasoning`, `--keep-em-dashes`, `--keep-hyphens`,
`--keep-quotes`, `--keep-semicolons`, `--typo-anywhere`.

```bash
# rewrite to stdout, medium
py -m humanizer essay.txt

# stronger, to a file, and show what moved
py -m humanizer essay.txt -l 8 -o out.txt --diff

# a Word document, edited in place, formatting kept
py -m humanizer report.docx -i -l 4

# reword only, spell everything correctly
py -m humanizer essay.txt -l 6 --no-typos

# typos and punctuation only, leave my wording alone
py -m humanizer essay.txt --no-rewrite --typos 8

# from a pipe
cat notes.md | py -m humanizer -l 6 > clean.md
```

`--seed` makes a run reproducible, which is what you want when you are comparing
levels on the same document. Without it every run differs.

## As a library

```python
from humanizer import Humanizer, Settings

h = Humanizer(Settings(level=7, seed=42))
print(h.run("It is important to note that we must utilize this — daily."))
print(h.stats)          # {'phrase': 2, 'dash': 1, ...}
```

Every change is an `Edit(start, end, new)` against the input string, so you can
apply them somewhere else instead:

```python
edits = h.plan(text)                            # nothing is applied
result, spans = apply_edits_with_spans(text, edits)   # and where each landed
```

That is exactly how the other two front ends work: the `.docx` writer replays the
edits onto individual runs, and the web UI uses the span list to highlight changes
rather than diffing two strings.

```python
from humanizer.docxio import humanize_docx
report = humanize_docx("in.docx", "out.docx", Humanizer(Settings(level=6)))
print(report.rewritten, "of", report.paragraphs, "paragraphs")
```

## HTTP API

The web UI is a thin client over two endpoints, so you can drive it from anything.

```
GET  /api/defaults            -> {version, settings, limits}
POST /api/humanize            {text, settings}        -> {text, spans, stats, words}
POST /api/docx                {file, name, settings}  -> {file, name, stats, changes}
```

`file` is base64 in both directions. `settings` takes the same fields as
`Settings`; unknown keys are ignored and every value is clamped server-side.

## How .docx editing works

A `.docx` is a zip of XML parts. The tool reads the body parts, joins the `<w:t>`
runs of each paragraph into plain text (so a phrase Word split across three runs is
still matched as one phrase), plans the edits, then writes each edit back into the
run it started in. Everything else in the archive is copied byte for byte with its
original timestamp and compression.

An edit spanning several runs puts its replacement in the first one — the same
result you would get retyping the sentence by hand.

## Tests

```
py tests/run_all.py     # no pytest needed
py -m pytest -q         # or with it
```

`test_humanizer.py` covers the engine and the .docx writer, `test_hebrew.py` the
Hebrew rules — prefix attachment, gender and number agreement, script routing and
RTL documents — `test_web.py` the server and its input validation, `test_webui.py`
the page itself (every element id the script reaches for has to exist, every
checkbox has to name a real setting and match its default, nothing may load from
another origin or make a network call), and `test_parity.py` that the JavaScript
engine and the Python one agree exactly.

## A note on what this is for

This is a writing tool. It makes stiff prose read more naturally and strips the
punctuation and vocabulary habits that make text obviously machine-written. Whether
that is appropriate for a given document is your call — if you are submitting work
somewhere with rules about authorship, those rules still apply.
