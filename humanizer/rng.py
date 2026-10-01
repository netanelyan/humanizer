"""The random number generator, specified rather than borrowed.

``random.Random`` would be the obvious choice, but the browser build has to
produce the same rewrite from the same seed as the command line does, and
Python's Mersenne Twister is not something you can faithfully reimplement in a
page's worth of JavaScript. So the generator is mulberry32: thirty lines,
exactly specified, and mirrored in ``webui/engine/rng.js``.

Every other operation is defined in terms of ``next()`` alone, so the two
implementations agree as long as that one function does.
``tests/test_parity.py`` checks that against the real thing rather than taking
it on trust.

The arithmetic is kept in unsigned 32-bit throughout. JavaScript's ``Math.imul``
and ``^`` coerce to *signed* int32, but the bit patterns are identical mod 2**32
and every value ends up masked, so the two agree.
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence

MASK = 0xFFFFFFFF


class Rng:
    """mulberry32. Seeded with a 32-bit integer, or from the OS if None."""

    __slots__ = ("_state", "seed")

    def __init__(self, seed: Optional[int] = None):
        if seed is None:
            seed = int.from_bytes(os.urandom(4), "little")
        self.seed = int(seed) & MASK
        self._state = self.seed

    def next(self) -> float:
        """A float in [0, 1). Everything else is built on this."""
        self._state = (self._state + 0x6D2B79F5) & MASK
        t = self._state
        t = ((t ^ (t >> 15)) * (t | 1)) & MASK
        mixed = ((t ^ (t >> 7)) * (t | 61)) & MASK
        t = t ^ ((t + mixed) & MASK)
        return ((t ^ (t >> 14)) & MASK) / 4294967296.0

    # Named to match random.Random so the call sites read the same.
    random = next

    def randrange(self, start: int, stop: Optional[int] = None) -> int:
        if stop is None:
            start, stop = 0, start
        span = stop - start
        if span <= 0:
            raise ValueError("empty range")
        return start + int(self.next() * span)

    def choice(self, sequence: Sequence):
        if not sequence:
            raise IndexError("cannot choose from an empty sequence")
        return sequence[int(self.next() * len(sequence))]

    def choices(self, population: Sequence, weights: Sequence[float], k: int = 1) -> List:
        """Weighted sampling with replacement, same shape as random.choices.

        The running sum is accumulated in population order so the floating point
        result is identical in both languages.
        """
        total = 0.0
        for weight in weights:
            total += weight
        picked = []
        for _ in range(k):
            if total <= 0:
                picked.append(population[0])
                continue
            target = self.next() * total
            running = 0.0
            chosen = population[len(population) - 1]
            for index in range(len(population)):
                running += weights[index]
                if target < running:
                    chosen = population[index]
                    break
            picked.append(chosen)
        return picked
