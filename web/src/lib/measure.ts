/**
 * Node sizes as drawn. One ResizeObserver for every node element; the sizes (in world units:
 * layout sizes are not affected by the world layer's transform) go to the view store, where
 * the connection lines, the child order and the paste placement read them.
 */

import type { NodeSize } from './stores/view.svelte';

type Sink = (id: string, size: NodeSize) => void;

let observer: ResizeObserver | null = null;
const sinks = new Map<Element, { id: string; sink: Sink }>();

function measureElement(element: HTMLElement): NodeSize {
  // A label not drawn (title and status only, below 40% zoom) has no box: the node's edges stand in.
  const shown = (label: HTMLElement | null) => (label && label.offsetParent !== null ? label : null);
  const parent = shown(element.querySelector<HTMLElement>('[data-label="parent"]'));
  const children = shown(element.querySelector<HTMLElement>('[data-label="children"]'));
  return {
    w: element.offsetWidth,
    h: element.offsetHeight,
    parentTop: parent ? offsetWithin(parent, element) : 0,
    childrenBottom: children ? offsetWithin(children, element) + children.offsetHeight : element.offsetHeight,
  };
}

function offsetWithin(child: HTMLElement, ancestor: HTMLElement): number {
  let top = 0;
  let current: HTMLElement | null = child;
  while (current && current !== ancestor) {
    top += current.offsetTop;
    current = current.offsetParent as HTMLElement | null;
  }
  return top;
}

/** Svelte action: report the element's size whenever it changes. */
export function measured(element: HTMLElement, params: { id: string; sink: Sink }) {
  observer ??= new ResizeObserver((records) => {
    for (const record of records) {
      const target = sinks.get(record.target);
      if (target) target.sink(target.id, measureElement(record.target as HTMLElement));
    }
  });
  sinks.set(element, params);
  observer.observe(element);
  params.sink(params.id, measureElement(element));
  return {
    update(next: { id: string; sink: Sink }) {
      sinks.set(element, next);
    },
    destroy() {
      sinks.delete(element);
      observer?.unobserve(element);
    },
  };
}
