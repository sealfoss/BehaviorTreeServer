/** On a Mac, Cmd takes the place of Ctrl in every gesture (design 11.4). */
export const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/** The "Ctrl" of the gestures: Cmd on a Mac. */
export function primary(event: { ctrlKey: boolean; metaKey: boolean }): boolean {
  return isMac ? event.metaKey : event.ctrlKey;
}

/** True while an element that edits text (or any form control) has the keyboard. */
export function isEditable(element: Element | null): boolean {
  if (!element) return false;
  const tag = element.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (element as HTMLElement).isContentEditable;
}
