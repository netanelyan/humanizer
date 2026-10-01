"""Emit webui/engine/lexicon.js from the Python lexicons.

The word and phrase tables are the bulk of the engine and the thing most likely
to drift between the two implementations, so they are not ported — they are
generated. Edit ``lexicon.py`` or ``lexicon_he.py`` and run this; the JavaScript
is never edited by hand.

    py tools/build_lexicon.py            # write the file
    py tools/build_lexicon.py --check    # fail if it is out of date

Tiers are emitted as arrays of pairs rather than objects so iteration order is
guaranteed to match Python's insertion order, which the table build depends on.
"""

from __future__ import annotations

import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from humanizer import lexicon, lexicon_he                       # noqa: E402

TARGET = os.path.join(ROOT, "humanizer", "webui", "engine", "lexicon.js")

HEADER = """\
/* GENERATED FILE - do not edit.
 *
 * Produced by tools/build_lexicon.py from humanizer/lexicon.py and
 * humanizer/lexicon_he.py. Those are the source of truth; this exists so the
 * ~%d word and phrase entries cannot drift between the Python engine and the
 * browser one. Change the Python, then run:
 *
 *     py tools/build_lexicon.py
 *
 * Tiers are arrays of [key, options] pairs, not objects, so the iteration
 * order matches Python's and the merged table comes out identical.
 */

"""


def js(value) -> str:
    """JSON with the non-ASCII left alone, so the Hebrew stays readable."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def pairs(table) -> str:
    return "[" + ",".join("[%s,%s]" % (js(k), js(v)) for k, v in table.items()) + "]"


def tiers(spec) -> str:
    return "[" + ",".join("[%d,%s]" % (level, pairs(table)) for level, table in spec) + "]"


def render() -> str:
    total = sum(len(table) for _, table in
                lexicon.PHRASE_TIERS + lexicon.WORD_TIERS
                + lexicon_he.PHRASE_TIERS + lexicon_he.WORD_TIERS)

    out = io.StringIO()
    out.write(HEADER % total)

    out.write("export const EN = {\n")
    out.write("  phraseTiers: %s,\n" % tiers(lexicon.PHRASE_TIERS))
    out.write("  wordTiers: %s,\n" % tiers(lexicon.WORD_TIERS))
    out.write("  contractions: %s,\n" % pairs(lexicon.CONTRACTIONS))
    out.write("  hedges: %s,\n" % js(lexicon.HEDGES))
    out.write("  openers: %s,\n" % js(lexicon.OPENERS))
    out.write("  connectiveStarts: %s,\n" % js(list(lexicon.CONNECTIVE_STARTS)))
    out.write("  hyphenKeep: %s,\n" % js(sorted(lexicon.HYPHEN_KEEP)))
    out.write("  hyphenJoin: %s,\n" % pairs(lexicon.HYPHEN_JOIN))
    out.write("};\n\n")

    out.write("export const HE = {\n")
    out.write("  phraseTiers: %s,\n" % tiers(lexicon_he.PHRASE_TIERS))
    out.write("  wordTiers: %s,\n" % tiers(lexicon_he.WORD_TIERS))
    out.write("  hedges: %s,\n" % js(lexicon_he.HEDGES))
    out.write("  openers: %s,\n" % js(lexicon_he.OPENERS))
    out.write("  connectiveStarts: %s,\n" % js(list(lexicon_he.CONNECTIVE_STARTS)))
    out.write("  hyphenKeep: %s,\n" % js(sorted(lexicon_he.HYPHEN_KEEP)))
    out.write("  skipWords: %s,\n" % js(sorted(lexicon_he.SKIP_WORDS)))
    out.write("};\n")
    return out.getvalue()


def main(argv) -> int:
    generated = render()
    check_only = "--check" in argv

    current = None
    if os.path.isfile(TARGET):
        with open(TARGET, encoding="utf-8") as handle:
            current = handle.read()

    if current == generated:
        print("lexicon.js is up to date")
        return 0

    if check_only:
        sys.stderr.write(
            "lexicon.js is out of date with the Python lexicons.\n"
            "run: py tools/build_lexicon.py\n")
        return 1

    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    with open(TARGET, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(generated)
    print("wrote %s (%d bytes)" % (os.path.relpath(TARGET, ROOT), len(generated)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
