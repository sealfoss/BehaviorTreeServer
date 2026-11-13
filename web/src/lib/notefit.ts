/**
 * Note text size (design 11.4, P-08): the largest font at which all of the text fits, found by
 * measuring, as the widget's `notes.fitted_pixel_size` does. Lines break between words only,
 * so a word too long for the width makes the font smaller as well.
 */

export const MIN_PIXEL_SIZE = 1;
export const MAX_PIXEL_SIZE = 2000;
/** Between the border and the text (the widget's TEXT_MARGIN). */
export const TEXT_MARGIN = 7;

/** Measures `text` word-wrapped at `width` with a font of `pixels`: the size it takes. */
export type Measure = (text: string, pixels: number, width: number) => { width: number; height: number };

/** The largest pixel size at which `text` fits `width` x `height`; MIN_PIXEL_SIZE when nothing fits. */
export function fittedPixelSize(text: string, width: number, height: number, measure: Measure): number {
  if (width <= 0 || height <= 0 || !text) return MIN_PIXEL_SIZE;
  const fits = (pixels: number): boolean => {
    const size = measure(text, pixels, width);
    return size.width <= width && size.height <= height;
  };
  let low = MIN_PIXEL_SIZE;
  let high = Math.max(MIN_PIXEL_SIZE, Math.min(Math.floor(height), MAX_PIXEL_SIZE));
  while (low < high) {
    const middle = Math.floor((low + high + 1) / 2);
    if (fits(middle)) low = middle;
    else high = middle - 1;
  }
  return low;
}

/**
 * A measuring function built on a hidden element of the page: the text is laid out with
 * normal word wrapping (never inside a word), and its scroll size is the size it needs.
 */
export function domMeasure(probe: HTMLElement): Measure {
  return (text, pixels, width) => {
    probe.style.fontSize = `${pixels}px`;
    probe.style.width = `${width}px`;
    probe.textContent = text;
    return { width: probe.scrollWidth, height: probe.scrollHeight };
  };
}
