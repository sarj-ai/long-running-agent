export type TabId = string;
export type PaneId = string;
export type SplitDirection = "horizontal" | "vertical";
export type FocusDirection = "down" | "left" | "right" | "up";

export interface Line {
  kind: "command" | "output" | "success";
  text: string;
}

export interface Pane {
  busy: boolean;
  id: PaneId;
  lines: Line[];
  mode: "codex" | "shell";
  name: string;
}

export interface Tab {
  activePane: PaneId;
  fullscreen: boolean;
  id: TabId;
  name: string;
  panes: Pane[];
  path: string;
  split: SplitDirection;
}

interface DemoActions {
  cancel(): void;
  closePane(): void;
  closeTab(): void;
  readonly currentPane: Pane;
  readonly currentTab: Tab;
  detach(): void;
  dispose(): void;
  readonly inputLabel: string;
  moveFocus(direction: FocusDirection): void;
  newTab(): void;
  readonly orderedTabs: Tab[];
  readonly prompt: string;
  renamePane(name: string): void;
  renameTab(name: string): void;
  reset(): void;
  selectPane(id: PaneId): void;
  selectTab(id: TabId): void;
  splitPane(direction: SplitDirection): void;
  submit(text: string): void;
  readonly suggestedInput: string;
  toggleFullscreen(): void;
}

const MAX_INPUT_LENGTH = 300;
const MAX_LINES = 32;
const MAX_TABS = 9;
const MAX_NAME_LENGTH = 30;
const TASK_DURATION_MS = 2200;
const CONNECT_COMMANDS: ReadonlySet<string> = new Set(["sarj-connect terminal", "vm", "vm terminal", "sarj-vm", "sarj-vm terminal"]);

function printable(text: string): string {
  return text.replaceAll(/\s+/gu, " ").replaceAll(/\p{Cc}/gu, "").trim();
}

function shortName(text: string): string {
  const segments = new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(printable(text));
  return Array.from(segments, ({ segment }) => segment).slice(0, MAX_NAME_LENGTH).join("");
}

function makePane(id: PaneId, mode: Pane["mode"] = "shell", lines: Line[] = []): Pane {
  return { id, mode, lines, busy: false, name: "Pane #1" };
}

function makeTab(id: TabId, name: string, pane: Pane): Tab {
  return { id, name, panes: [pane], activePane: pane.id, split: "vertical", fullscreen: false, path: `~/demo/${id}` };
}

function initialTabs(): Partial<Record<TabId, Tab>> {
  return {
    docs: makeTab("docs", "docs", makePane("docs-1", "codex", [
      { kind: "command", text: "› Simplify the README" },
      { kind: "success", text: "Updated README.md with a shorter quickstart.\nOne connection command and two examples." },
    ])),
    tests: makeTab("tests", "tests", makePane("tests-1", "codex", [
      { kind: "command", text: "› Run the tests" },
      { kind: "success", text: "12 passed in 0.42s. Everything is green." },
    ])),
    shell: makeTab("shell", "shell", makePane("shell-1")),
  };
}

function append(lines: Line[], ...entries: Line[]): void {
  lines.push(...entries);
  lines.splice(0, Math.max(0, lines.length - MAX_LINES));
}

function response(task: string): string {
  if (/\b(?:test|tests|check|verify)\b/iu.test(task)) return "Ran the demo tests.\n12 passed in 0.42s.";
  if (/\b(?:explain|summarize|summary|what)\b/iu.test(task)) return "This sample project has a docs site and its tests.\nEach Codex conversation runs in its own terminal pane.";
  return "Updated the demo README.\nThe shorter quickstart is ready to review.";
}

export class TerminalDemo implements DemoActions {
  attached = false;
  connectedBefore = false;
  active: TabId = "docs";
  localLines: Line[] = [];
  tabs: Partial<Record<TabId, Tab>> = initialTabs();
  status = "";
  readonly #onChange: () => void;
  readonly #timers = new Map<PaneId, ReturnType<typeof setTimeout>>();
  #order: TabId[] = ["docs", "tests", "shell"];
  #tabIds = new Map<TabId, number>([["docs", 0], ["tests", 1], ["shell", 2]]);
  #tabHistory: TabId[] = [];
  #nextId = 4;
  #disposed = false;
  #sessionEnded = false;

