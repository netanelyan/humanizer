"""The slips real typing leaves behind, in English and in Hebrew.

Deliberately narrow: every kind here is something a person actually does at a
keyboard, and nothing is applied densely enough to read as damage. Sites are
spread across the text rather than clustered, and the opening sentence is left
alone by default because that is the part people reread.

Each word is routed by script, so a mixed document gets the right slips in the
right places and never an English mistake on a Hebrew word. Hebrew has no
capitalisation, which removes three of the English kinds outright, and adds
mistakes English has no equivalent for — a final letter typed in its ordinary
form, a ‏ו‎ or ‏י‎ dropped out of a full spelling, a prefix left floating.

    shared      drop_letter, double_letter, swap_letters,
                double_space, missing_space, dropped_comma
    English     lowercase_start, key_slip, lost_apostrophe,
                shift_held, homophone
    Hebrew      he_final_form, he_ktiv, he_key_slip,
                he_alef_he, he_prefix_split
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .lexicon_he import SKIP_WORDS as HEBREW_SKIP_WORDS

HEBREW_RANGE = "֐-׿"
HEBREW_LETTERS = "א-ת"

# QWERTY neighbours, for the English fat-finger substitutions.
_NEIGHBOURS: Dict[str, str] = {
    "a": "qwsz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr",
    "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "i": "ujko", "j": "huikmn",
    "k": "jiolm", "l": "kop;", "m": "njk", "n": "bhjm", "o": "iklp",
    "p": "ol;[", "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy",
    "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu",
    "z": "asx",
}

# The Israeli standard layout, by physical key position:
#   ק ר א ט ו ן ם פ
#   ש ד ג כ ע י ח ל ך ף
#   ז ס ב ה נ מ צ ת ץ
_HE_NEIGHBOURS: Dict[str, str] = {
    "ק": "רדג", "ר": "קאגכ", "א": "רטכע", "ט": "אויע", "ו": "טןיח",
    "ן": "ומחל", "ם": "ןפלך", "פ": "םךף",
    "ש": "דזס", "ד": "שגקזס", "ג": "דכרסב", "כ": "גערבה", "ע": "כיטהנ",
    "י": "עחונמ", "ח": "ילןמצ", "ל": "חךםצת", "ך": "לףפתץ", "ף": "ךץפ",
    "ז": "שסד", "ס": "זבשדג", "ב": "סהדגכ", "ה": "בנגכע", "נ": "המכעי",
    "מ": "נצעיח", "צ": "מתיחל", "ת": "צץחלך", "ץ": "תלךף",
}

_FINAL_TO_PLAIN = {"ם": "מ", "ן": "נ", "ץ": "צ", "ף": "פ", "ך": "כ"}
_PLAIN_TO_FINAL = {plain: final for final, plain in _FINAL_TO_PLAIN.items()}
_HE_PREFIXES = "ובלכמשה"

_HOMOPHONES: Dict[str, List[str]] = {
    "its": ["it's"], "it's": ["its"],
    "your": ["you're"], "you're": ["your"],
    "their": ["there"], "there": ["their"], "they're": ["their"],
    "then": ["than"], "than": ["then"],
    "too": ["to"],
    "lose": ["loose"], "loose": ["lose"],
    "affect": ["effect"], "effect": ["affect"],
    "who's": ["whose"], "whose": ["who's"],
    "alot": ["a lot"],
}

_APOSTROPHE_RE = re.compile(r"\b([A-Za-z]+)['’](ll|re|ve|nt|t|s|d|m)\b")
_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?|[" + HEBREW_RANGE + r"]+")
_HEBREW_RE = re.compile("[" + HEBREW_LETTERS + "]")
_SENTENCE_START_RE = re.compile(
    r"(?:^|(?<=[.!?][\"'”’)\]])\s+|(?<=[.!?])\s+)(?=[^\W\d_])", re.UNICODE
)

_SKIP_WORDS = {
    "a", "an", "i", "is", "it", "of", "or", "to", "in", "on", "at", "be",
    "as", "by", "we", "he", "do", "if", "so", "no", "up", "us", "my", "me",
}

# relative weight per kind
_WEIGHTS: List[Tuple[str, float]] = [
    # shared
    ("drop_letter", 20.0),
    ("double_space", 14.0),
    ("swap_letters", 9.0),
    ("double_letter", 7.0),
    ("missing_space", 5.0),
    ("dropped_comma", 4.0),
    # English
    ("lowercase_start", 16.0),
    ("key_slip", 12.0),
    ("lost_apostrophe", 7.0),
    ("shift_held", 3.0),
    ("homophone", 3.0),
    # Hebrew
    ("he_final_form", 20.0),
    ("he_key_slip", 14.0),
    ("he_ktiv", 12.0),
    ("he_alef_he", 5.0),
    ("he_prefix_split", 4.0),
]

HEBREW_KINDS = frozenset(k for k, _ in _WEIGHTS if k.startswith("he_"))
ENGLISH_KINDS = frozenset(
    ("lowercase_start", "key_slip", "lost_apostrophe", "shift_held", "homophone"))
SHARED_KINDS = frozenset(
    ("drop_letter", "double_letter", "swap_letters",
     "double_space", "missing_space", "dropped_comma"))


def is_hebrew(text: str) -> bool:
    return bool(_HEBREW_RE.search(text))


def _blocked(spans: Sequence[Tuple[int, int]], start: int, end: int) -> bool:
    for span_start, span_end in spans:
        if span_start >= end:
            break
        if start < span_end and span_start < end:
            return True
    return False


class TypoEngine:
    """Picks well-spaced spots in the text and roughs them up a little."""

    def __init__(self, rng, kinds: Optional[Sequence[str]] = None):
        self._rng = rng
        allowed = set(kinds) if kinds else None
        self._weights = [(k, w) for k, w in _WEIGHTS if allowed is None or k in allowed]

    # -- site collection -------------------------------------------------

    def _sites(self, text: str, blocked, skip_before: int):
        """Group every candidate position in the text by typo kind."""
        sites: Dict[str, List] = {kind: [] for kind, _ in self._weights}
        kinds = set(sites)

        # match end, not start: the pattern eats the gap before the sentence
        starts = set(m.end() for m in _SENTENCE_START_RE.finditer(text))

        for match in _WORD_RE.finditer(text):
            start, end = match.span()
            if start < skip_before or _blocked(blocked, start, end):
                continue
            word = match.group(0)
            if is_hebrew(word):
                self._hebrew_sites(sites, kinds, start, end, word)
            else:
                self._english_sites(sites, kinds, start, end, word, starts)

        if "lost_apostrophe" in kinds:
            for match in _APOSTROPHE_RE.finditer(text):
                start, end = match.span()
                if start >= skip_before and not _blocked(blocked, start, end):
                    sites["lost_apostrophe"].append((start, end, match.group(0)))

        if "double_space" in kinds or "missing_space" in kinds:
            letter = "A-Za-z" + HEBREW_RANGE
            pattern = r"(?<=[" + letter + r",.])( )(?=[" + letter + r"])"
            for match in re.finditer(pattern, text):
                start, end = match.span(1)
                if start < skip_before or _blocked(blocked, start, end):
                    continue
                if "double_space" in kinds:
                    sites["double_space"].append((start, end, " "))
                left = re.search("[" + letter + "]+$", text[:start])
                right = re.match("[" + letter + "]+", text[end:])
                if ("missing_space" in kinds and left and right
                        and len(left.group(0)) + len(right.group(0)) <= 14):
                    sites["missing_space"].append((start, end, " "))

        if "dropped_comma" in kinds:
            pattern = r",(?= [a-z" + HEBREW_LETTERS + r"])"
            for match in re.finditer(pattern, text):
                start, end = match.span()
                if start >= skip_before and not _blocked(blocked, start, end):
                    sites["dropped_comma"].append((start, end, ","))

        return {k: v for k, v in sites.items() if v}

    def _english_sites(self, sites, kinds, start, end, word, starts):
        lowered = word.lower()
        core = word.replace("'", "").replace("’", "")

        if start in starts and word[0].isupper():
            if word != "I" and not (len(word) > 1 and word[1].isupper()):
                if "lowercase_start" in kinds:
                    sites["lowercase_start"].append((start, end, word))
                if "shift_held" in kinds and len(word) >= 3 and word[1].isalpha():
                    sites["shift_held"].append((start, end, word))

        if lowered in _HOMOPHONES and "homophone" in kinds:
            sites["homophone"].append((start, end, word))

        if lowered in _SKIP_WORDS or len(core) < 4 or word.isupper():
            return
        if "'" in word or "’" in word:
            return

        if "drop_letter" in kinds and len(word) >= 5:
            sites["drop_letter"].append((start, end, word))
        if "key_slip" in kinds:
            sites["key_slip"].append((start, end, word))
        if "swap_letters" in kinds and len(word) >= 5:
            sites["swap_letters"].append((start, end, word))
        if "double_letter" in kinds:
            sites["double_letter"].append((start, end, word))

    def _hebrew_sites(self, sites, kinds, start, end, word):
        if word in HEBREW_SKIP_WORDS or len(word) < 3:
            return

        # ‏שלום‎ -> ‏שלומ‎: the single most recognisable Hebrew typing slip
        if "he_final_form" in kinds and word[-1] in _FINAL_TO_PLAIN:
            sites["he_final_form"].append((start, end, word))

        if "he_key_slip" in kinds:
            sites["he_key_slip"].append((start, end, word))

        # a ‏ו‎ or ‏י‎ that full spelling put there and a fast typist leaves out
        if "he_ktiv" in kinds and len(word) >= 4:
            if any(c in "וי" for c in word[1:]):
                sites["he_ktiv"].append((start, end, word))

        if "he_alef_he" in kinds and len(word) >= 4 and word[-1] in "אה":
            sites["he_alef_he"].append((start, end, word))

        # ‏וכך‎ -> ‏ו כך‎: the prefix gets its own space by accident
        if "he_prefix_split" in kinds and len(word) >= 5 and word[0] in _HE_PREFIXES:
            sites["he_prefix_split"].append((start, end, word))

        if len(word) < 4:
            return
        if "drop_letter" in kinds and len(word) >= 5:
            sites["drop_letter"].append((start, end, word))
        if "swap_letters" in kinds and len(word) >= 5:
            sites["swap_letters"].append((start, end, word))
        if "double_letter" in kinds:
            sites["double_letter"].append((start, end, word))

    # -- mutations -------------------------------------------------------

    def _mutate(self, kind: str, word: str):
        rng = self._rng

        # -- shared --
        if kind == "drop_letter":
            # never the first character: people rarely miss the letter they
            # aimed at first, they miss one in the middle of the run
            index = rng.randrange(1, len(word) - 1)
            return word[:index] + word[index + 1:]

        if kind == "double_letter":
            index = rng.randrange(1, len(word))
            if word[index] == word[index - 1]:
                return None
            return word[:index + 1] + word[index] + word[index + 1:]

        if kind == "swap_letters":
            index = rng.randrange(1, len(word) - 2)
            if word[index] == word[index + 1]:
                return None
            return word[:index] + word[index + 1] + word[index] + word[index + 2:]

        if kind == "double_space":
            return "  "
        if kind in ("missing_space", "dropped_comma"):
            return ""

        # -- English --
        if kind == "lowercase_start":
            return word[0].lower() + word[1:]

        if kind == "shift_held":
            return word[0] + word[1].upper() + word[2:]

        if kind == "key_slip":
            return self._slip(word, _NEIGHBOURS, cased=True)

        if kind == "lost_apostrophe":
            return word.replace("'", "").replace("’", "")

        if kind == "homophone":
            options = _HOMOPHONES.get(word.lower())
            if not options:
                return None
            swap = rng.choice(options)
            return swap[0].upper() + swap[1:] if word[0].isupper() else swap

        # -- Hebrew --
        if kind == "he_final_form":
            return word[:-1] + _FINAL_TO_PLAIN[word[-1]]

        if kind == "he_key_slip":
            return self._slip(word, _HE_NEIGHBOURS, cased=False)

        if kind == "he_ktiv":
            options = [i for i in range(1, len(word)) if word[i] in "וי"]
            if not options:
                return None
            index = rng.choice(options)
            trimmed = word[:index] + word[index + 1:]
            # dropping the letter can strand a final form mid-word
            return trimmed if len(trimmed) >= 3 else None

        if kind == "he_alef_he":
            return word[:-1] + ("ה" if word[-1] == "א" else "א")

        if kind == "he_prefix_split":
            return word[0] + " " + word[1:]

        return None

    def _slip(self, word: str, table: Dict[str, str], cased: bool):
        rng = self._rng
        options = [i for i in range(1, len(word)) if word[i].lower() in table]
        if not options:
            return None
        index = rng.choice(options)
        char = word[index]
        hit = rng.choice(table[char.lower()])
        if not hit.isalpha():
            return None
        if cased and char.isupper():
            hit = hit.upper()
        return word[:index] + hit + word[index + 1:]

    # -- planning --------------------------------------------------------

    def plan(self, text: str, rate: float, blocked=(), skip_first_sentence: bool = True):
        """Return edits introducing roughly ``rate`` typos per 1000 words."""
        from .core import Edit, dedupe

        if rate <= 0 or not text.strip():
            return []

        words = len(_WORD_RE.findall(text))
        if words < 8:
            return []

        expected = rate * words / 1000.0
        count = int(expected)
        if self._rng.random() < expected - count:
            count += 1
        if count <= 0:
            return []

        skip_before = 0
        if skip_first_sentence:
            starts = [m.end() for m in _SENTENCE_START_RE.finditer(text)]
            skip_before = starts[1] if len(starts) > 1 else 0

        sites = self._sites(text, blocked, skip_before)
        if not sites:
            return []

        # Keep the slips apart so they read as scattered rather than a burst of
        # damage in one paragraph.
        gap = max(28, int(len(text) / max(count * 1.6, 1)))

        edits: List[Edit] = []
        placed: List[int] = []
        kinds = [k for k, _ in self._weights if k in sites]
        weights = [w for k, w in self._weights if k in sites]
        attempts = 0
        limit = count * 25

        while len(edits) < count and attempts < limit:
            attempts += 1
            kind = self._rng.choices(kinds, weights=weights, k=1)[0]
            pool = sites.get(kind)
            if not pool:
                continue
            start, end, original = pool[self._rng.randrange(len(pool))]
            if any(abs(start - other) < gap for other in placed):
                continue
            new = self._mutate(kind, original)
            if new is None or new == original:
                continue
            edits.append(Edit(start, end, new, "typo:" + kind, original))
            placed.append(start)

        return dedupe(edits)
