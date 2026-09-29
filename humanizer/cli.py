"""Command line front end."""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from typing import List, Optional, Sequence

from . import __version__
from .core import Humanizer, Settings, apply_edits

PRESETS = {"off": 0, "light": 3, "medium": 5, "strong": 7, "heavy": 9, "max": 10}

_ANSI = {
    "dim": "\033[2m", "red": "\033[31m", "green": "\033[32m",
    "yellow": "\033[33m", "cyan": "\033[36m", "bold": "\033[1m", "off": "\033[0m",
}


def _colour_enabled(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class Painter:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, name: str, text: str) -> str:
        if not self.enabled:
            return text
        return _ANSI[name] + text + _ANSI["off"]


def _level(value: str) -> float:
    key = value.strip().lower()
    if key in PRESETS:
        return float(PRESETS[key])
    try:
        number = float(key)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "level must be 0-10 or one of: %s" % ", ".join(PRESETS))
    if not 0 <= number <= 10:
        raise argparse.ArgumentTypeError("level must be between 0 and 10")
    return number


def _read_text(path: Optional[str]) -> str:
    if path is None or path == "-":
        data = sys.stdin.buffer.read()
    else:
        with open(path, "rb") as handle:
            data = handle.read()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", "replace")


def _write_text(path: Optional[str], text: str) -> None:
    if path is None or path == "-":
        stream = sys.stdout
        try:
            stream.reconfigure(encoding="utf-8")       # py3.7+
        except (AttributeError, ValueError):
            pass
        stream.write(text)
        if text and not text.endswith("\n"):
            stream.write("\n")
        return
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _slider(level: float, paint: Painter) -> str:
    filled = int(round(level))
    bar = "█" * filled + "·" * (10 - filled)
    return "%s %s" % (paint("cyan", bar), paint("bold", "%g/10" % level))


