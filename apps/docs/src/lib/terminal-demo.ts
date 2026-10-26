export type TabId = "docs" | "tests";

export interface Line {
  kind: "command" | "output" | "success";
  text: string;
}

export interface Tab {
  busy: boolean;
  id: TabId;
  lines: Line[];
  mode: "codex" | "shell";
  name: string;
}

const MAX_INPUT_LENGTH = 300;
const MAX_LINES = 16;
const TASK_DURATION_MS = 2200;
const CONNECT_COMMANDS: ReadonlySet<string> = new Set(["sarj-connect terminal", "vm", "vm terminal", "sarj-vm", "sarj-vm terminal"]);

interface DemoActions {
  cancel(): void;
  readonly currentTab: Tab;
  detach(): void;
  dispose(): void;
  readonly inputLabel: string;
  readonly prompt: string;
  reset(): void;
  selectTab(id: TabId): void;
  submit(text: string): void;
  readonly suggestedInput: string;
}

function createTabs(): Record<TabId, Tab> {
  return {
    docs: { id: "docs", name: "Docs", mode: "shell", lines: [], busy: false },
    tests: { id: "tests", name: "Tests", mode: "shell", lines: [], busy: false },
  };
}

function append(lines: Line[], ...entries: Line[]): void {
  lines.push(...entries);
  lines.splice(0, Math.max(0, lines.length - MAX_LINES));
}

function response(tab: Tab, task: string): string {
  if (/\b(?:test|tests|check|verify)\b/iu.test(task)) return "Ran the demo tests.\n12 passed. Everything is green.";
  if (/\b(?:explain|summarize|summary|what)\b/iu.test(task)) return "This demo project has a small docs site and its tests.\nYour terminal tabs stay here when you disconnect.";
  return tab.id === "docs"
    ? "Simplified the demo README.\nOne connection command, two clear examples. Ready to review."
    : "Added a reconnect test to the demo project.\n12 tests passed. Ready to review.";
}

export class TerminalDemo implements DemoActions {
  attached = false;
  connectedBefore = false;
  active: TabId = "docs";
  localLines: Line[] = [];
  tabs = createTabs();
  status = "";
  readonly #onChange: () => void;
  readonly #timers = new Map<TabId, ReturnType<typeof setTimeout>>();
  #disposed = false;

  constructor(onChange: () => void) {
    this.#onChange = onChange;
  }

  get currentTab(): Tab {
    return this.tabs[this.active];
  }

  get inputLabel(): string {
    if (!this.attached) return "Local terminal command";
    return this.currentTab.mode === "codex" ? `${this.currentTab.name} Codex prompt` : `${this.currentTab.name} terminal command`;
  }

  get suggestedInput(): string {
    if (!this.attached) return "sarj-connect terminal";
    if (this.currentTab.mode === "shell") return "codex";
    return this.active === "docs" ? "Simplify the README" : "Run the tests";
  }

  get prompt(): string {
    return this.attached && this.currentTab.mode === "codex" ? "›" : "$";
  }

