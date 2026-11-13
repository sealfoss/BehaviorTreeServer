import { describe, expect, it } from 'vitest';
import { fittedPixelSize, MIN_PIXEL_SIZE, type Measure } from '../src/lib/notefit';

/**
 * A measuring function like the browser's layout, with fixed metrics: each character is
 * 0.5 em wide, a line 1.2 em high; lines break between words only, so a word longer than the
 * width overflows it.
 */
const measure: Measure = (text, pixels, width) => {
  const charWidth = pixels * 0.5;
  let lines = 0;
  let widest = 0;
  for (const paragraph of text.split('\n')) {
    let line = 0;
    lines++;
    for (const word of paragraph.split(' ')) {
      const wordWidth = word.length * charWidth;
      const withSpace = line === 0 ? wordWidth : line + charWidth + wordWidth;
      if (line > 0 && withSpace > width) {
        lines++;
        line = wordWidth;
      } else {
        line = withSpace;
      }
      widest = Math.max(widest, line);
    }
  }
  return { width: widest, height: lines * pixels * 1.2 };
};

describe('note font fitting (P-08)', () => {
  it('uses the largest size at which all of the text fits', () => {
    const size = fittedPixelSize('Charging logic', 200, 100, measure);
    // Two lines ("Charging" / "logic"): 2 * 1.2 em must fit 100 px -> 41 px; "Charging" is 4 em = 164 px.
    expect(size).toBe(41);
    const fits = (pixels: number) => {
      const m = measure('Charging logic', pixels, 200);
      return m.width <= 200 && m.height <= 100;
    };
    expect(fits(size)).toBe(true);
    expect(fits(size + 1)).toBe(false);
  });

  it('shrinks the font as more text is typed', () => {
    const short = fittedPixelSize('retries three times', 200, 120, measure);
    const long = fittedPixelSize('retries three times, then reports an error to the operator station', 200, 120, measure);
    expect(long).toBeLessThan(short);
    const result = measure('retries three times, then reports an error to the operator station', long, 200);
    expect(result.width).toBeLessThanOrEqual(200);
    expect(result.height).toBeLessThanOrEqual(120);
  });

  it('never splits a word: a long word limits the size too', () => {
    const word = 'Supercalifragilisticexpialidocious'; // 34 characters
    const size = fittedPixelSize(word, 170, 400, measure);
    expect(size).toBe(10); // 34 * 0.5 * 10 = 170
  });

  it('grows as the note is enlarged', () => {
    expect(fittedPixelSize('a note', 300, 200, measure)).toBeGreaterThan(fittedPixelSize('a note', 150, 100, measure));
  });

  it('falls back to the smallest size when nothing fits or there is no text', () => {
    expect(fittedPixelSize('', 100, 100, measure)).toBe(MIN_PIXEL_SIZE);
    expect(fittedPixelSize('text', 0, 100, measure)).toBe(MIN_PIXEL_SIZE);
    expect(fittedPixelSize('x'.repeat(5000), 10, 10, measure)).toBe(MIN_PIXEL_SIZE);
  });
});
