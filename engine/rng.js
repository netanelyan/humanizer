/* The random number generator, mirroring humanizer/rng.py exactly.
 *
 * mulberry32, chosen because it can be written identically in both languages:
 * the same seed has to produce the same rewrite in this page as it does on the
 * command line. tests/test_parity.py asserts the two streams match rather than
 * trusting that they do.
 *
 * Math.imul and ^ coerce to signed int32 here while the Python side stays
 * unsigned, but the bit patterns are identical mod 2**32 and every value is
 * masked back with >>> 0, so the results agree.
 */

export class Rng {
  constructor(seed) {
    if (seed === null || seed === undefined) {
      seed = Math.floor(Math.random() * 4294967296);
    }
    this.seed = seed >>> 0;
    this.state = this.seed;
  }

  /** A float in [0, 1). Everything else is built on this. */
  next() {
    this.state = (this.state + 0x6d2b79f5) >>> 0;
    let t = this.state;
    t = Math.imul(t ^ (t >>> 15), t | 1) >>> 0;
    const mixed = Math.imul(t ^ (t >>> 7), t | 61) >>> 0;
    t = (t ^ ((t + mixed) >>> 0)) >>> 0;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }

  /** Alias, so call sites read the same as the Python ones. */
  random() {
    return this.next();
  }

  randrange(start, stop) {
    if (stop === undefined) {
      stop = start;
      start = 0;
    }
    const span = stop - start;
    if (span <= 0) throw new Error('empty range');
    return start + Math.floor(this.next() * span);
  }

  choice(sequence) {
    if (!sequence.length) throw new Error('cannot choose from an empty sequence');
    return sequence[Math.floor(this.next() * sequence.length)];
  }

  /** Weighted sampling with replacement. The running sum is accumulated in
   *  population order so the floating point result matches Python's. */
  choices(population, weights, k = 1) {
    let total = 0;
    for (const weight of weights) total += weight;
    const picked = [];
    for (let i = 0; i < k; i++) {
      if (total <= 0) {
        picked.push(population[0]);
        continue;
      }
      const target = this.next() * total;
      let running = 0;
      let chosen = population[population.length - 1];
      for (let index = 0; index < population.length; index++) {
        running += weights[index];
        if (target < running) {
          chosen = population[index];
          break;
        }
      }
      picked.push(chosen);
    }
    return picked;
  }
}
