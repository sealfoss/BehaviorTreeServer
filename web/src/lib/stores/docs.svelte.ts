/** The session's documents (design 11.2 `docs`): nodes, connections, options, revision. */

import { applyOp, type ApplyResult, type Op } from '../ops';
import type { TreeDocument } from '../types';

export class DocsStore {
  docs = $state<Record<string, TreeDocument>>({});
  /** The tree of the session (the one Execute runs). */
  root = $state<string | null>(null);

  replaceAll(docs: Record<string, TreeDocument>, root: string | null): void {
    this.docs = structuredClone(docs);
    this.root = root;
  }

  get(key: string | null | undefined): TreeDocument | undefined {
    return key ? this.docs[key] : undefined;
  }

  /** A document opened, saved, reverted, renamed or sent again. */
  set(doc: TreeDocument): void {
    this.docs[doc.key] = structuredClone(doc);
  }

  remove(key: string): void {
    delete this.docs[key];
  }

  /**
   * Apply an operation. `rev` is the revision the gateway gave it (a broadcast); local
   * operations leave the revision alone. `edit`: made by a browser, so an unsaved change.
   * Returns `resync` when the page needs the document again.
   */
  apply(key: string, op: Op, rev?: number, edit = false): ApplyResult {
    const doc = this.docs[key];
    if (!doc) return 'ignored';
    const result = applyOp(doc, op);
    if (rev !== undefined && rev > doc.rev) doc.rev = rev;
    // An edit by a browser is an unsaved change; values written by running leaves are not.
    if (edit && result === 'applied') doc.unsaved = true;
    return result;
  }
}