  constructor(onChange: () => void) {
    this.#onChange = onChange;
  }

  get orderedTabs(): Tab[] {
    return this.#order.map((id) => {
      const tab = this.tabs[id];
      if (!tab) throw new Error("The selected demo tab no longer exists.");
      return tab;
    });
  }

  get currentTab(): Tab {
    const tab = this.tabs[this.active];
    if (!tab) throw new Error("The selected demo tab no longer exists.");
    return tab;
  }

  get currentPane(): Pane {
    const pane = this.currentTab.panes.find((candidate) => candidate.id === this.currentTab.activePane);
    if (!pane) throw new Error("The selected demo pane no longer exists.");
    return pane;
  }

  get inputLabel(): string {
    if (!this.attached) return "Local terminal command";
    return `${this.currentTab.name} ${this.currentPane.mode === "codex" ? "Codex prompt" : "terminal command"}`;
  }

  get suggestedInput(): string {
    return this.attached ? "" : "sarj-connect terminal";
  }

  get prompt(): string {
    return this.attached && this.currentPane.mode === "codex" ? "›" : "❯";
  }

  submit(text: string): void {
    if (this.#disposed || (this.attached && this.currentPane.busy)) return;
    const lines = this.attached ? this.currentPane.lines : this.localLines;
    if (text.length > MAX_INPUT_LENGTH) {
      append(lines, { kind: "output", text: "Keep your command to 300 characters or fewer." });
      this.#announce("Command is too long.");
      return;
    }
    const command = printable(text);
    if (!command) return;
    append(lines, { kind: "command", text: `${this.prompt} ${command}` });
    if (!this.attached) this.#localCommand(command);
    else if (this.currentPane.mode === "shell") this.#shellCommand(command);
    else this.#codexCommand(command);
  }

  selectTab(id: TabId): void {
    if (this.#disposed || !this.attached || !Object.hasOwn(this.tabs, id) || this.active === id) return;
    this.#focusTab(id);
    this.#announce(`${this.currentTab.name} tab selected.`);
  }

  selectPane(id: PaneId): void {
    if (this.#disposed || !this.attached || !this.currentTab.panes.some((pane) => pane.id === id)) return;
    this.currentTab.activePane = id;
    this.#announce(`${this.currentPane.name} pane selected.`);
  }

  newTab(): void {
    if (this.#disposed || !this.attached) return;
    if (this.#order.length >= MAX_TABS) {
      this.#announce("This sample session has room for nine tabs.");
      return;
    }
    const number = Math.max(-1, ...this.#tabIds.values()) + 1;
    const id = `tab-${String(number + 1)}`;
    const tab = makeTab(id, `Tab #${String(number + 1)}`, makePane(`pane-${String(this.#nextId++)}`));
    tab.path = "~/demo";
    this.tabs[id] = tab;
    this.#order.push(id);
    this.#tabIds.set(id, number);
    this.#focusTab(id);
    this.#announce("New tab opened.");
  }

  closeTab(): void {
    if (this.#disposed || !this.attached) return;
    const index = this.#order.indexOf(this.active);
    for (const pane of this.currentTab.panes) this.#stopPane(pane);
    Reflect.deleteProperty(this.tabs, this.active);
    this.#tabIds.delete(this.active);
    this.#order.splice(index, 1);
    const previous = this.#tabHistory.pop();
    const next = previous && Object.hasOwn(this.tabs, previous) ? previous : this.#order.at(0);
    if (next) {
      this.active = next;
      this.#announce("Tab closed.");
      return;
    }
    const id = "tab-1";
    const fresh = makeTab(id, "Tab #1", makePane(`pane-${String(this.#nextId++)}`));
    fresh.path = "~/demo";
    this.tabs[id] = fresh;
    this.#order = [id];
    this.#tabIds = new Map([[id, 0]]);
    this.#tabHistory = [];
    this.active = id;
    this.attached = false;
    this.#sessionEnded = true;
    append(this.localLines, { kind: "output", text: "Session ended. Run sarj-connect terminal to start a new one." });
    this.#announce("The last tab closed. Session ended.");
  }

  splitPane(direction: SplitDirection): void {
    if (this.#disposed || !this.attached) return;
    const tab = this.currentTab;
    if (tab.panes.length >= 2) {
      this.#announce("This sample tab has room for two panes.");
      return;
    }
    const pane = makePane(`pane-${String(this.#nextId++)}`);
    pane.name = `Pane #${String(tab.panes.length + 1)}`;
    tab.panes.push(pane);
    tab.activePane = pane.id;
    tab.split = direction;
    tab.fullscreen = false;
    this.#announce("New pane opened.");
  }

  closePane(): void {
    if (this.#disposed || !this.attached) return;
    const tab = this.currentTab;
    if (tab.panes.length === 1) {
      this.closeTab();
      return;
    }
    this.#stopPane(this.currentPane);
    tab.panes = tab.panes.filter((pane) => pane.id !== tab.activePane);
    const remaining = tab.panes.at(0);
    if (remaining) tab.activePane = remaining.id;
    tab.fullscreen = false;
    this.#announce("Pane closed.");
  }

  moveFocus(direction: FocusDirection): void {
    if (this.#disposed || !this.attached) return;
    const tab = this.currentTab;
    const horizontal = direction === "left" || direction === "right";
    const forward = direction === "right" || direction === "down";
    if (tab.panes.length > 1 && (horizontal ? tab.split === "vertical" : tab.split === "horizontal")) {
      const index = tab.panes.findIndex((pane) => pane.id === tab.activePane);
      const target = index + (forward ? 1 : -1);
      const next = target < 0 ? undefined : tab.panes.at(target);
      if (next) {
        this.selectPane(next.id);
        return;
      }
    }
    if (horizontal) this.#focusAdjacentTab(direction);
  }

  toggleFullscreen(): void {
    if (this.#disposed || !this.attached) return;
    this.currentTab.fullscreen = !this.currentTab.fullscreen;
    this.#announce(this.currentTab.fullscreen ? "Pane fullscreen." : "Pane fullscreen closed.");
  }

  renameTab(name: string): void {
    if (this.#disposed || !this.attached) return;
    this.currentTab.name = shortName(name);
    this.#announce("Tab renamed.");
  }

  renamePane(name: string): void {
    if (this.#disposed || !this.attached) return;
    this.currentPane.name = shortName(name);
    this.#announce("Pane renamed.");
  }

  detach(): void {
    if (this.#disposed || !this.attached) return;
    this.attached = false;
    this.#tabHistory = [];
    append(this.localLines, { kind: "output", text: "[detached from sarj-home]" });
    this.#announce("Detached. Your tabs are still running.");
  }

  cancel(): void {
    if (this.#disposed || !this.attached || !this.currentPane.busy) return;
    const pane = this.currentPane;
    this.#stopPane(pane);
    append(pane.lines, { kind: "output", text: "■ Conversation interrupted — tell the model what to do differently." });
    this.#announce(`${this.currentTab.name}: task interrupted.`);
  }

  reset(): void {
    if (this.#disposed) return;
    this.#clearTimers();
    this.attached = false;
    this.connectedBefore = false;
    this.active = "docs";
    this.localLines = [];
    this.tabs = initialTabs();
    this.#order = ["docs", "tests", "shell"];
    this.#tabIds = new Map([["docs", 0], ["tests", 1], ["shell", 2]]);
    this.#tabHistory = [];
    this.#nextId = 4;
    this.#sessionEnded = false;
    this.#announce("Demo reset.");
  }

  dispose(): void {
    this.#clearTimers();
    this.#disposed = true;
  }

  #localCommand(command: string): void {
    if (CONNECT_COMMANDS.has(command)) {
      this.attached = true;
      const message = this.#sessionEnded ? "Connected to a new session." : (this.connectedBefore ? "Reconnected to sarj-home." : "Connected to sarj-home.");
      append(this.localLines, { kind: "success", text: message });
      this.connectedBefore = true;
      this.#sessionEnded = false;
      this.#announce(message);
      return;
    }
    if (/^(?:sarj-connect|sarj-vm|vm) browser(?: |$)/u.test(command)) append(this.localLines, { kind: "output", text: "The browser command opens your VM browser. This sample session demonstrates terminal tabs." });
    else if (command === "clear") this.localLines = [];
    else if (command === "help") append(this.localLines, { kind: "output", text: "sarj-connect terminal — connect to the sample session" });
    else append(this.localLines, { kind: "output", text: `zsh: command not found: ${command.split(" ", 1)[0]}` });
    this.#announce("Local command complete.");
  }

  #shellCommand(command: string): void {
    const pane = this.currentPane;
    switch (command) {
      case "codex":
        pane.mode = "codex";
        break;
      case "pwd":
        append(pane.lines, { kind: "output", text: this.currentTab.path });
        break;
      case "ls":
        append(pane.lines, { kind: "output", text: "README.md  src/  tests/" });
        break;
      case "clear":
        pane.lines = [];
        break;
      case "exit":
        this.closePane();
        return;
      case "help":
        append(pane.lines, { kind: "output", text: "codex  ls  pwd  clear  exit\nAlt + 1 / 2 / 3 selects a tab. Alt + d detaches." });
        break;
      default:
        append(pane.lines, { kind: "output", text: `zsh: command not found: ${command.split(" ", 1)[0]}` });
    }
    this.#announce(`${this.currentTab.name}: ${command === "codex" ? "Codex ready." : "command complete."}`);
  }

  #codexCommand(command: string): void {
    const pane = this.currentPane;
    switch (command) {
      case "/exit":
        pane.mode = "shell";
        break;
      case "/clear":
        pane.lines = [];
        break;
      case "/help":
        append(pane.lines, { kind: "output", text: "/clear   clear this conversation\n/exit    return to the shell\nCtrl + C interrupts the current task." });
        break;
      default:
        if (command.startsWith("/")) append(pane.lines, { kind: "output", text: "Unrecognized command. Type /help for available commands." });
        else {
          pane.busy = true;
          const name = this.currentTab.name;
          const timer = setTimeout(() => {
            this.#timers.delete(pane.id);
            pane.busy = false;
            append(pane.lines, { kind: "success", text: response(command) });
            this.#announce(`${name}: demo task finished.`);
          }, TASK_DURATION_MS);
          this.#timers.set(pane.id, timer);
          this.status = "";
          this.#onChange();
          return;
        }
    }
    this.#announce(`${this.currentTab.name}: command complete.`);
  }

  #stopPane(pane: Pane): void {
    clearTimeout(this.#timers.get(pane.id));
    this.#timers.delete(pane.id);
    pane.busy = false;
  }

  #focusTab(id: TabId): void {
    this.#tabHistory = this.#tabHistory.filter((previous) => previous !== id);
    this.#tabHistory.push(this.active);
    this.active = id;
  }

  #focusAdjacentTab(direction: "left" | "right"): void {
    const index = this.#order.indexOf(this.active);
    const offset = direction === "right" ? 1 : -1;
    const id = this.#order[(index + offset + this.#order.length) % this.#order.length];
    if (!id || id === this.active) return;
    this.#focusTab(id);
    const destination = this.currentTab;
    if (destination.split === "vertical") {
      const edge = destination.panes.at(direction === "right" ? 0 : -1);
      if (edge) destination.activePane = edge.id;
    }
    this.#announce(`${destination.name} tab selected.`);
  }

  #clearTimers(): void {
    for (const timer of this.#timers.values()) clearTimeout(timer);
    this.#timers.clear();
    for (const tab of this.orderedTabs) for (const pane of tab.panes) pane.busy = false;
  }

  #announce(message: string): void {
    this.status = message;
    this.#onChange();
  }
}