def _build_settings(args) -> Settings:
    return Settings(
        level=args.level,
        typo_rate=0.0 if args.no_typos else args.typos,
        seed=args.seed,
        em_dashes=not args.keep_em_dashes,
        hyphens=not args.keep_hyphens,
        contractions=not args.no_contractions,
        lexical=not args.no_rewrite,
        seasoning=not args.no_seasoning,
        typos=not args.no_typos,
        quotes=not args.keep_quotes,
        semicolons=not args.keep_semicolons,
        protect_first_sentence=not args.typo_anywhere,
        preserve=list(args.preserve or []),
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

_LABELS = {
    "dash": "em dashes removed",
    "quote": "smart quotes straightened",
    "semicolon": "semicolons split",
    "phrase": "phrases reworded",
    "word": "words swapped",
    "contraction": "contractions",
    "hyphen": "hyphens loosened",
    "opener": "sentence openers",
    "hedge": "hedges added",
    "typo:drop_letter": "missing letters",
    "typo:lowercase_start": "lowercase sentence starts",
    "typo:double_space": "double spaces",
    "typo:key_slip": "neighbouring-key slips",
    "typo:swap_letters": "swapped letters",
    "typo:double_letter": "doubled letters",
    "typo:lost_apostrophe": "dropped apostrophes",
    "typo:missing_space": "run-together words",
    "typo:dropped_comma": "dropped commas",
    "typo:shift_held": "shift held too long",
    "typo:homophone": "homophone slips",
}


def _print_stats(stats, level, words, paint, stream) -> None:
    stream.write("\n%s  %s\n" % (paint("bold", "level"), _slider(level, paint)))
    if words:
        stream.write("%s  %d\n" % (paint("bold", "words"), words))
    total = sum(stats.values())
    if not total:
        stream.write(paint("yellow", "no changes made\n"))
        return
    stream.write("%s  %d\n\n" % (paint("bold", "edits"), total))
    width = max(len(_LABELS.get(k, k)) for k in stats)
    for kind, count in sorted(stats.items(), key=lambda kv: (-kv[1], kv[0])):
        label = _LABELS.get(kind, kind)
        stream.write("  %s %s\n" % (label.ljust(width), paint("cyan", str(count))))


_WORD_SPLIT = re.compile(r"(\s+)")


def _inline_diff(before: str, after: str, paint: Painter) -> str:
    """Word-level diff, colourised in a terminal and bracketed anywhere else.

    Without colour the two versions would run together into nonsense, so the
    plain form marks them the way `git diff --word-diff` does.
    """
    old = _WORD_SPLIT.split(before)
    new = _WORD_SPLIT.split(after)
    out: List[str] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.append("".join(old[i1:i2]))
            continue
        removed = "".join(old[i1:i2])
        added = "".join(new[j1:j2])
        if paint.enabled:
            if removed:
                out.append(paint("red", removed))
            if added:
                out.append(paint("green", added))
        elif not removed.strip() and not added.strip():
            # a whitespace-only change, such as a doubled space
            out.append("{+%s+}" % added if added != removed else added)
        else:
            # keep the original spacing around the markers rather than
            # inventing one, or every change picks up a stray double space
            out.append(removed[:len(removed) - len(removed.lstrip())]
                       or added[:len(added) - len(added.lstrip())])
            if removed.strip():
                out.append("[-%s-]" % removed.strip())
            if added.strip():
                out.append("{+%s+}" % added.strip())
            out.append(removed[len(removed.rstrip()):]
                       or added[len(added.rstrip()):])
    return "".join(out)


def _print_changes(pairs, paint, stream, limit: Optional[int] = None) -> None:
    shown = 0
    for before, after in pairs:
        if before == after:
            continue
        if limit is not None and shown >= limit:
            stream.write(paint("dim", "  ... and more\n"))
            break
        shown += 1
        stream.write("\n" + _inline_diff(before, after, paint) + "\n")


def _sweep(text: str, args, paint: Painter, stream) -> int:
    """Show the same opening at several levels so you can pick one."""
    sample = " ".join(text.split())
    sentences = re.split(r"(?<=[.!?])\s+", sample)
    sample = " ".join(sentences[:3])[:600]
    if not sample.strip():
        stream.write("nothing to preview\n")
        return 1
    stream.write("\n%s\n%s\n" % (paint("bold", "level 0 (original)"), sample))
    for level in (2, 4, 6, 8, 10):
        settings = _build_settings(args)
        settings.level = level
        if args.typos is None and not args.no_typos:
            settings.typo_rate = None
        rendered = Humanizer(settings).run(sample)
        stream.write("\n%s  %s\n%s\n" % (
            paint("bold", "level %d" % level),
            _slider(level, paint),
            _inline_diff(sample, rendered, paint),
        ))
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="humanize",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Rewrite text so it reads like a person typed it.",
        epilog="""\
examples
  humanize essay.txt                     rewrite to stdout at level 5
  humanize essay.txt -l 8 -o out.txt     stronger rewrite, to a file
  humanize report.docx -i -l 4           edit the .docx in place, formatting kept
  humanize essay.txt --sweep             preview levels 2/4/6/8/10 and pick one
  cat notes.md | humanize -l 6 --diff    read stdin, show what changed
  humanize essay.txt --typos 0           reword only, no spelling slips

the slider
  0 off   3 light   5 medium   7 strong   9 heavy   10 max
  Level drives the wording only. Typo frequency follows it but can be set on
  its own with --typos (slips per 1000 words) or switched off with --no-typos.
""")

    parser.add_argument("input", nargs="?", help="file to rewrite (.txt, .md, .docx); omit for stdin")
    parser.add_argument("-o", "--output", help="where to write; omit for stdout")
    parser.add_argument("-i", "--in-place", action="store_true", help="overwrite the input file")

    slider = parser.add_argument_group("slider")
    slider.add_argument("-l", "--level", type=_level, default=5.0, metavar="N",
                        help="0-10, or a preset name (default: 5)")
    slider.add_argument("--typos", type=float, metavar="RATE",
                        help="typos per 1000 words (default: follows --level)")
    slider.add_argument("--seed", type=int, help="fixed seed, for a repeatable rewrite")

    switches = parser.add_argument_group("what to change")
    switches.add_argument("--no-typos", action="store_true", help="reword only, spell everything right")
    switches.add_argument("--no-rewrite", action="store_true", help="typos and punctuation only")
    switches.add_argument("--no-contractions", action="store_true")
    switches.add_argument("--no-seasoning", action="store_true", help="no added hedges or sentence openers")
    switches.add_argument("--keep-em-dashes", action="store_true", help="leave em dashes alone")
    switches.add_argument("--keep-hyphens", action="store_true", help="leave hyphenated compounds alone")
    switches.add_argument("--keep-quotes", action="store_true", help="leave curly quotes alone")
    switches.add_argument("--keep-semicolons", action="store_true")
    switches.add_argument("--typo-anywhere", action="store_true",
                          help="allow typos in the opening sentence too")
    switches.add_argument("--preserve", action="append", metavar="REGEX",
                          help="never touch text matching this (repeatable)")
    switches.add_argument("--comments", action="store_true",
                          help=".docx: rewrite comment text as well")

    output = parser.add_argument_group("output")
    output.add_argument("--diff", action="store_true", help="show what changed")
    output.add_argument("--stats", action="store_true", help="summarise the edits")
    output.add_argument("--sweep", action="store_true",
                        help="preview the opening at several levels, change nothing")
    output.add_argument("--dry-run", action="store_true", help="do not write anything")
    output.add_argument("--no-color", action="store_true")
    parser.add_argument("--serve", nargs="?", const=8000, type=int, metavar="PORT",
                        help="open the web UI instead (default port 8000)")
    parser.add_argument("--version", action="version", version="humanize " + __version__)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.serve is not None:
        from .web import serve
        try:
            serve(port=args.serve)
        except OSError as error:
            sys.stderr.write("cannot serve on port %d: %s\n" % (args.serve, error))
            return 1
        return 0

    report_stream = sys.stderr if args.output in (None, "-") and not args.in_place else sys.stdout
    try:
        report_stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    paint = Painter(not args.no_color and _colour_enabled(report_stream))

    if args.in_place:
        if not args.input or args.input == "-":
            parser.error("--in-place needs a file")
        if args.output:
            parser.error("--in-place and --output are mutually exclusive")
        args.output = args.input

    is_docx = bool(args.input) and args.input.lower().endswith((".docx", ".docm"))

    if args.sweep:
        from .docxio import extract_docx_text
        text = extract_docx_text(args.input) if is_docx else _read_text(args.input)
        return _sweep(text, args, paint, report_stream)

    settings = _build_settings(args)

    if is_docx:
        import tempfile
        from .docxio import humanize_docx
        destination = args.output or _default_docx_output(args.input)
        scratch = None
        if args.dry_run:
            handle, scratch = tempfile.mkstemp(suffix=".docx")
            os.close(handle)
            destination = scratch
        humanizer = Humanizer(settings)
        try:
            report = humanize_docx(args.input, destination, humanizer,
                                   include_comments=args.comments)
        except (ValueError, OSError) as error:
            report_stream.write(paint("red", "error: %s\n" % error))
            return 1
        finally:
            if scratch and os.path.exists(scratch):
                os.remove(scratch)
        if args.diff:
            _print_changes(report.changes, paint, report_stream, limit=40)
        if args.stats or args.diff:
            _print_stats(report.stats, settings.clamped_level(), report.words,
                         paint, report_stream)
        if not args.dry_run:
            report_stream.write("\n%s %s  (%d of %d paragraphs touched)\n" % (
                paint("green", "wrote"), destination,
                report.rewritten, report.paragraphs))
        return 0

    text = _read_text(args.input)
    humanizer = Humanizer(settings)
    edits = humanizer.plan(text)
    result = apply_edits(text, edits)

    if not args.dry_run:
        _write_text(args.output, result)
    if args.diff:
        _print_changes([(text, result)], paint, report_stream)
    if args.stats or args.diff:
        words = len(re.findall(r"[A-Za-z']+", text))
        _print_stats(humanizer.stats, settings.clamped_level(), words,
                     paint, report_stream)
    return 0


def _default_docx_output(path: str) -> str:
    stem, extension = os.path.splitext(path)
    return stem + ".humanized" + extension


if __name__ == "__main__":
    raise SystemExit(main())