  submit(text: string): void {
    if (this.#disposed || (this.attached && this.currentTab.busy)) return;
    const lines = this.attached ? this.currentTab.lines : this.localLines;
    if (text.length > MAX_INPUT_LENGTH) {
      append(lines, { kind: "output", text: "Keep your command under 300 characters." });
      this.#announce("Command is too long.");
      return;
    }
    const command = text.replaceAll(/\s+/gu, " ").trim();
    if (!command) return;
    append(lines, { kind: "command", text: `${this.prompt} ${command}` });
    if (!this.attached) this.#localCommand(command);
    else if (this.currentTab.mode === "shell") this.#shellCommand(command);
    else this.#codexCommand(command);
  }

  selectTab(id: TabId): void {
    if (this.#disposed || !this.attached || this.active === id) return;
    this.active = id;
    this.#announce(`${this.currentTab.name} tab selected.`);
  }

  detach(): void {
    if (this.#disposed || !this.attached) return;
    this.attached = false;
    append(this.localLines, { kind: "output", text: "Detached. Your tabs keep running. Run sarj-connect terminal to return." });
    this.#announce("Detached. Your tabs are still running.");
  }

  cancel(): void {
    if (this.#disposed || !this.attached || !this.currentTab.busy) return;
    const tab = this.currentTab;
    clearTimeout(this.#timers.get(tab.id));
    this.#timers.delete(tab.id);
    tab.busy = false;
    append(tab.lines, { kind: "output", text: "Interrupted. Your conversation is still here." });
    this.#announce(`${tab.name}: task interrupted.`);
  }

  reset(): void {
    if (this.#disposed) return;
    this.#clearTimers();
    this.attached = false;
    this.connectedBefore = false;
    this.active = "docs";
    this.localLines = [];
    this.tabs = createTabs();
    this.#announce("Demo reset.");
  }

  dispose(): void {
    this.#clearTimers();
    this.#disposed = true;
  }

  #localCommand(command: string): void {
    if (CONNECT_COMMANDS.has(command)) {
      this.attached = true;
      const message = this.connectedBefore ? "Reconnected. Both tabs are just as you left them." : "Connected. Start Codex in either tab.";
      append(this.localLines, { kind: "success", text: message });
      this.connectedBefore = true;
      this.#announce(message);
      return;
    }
    if (/^(?:sarj-connect|sarj-vm|vm) browser(?: |$)/u.test(command)) {
      append(this.localLines, { kind: "output", text: "The real browser command opens your persistent VM browser. This demo focuses on terminal tabs." });
    } else if (command === "clear") this.localLines = [];
    else append(this.localLines, { kind: "output", text: "Try sarj-connect terminal to connect to the demo VM." });
    this.#announce("Local command complete.");
  }

  #shellCommand(command: string): void {
    const tab = this.currentTab;
    switch (command) {
      case "codex":
        tab.mode = "codex";
        append(tab.lines, { kind: "output", text: `Codex · simulated\n~/demo/${tab.id}\nWhat would you like to work on?` });
        break;
      case "pwd":
        append(tab.lines, { kind: "output", text: `~/demo/${tab.id}` });
        break;
      case "ls":
        append(tab.lines, { kind: "output", text: "README.md  src/  tests/" });
        break;
      case "clear":
        tab.lines = [];
        break;
      case "help":
        append(tab.lines, { kind: "output", text: "Try codex, ls, pwd, or clear.\nAlt + 1 / 2 switches tabs. Alt + d detaches." });
        break;
      default:
        append(tab.lines, { kind: "output", text: "This demo supports codex, ls, pwd, and clear. Type help for shortcuts." });
    }
    this.#announce(`${tab.name}: ${command === "codex" ? "Codex ready." : "command complete."}`);
  }

  #codexCommand(command: string): void {
    const tab = this.currentTab;
    switch (command) {
      case "/exit":
        tab.mode = "shell";
        append(tab.lines, { kind: "output", text: "Back at the shell. Type codex to start again." });
        break;
      case "/clear":
        tab.lines = [];
        break;
      case "/help":
        append(tab.lines, { kind: "output", text: "Ask for a demo change, or use /clear and /exit.\nCtrl + C interrupts the current task." });
        break;
      default:
        if (command.startsWith("/")) append(tab.lines, { kind: "output", text: "Try /help, /clear, or /exit." });
        else {
          tab.busy = true;
          const timer = setTimeout(() => {
            this.#timers.delete(tab.id);
            tab.busy = false;
            append(tab.lines, { kind: "success", text: response(tab, command) });
            this.#announce(`${tab.name}: demo task finished.`);
          }, TASK_DURATION_MS);
          this.#timers.set(tab.id, timer);
          this.status = "";
          this.#onChange();
          return;
        }
    }
    this.#announce(`${tab.name}: command complete.`);
  }

  #clearTimers(): void {
    for (const timer of this.#timers.values()) clearTimeout(timer);
    this.#timers.clear();
    for (const tab of Object.values(this.tabs)) tab.busy = false;
  }

  #announce(message: string): void {
    this.status = message;
    this.#onChange();
  }
}
