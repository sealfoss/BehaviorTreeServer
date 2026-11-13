/** What the page shows besides the tree: notices, the open dialog, menus, findings, recovery. */

import type { Finding } from '../types';

export interface Notice {
  id: number;
  level: 'info' | 'warning' | 'error';
  text: string;
}

/** A question to the user: buttons, optionally a text field or a list to choose from. */
export interface Question {
  title: string;
  text: string;
  buttons: { label: string; value: string; primary?: boolean }[];
  input?: { label: string; value: string };
  choices?: string[];
  testid?: string;
}

export interface MenuItem {
  label: string;
  action?: () => void;
  disabled?: boolean;
  checked?: boolean;
  separator?: boolean;
  heading?: boolean;
  submenu?: MenuItem[];
  color?: string;
}

export type PanelDialog = 'files' | 'configure' | 'source' | 'findings' | 'entry' | null;

export class UiStore {
  notices = $state<Notice[]>([]);
  question = $state<(Question & { resolve: (answer: { button: string; input: string } | null) => void }) | null>(null);
  menu = $state<{ x: number; y: number; items: MenuItem[] } | null>(null);
  dialog = $state<PanelDialog>(null);
  /** The entry the Source dialog (or the whole-value view) is for. */
  dialogEntry = $state<string | null>(null);
  findings = $state<Finding[]>([]);
  /** Findings of an Execute that waits for the user's go-ahead (design 9.5). */
  findingsNeedConfirm = $state(false);
  recovery = $state<string[]>([]);
  lostEdits = $state<string[]>([]);
  panelTab = $state<'blackboard' | 'log' | 'inspector'>('blackboard');
  private nextNotice = 1;

  notify(level: Notice['level'], text: string, ms = level === 'error' ? 10000 : 5000): void {
    const id = this.nextNotice++;
    this.notices.push({ id, level, text });
    if (ms > 0 && typeof setTimeout !== 'undefined') setTimeout(() => this.dismiss(id), ms);
  }

  dismiss(id: number): void {
    this.notices = this.notices.filter((notice) => notice.id !== id);
  }

  /** Ask a question; resolves with the button and the text typed, or null when dismissed. */
  ask(question: Question): Promise<{ button: string; input: string } | null> {
    this.question?.resolve(null);
    return new Promise((resolve) => {
      this.question = { ...question, resolve };
    });
  }

  answer(answer: { button: string; input: string } | null): void {
    const question = this.question;
    this.question = null;
    question?.resolve(answer);
  }

  openMenu(x: number, y: number, items: MenuItem[]): void {
    this.menu = { x, y, items };
  }

  closeMenu(): void {
    this.menu = null;
  }
}
