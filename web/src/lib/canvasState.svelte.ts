/** Transient canvas state the node components read: the connection being dragged, the label under it. */

export interface LabelRef {
  id: string;
  kind: 'parent' | 'children';
}

class CanvasState {
  /** The label a connection is dragged from, and the compatible label under the pointer. */
  connectFrom = $state<LabelRef | null>(null);
  connectOver = $state<LabelRef | null>(null);
  /** The note whose text is being edited. */
  editingNote = $state<string | null>(null);
  /** The group whose name is being edited in its title bar. */
  editingGroup = $state<string | null>(null);
  /** Node to rename in place (title editor). */
  renaming = $state<string | null>(null);
}

export const canvasState = new CanvasState();

/** Pinned fields of ROS leaves (shown on the node) are kept per browser: no operation carries them. */
class Pins {
  byNode = $state<Record<string, string[]>>(loadPins());

  of(id: string): string[] {
    return this.byNode[id] ?? [];
  }

  set(id: string, fields: string[]): void {
    this.byNode[id] = fields;
    try {
      localStorage.setItem(`bt.pins.${id}`, JSON.stringify(fields));
    } catch {
      // Not kept in a private window.
    }
  }
}

function loadPins(): Record<string, string[]> {
  const result: Record<string, string[]> = {};
  try {
    for (let index = 0; index < localStorage.length; index++) {
      const key = localStorage.key(index);
      if (key?.startsWith('bt.pins.')) result[key.slice(8)] = JSON.parse(localStorage.getItem(key) ?? '[]') as string[];
    }
  } catch {
    // No storage: nothing pinned yet.
  }
  return result;
}

export const pins = new Pins();
