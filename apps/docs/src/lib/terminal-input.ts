const MAX_INPUT = 300;
const MAX_HISTORY = 20;
const MAX_DRAFTS = 80;
const GRAPHEME_SEGMENTER = new Intl.Segmenter(undefined, { granularity: "grapheme" });

interface Draft {
  cursor: number;
  draft: string;
  history: string[];
  historyIndex: number;
  text: string;
}

function characters(text: string): string[] {
  return [...GRAPHEME_SEGMENTER.segment(text)].map(({ segment }) => segment);
}

function bounded(text: string, limit = MAX_INPUT): string {
  const result: string[] = [];
  let length = 0;
  for (const character of characters(text)) {
    if (length + character.length > limit) break;
    result.push(character);
    length += character.length;
  }
  return result.join("");
}

export function printable(text: string): string {
  return text.replaceAll(/\p{Cc}/gu, "");
}

interface InputActions {
  readonly afterCursor: string;
  backspace(): void;
  readonly beforeCursor: string;
  clear(): void;
  context(key: string, suggested?: string): void;
  readonly current: Draft;
  delete(): void;
  deleteToEnd(): void;
  deleteWord(): void;
  end(): void;
  history(offset: -1 | 1): void;
  home(): void;
  insert(text: string): void;
  move(offset: number): void;
  replace(text: string): void;
  reset(): void;
  submit(): string;
}

export class TerminalInput implements InputActions {
  readonly #drafts = new Map<string, Draft>();
  #key = "local";

  constructor() {
    this.context("local", "sarj-connect terminal");
  }

  get current(): Draft {
    const current = this.#drafts.get(this.#key);
    if (!current) throw new Error("Terminal input context is missing.");
    return current;
  }

  get beforeCursor(): string {
    return characters(this.current.text).slice(0, this.current.cursor).join("");
  }

  get afterCursor(): string {
    return characters(this.current.text).slice(this.current.cursor).join("");
  }

  context(key: string, suggested = ""): void {
    const changed = key !== this.#key;
    this.#key = key;
    if (!this.#drafts.has(key)) {
      if (this.#drafts.size >= MAX_DRAFTS) {
        const oldest = this.#drafts.keys().next().value;
        if (oldest !== undefined) this.#drafts.delete(oldest);
      }
      const text = bounded(printable(suggested));
      this.#drafts.set(key, { text, cursor: characters(text).length, draft: text, history: [], historyIndex: 0 });
    }
    if (changed && key === "local" && !this.current.text.trim()) this.replace("sarj-connect terminal");
  }

  replace(text: string): void {
    const current = this.current;
    current.text = bounded(printable(text));
    current.cursor = characters(current.text).length;
    current.historyIndex = current.history.length;
    current.draft = current.text;
  }

  insert(text: string): void {
    const current = this.current;
    const clean = printable(text.replaceAll(/\r?\n|\r/gu, " "));
    const head = this.beforeCursor;
    const tail = this.afterCursor;
    const inserted = bounded(clean, Math.max(0, MAX_INPUT - current.text.length));
    current.text = `${head}${inserted}${tail}`;
    current.cursor = characters(`${head}${inserted}`).length;
    this.#edited();
  }

  move(offset: number): void {
    this.current.cursor = Math.max(0, Math.min(characters(this.current.text).length, this.current.cursor + offset));
  }

  home(): void {
    this.current.cursor = 0;
  }

  end(): void {
    this.current.cursor = characters(this.current.text).length;
  }

  backspace(): void {
    const current = this.current;
    if (current.cursor === 0) return;
    const parts = characters(current.text);
    parts.splice(current.cursor - 1, 1);
    current.text = parts.join("");
    current.cursor -= 1;
    this.#edited();
  }

  delete(): void {
    const parts = characters(this.current.text);
    parts.splice(this.current.cursor, 1);
    this.current.text = parts.join("");
    this.#edited();
  }

  deleteWord(): void {
    const head = this.beforeCursor.replace(/\s*\S+\s*$/u, "");
    this.current.text = `${head}${this.afterCursor}`;
    this.current.cursor = characters(head).length;
    this.#edited();
  }

  deleteToEnd(): void {
    this.current.text = this.beforeCursor;
    this.#edited();
  }

  clear(): void {
    this.replace("");
  }

  history(offset: -1 | 1): void {
    const current = this.current;
    if (current.history.length === 0) return;
    if (current.historyIndex === current.history.length) current.draft = current.text;
    current.historyIndex = Math.max(0, Math.min(current.history.length, current.historyIndex + offset));
    current.text = current.historyIndex === current.history.length ? current.draft : (current.history[current.historyIndex] ?? "");
    current.cursor = characters(current.text).length;
  }

  submit(): string {
    const current = this.current;
    const text = current.text.trim();
    if (text && current.history.at(-1) !== text) {
      current.history.push(text);
      current.history.splice(0, Math.max(0, current.history.length - MAX_HISTORY));
    }
    this.clear();
    return text;
  }

  reset(): void {
    this.#drafts.clear();
    this.context("local", "sarj-connect terminal");
  }

  #edited(): void {
    this.current.historyIndex = this.current.history.length;
    this.current.draft = this.current.text;
  }
}
