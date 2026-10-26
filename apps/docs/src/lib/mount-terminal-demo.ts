import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";

import { type FocusDirection, type Tab, TerminalDemo } from "./terminal-demo";
import { TerminalInput } from "./terminal-input";
import { type DemoMode, type HitTarget, terminalScreen } from "./terminal-screen";

const ESC = "\u{1B}";
const THEME = { background: "#1e1e2e", foreground: "#cdd6f4", cursor: "#f5e0dc", selectionBackground: "#585b7080", black: "#45475a", red: "#f38ba8", green: "#a6e3a1", yellow: "#f9e2af", blue: "#89b4fa", magenta: "#f5c2e7", cyan: "#94e2d5", white: "#bac2de", brightBlack: "#585b70", brightRed: "#f38ba8", brightGreen: "#a6e3a1", brightYellow: "#f9e2af", brightBlue: "#89b4fa", brightMagenta: "#f5c2e7", brightCyan: "#94e2d5", brightWhite: "#a6adc8" } as const;

export async function mountTerminalDemo(root: HTMLElement): Promise<void> {
  const host = root.querySelector<HTMLElement>("[data-terminal]");
  const instructions = root.querySelector<HTMLElement>("[data-instructions]");
  const status = root.querySelector<HTMLElement>("[data-status]");
  const transcript = root.querySelector<HTMLElement>("[data-transcript]");
  const action = root.querySelector<HTMLButtonElement>("[data-submit]");
  const detach = root.querySelector<HTMLButtonElement>("[data-detach]");
  const tabs = root.querySelector<HTMLElement>("[data-accessible-tabs]");
  if (!host || !instructions || !status || !transcript || !action || !detach || !tabs) return;
  const instanceId = `lra-terminal-${crypto.randomUUID()}`;
  instructions.id = `${instanceId}-instructions`;
  host.id = `${instanceId}-panel`;
  host.setAttribute("aria-describedby", instructions.id);
  await Promise.allSettled([document.fonts.load("14px LRATerminal"), document.fonts.load("700 14px LRATerminal")]);
  if (!root.isConnected) return;
  const lifetime = new AbortController();
  const term = new Terminal({ cols: 110, rows: 32, fontFamily: "LRATerminal, monospace", fontSize: 14, lineHeight: 1, cursorBlink: false, cursorStyle: "block", cursorInactiveStyle: "outline", convertEol: false, scrollback: 0, screenReaderMode: false, theme: THEME, allowProposedApi: false });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.open(host);
  term.textarea?.setAttribute("aria-describedby", instructions.id);
  const focusTerminal = (): void => { term.textarea?.focus({ preventScroll: true }); };
  const input = new TerminalInput();
  let mode: DemoMode = "locked";
  const ratios = new WeakMap<Tab, number>();
  let scroll = 0;
  let targets: HitTarget[] = [];
  let originalName = "";
  let accessibleTabKey = "";
  let writing = false;
  let ready = false;
  let paintPending = false;
  let alternateBuffer = false;
  let requestedPaint = 0;
  let dimensions: { cols: number; rows: number } | undefined;

  const paint = (): void => {
    if (!root.isConnected) return;
    if (writing) { paintPending = true; return; }
    if (dimensions) {
      term.resize(dimensions.cols, dimensions.rows);
      dimensions = undefined;
    }
    const columns = term.cols;
    const rows = term.rows;
    const token = requestedPaint;
    root.dataset.columns = String(columns);
    root.dataset.rows = String(rows);
    const frame = terminalScreen({ demo, input, mode, columns, rows, ratio: ratios.get(demo.currentTab) ?? 0.5, scroll });
    targets = frame.targets;
    writing = true;
    const enterBuffer = alternateBuffer ? "" : `${ESC}[?1049h`;
    alternateBuffer = true;
    term.write(`${enterBuffer}${frame.ansi}`, () => {
      root.dataset.paintedColumns = String(columns);
      root.dataset.paintedRows = String(rows);
      root.dataset.paintedToken = String(token);
      root.dataset.buffer = term.buffer.active.type;
      if (!ready) {
        ready = true;
        root.setAttribute("aria-busy", "false");
        root.dataset.ready = "true";
        for (const button of root.querySelectorAll<HTMLButtonElement>("button")) button.disabled = false;
      }
      writing = false;
      if (paintPending || requestedPaint !== token || dimensions) { paintPending = false; paint(); }
    });
  };

  const context = (): string => {
    if (!demo.attached) return "local";
    if (mode === "rename-tab") return `rename-tab:${demo.active}`;
    if (mode === "rename-pane") return `rename-pane:${demo.currentPane.id}`;
    return `${demo.currentPane.id}:${demo.currentPane.mode}`;
  };

  const render = (): void => {
    input.context(context(), demo.suggestedInput);
    requestedPaint += 1;
    root.dataset.paintRequested = String(requestedPaint);
    paint();
    status.textContent = demo.status;
    const lines = demo.attached ? demo.currentPane.lines : demo.localLines;
    const transcriptText = [demo.attached ? `VM · ${demo.currentTab.name} · ${demo.currentPane.name}` : "Your Mac", ...lines.map((line) => line.text), `${demo.prompt} ${input.current.text}`].join("\n");
    if (transcript.textContent !== transcriptText) transcript.textContent = transcriptText;
    root.dataset.attached = String(demo.attached);
    root.dataset.mode = mode;
    root.dataset.activeTab = demo.active;
    root.dataset.activePane = demo.currentPane.id;
    root.dataset.busy = String(demo.attached && demo.currentPane.busy);
    root.dataset.columns = String(term.cols);
    root.dataset.rows = String(term.rows);
    term.textarea?.setAttribute("aria-label", mode.startsWith("rename-") ? "Terminal name" : demo.inputLabel);
    action.textContent = demo.attached ? (demo.currentPane.busy ? "Stop" : (demo.currentPane.mode === "codex" ? "Send ↵" : "Run ↵")) : (demo.connectedBefore ? "Reconnect ↵" : "Connect ↵");
    detach.hidden = !demo.attached;
    tabs.hidden = !demo.attached;
    const tabKey = demo.orderedTabs.map((tab) => `${tab.id}:${tab.name}`).join("|");
    if (tabKey !== accessibleTabKey) {
      const buttons = demo.orderedTabs.map((tab, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.disabled = !ready;
        button.dataset.tab = tab.id;
        button.id = `${instanceId}-${tab.id}`;
        button.setAttribute("aria-controls", host.id);
        button.setAttribute("role", "tab");
        button.textContent = `${String(index + 1)} ${tab.name || "Tab"}`;
        return button;
      });
      tabs.replaceChildren(...buttons);
      accessibleTabKey = tabKey;
    }
    for (const button of tabs.querySelectorAll<HTMLButtonElement>("button")) {
      const selected = button.dataset.tab === demo.active;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    }
    host.setAttribute("role", demo.attached ? "tabpanel" : "region");
    if (demo.attached) {
      host.setAttribute("aria-labelledby", `${instanceId}-${demo.active}`);
      host.removeAttribute("aria-label");
    } else {
      host.removeAttribute("aria-labelledby");
      host.setAttribute("aria-label", "Local terminal");
    }
  };
  const demo = new TerminalDemo(render);

  const submit = (): void => {
    if (demo.attached && demo.currentPane.busy) { demo.cancel(); return; }
    const text = input.submit();
    if (!text) return;
    scroll = 0;
    demo.submit(text);
  };

  const beginRename = (pane: boolean): void => {
    originalName = pane ? demo.currentPane.name : demo.currentTab.name;
    mode = pane ? "rename-pane" : "rename-tab";
    input.context(context());
    input.clear();
    if (!pane) demo.renameTab("");
    render();
  };

  const finishRename = (restore = false): void => {
    const text = restore ? originalName : input.current.text;
    if (mode === "rename-pane") demo.renamePane(text);
    else demo.renameTab(text);
    mode = "locked";
    render();
  };

  const lockAfter = (operation: () => void): void => {
    mode = "locked";
    scroll = 0;
    operation();
    render();
  };

  const focus = (direction: FocusDirection): void => { lockAfter(() => { demo.moveFocus(direction); }); };
  const selectNumber = (number: number): void => {
    const tab = demo.orderedTabs[number - 1];
    if (number <= demo.orderedTabs.length) lockAfter(() => { demo.selectTab(tab.id); });
  };

  const modeKey = (key: string): void => {
    const directions: Partial<Record<string, FocusDirection>> = { h: "left", j: "down", k: "up", l: "right" };
    const direction = directions[key];
    if (direction && (mode === "pane" || mode === "move" || mode === "tab")) { focus(direction); return; }
    switch (mode) {
      case "normal":
        if (/^[1-9]$/u.test(key)) selectNumber(Number(key));
        else if (key === "d") lockAfter(() => { demo.detach(); });
        break;
      case "pane": paneKey(key); break;
      case "tab": tabKey(key); break;
      case "session": if (key === "d") lockAfter(() => { demo.detach(); }); break;
      case "locked": case "move": case "rename-pane": case "rename-tab": case "resize": case "scroll": break;
    }
  };

  const paneKey = (key: string): void => {
    switch (key) {
      case "n": case "r": lockAfter(() => { demo.splitPane("vertical"); }); break;
      case "d": lockAfter(() => { demo.splitPane("horizontal"); }); break;
      case "x": lockAfter(() => { demo.closePane(); }); break;
      case "f": lockAfter(() => { demo.toggleFullscreen(); }); break;
      case "c": beginRename(true); break;
      default: break;
    }
  };

  const tabKey = (key: string): void => {
    if (/^[1-9]$/u.test(key)) { selectNumber(Number(key)); return; }
    switch (key) {
      case "n": lockAfter(() => { demo.newTab(); }); break;
      case "x": lockAfter(() => { demo.closeTab(); }); break;
      case "r": beginRename(false); break;
      default: break;
    }
  };

  const altKey = (event: KeyboardEvent): boolean => {
    if (mode !== "locked" && mode !== "normal") return false;
    if (event.shiftKey && event.code !== "KeyR") return false;
    const directions: Partial<Record<string, FocusDirection>> = { ArrowLeft: "left", KeyB: "left", KeyH: "left", ArrowRight: "right", KeyF: "right", KeyL: "right", ArrowUp: "up", KeyK: "up", ArrowDown: "down", KeyJ: "down" };
    const direction = directions[event.code];
    if (direction) { focus(direction); return true; }
    if (/^Digit[1-9]$/u.test(event.code)) { selectNumber(Number(event.code.slice(-1))); return true; }
    switch (event.code) {
      case "KeyN": lockAfter(() => { demo.newTab(); }); break;
      case "KeyW": lockAfter(() => { demo.closeTab(); }); break;
      case "KeyX": lockAfter(() => { demo.closePane(); }); break;
      case "KeyZ": lockAfter(() => { demo.toggleFullscreen(); }); break;
      case "KeyG": mode = "normal"; render(); break;
      case "KeyV": lockAfter(() => { demo.splitPane("vertical"); }); break;
      case "KeyS": lockAfter(() => { demo.splitPane("horizontal"); }); break;
      case "KeyR": beginRename(event.shiftKey); break;
      case "KeyD": lockAfter(() => { demo.detach(); }); break;
      default: return false;
    }
    return true;
  };

  const controlKey = (event: KeyboardEvent): boolean => {
    if (event.code === "KeyA" || event.code === "KeyG") { mode = mode === "locked" ? "normal" : "locked"; render(); return true; }
    if (mode === "locked" || mode.startsWith("rename-")) return false;
    const modes: Partial<Record<string, DemoMode>> = { KeyP: "pane", KeyT: "tab", KeyN: "resize", KeyH: "move", KeyS: "scroll", KeyO: "session" };
    const next = modes[event.code];
    if (next) { mode = mode === next ? "normal" : next; render(); return true; }
    if (event.code !== "KeyQ") return false;
    lockAfter(() => { while (demo.attached) demo.closeTab(); });
    return true;
  };

  term.attachCustomKeyEventHandler((event) => {
    if (event.type !== "keydown" || event.isComposing) return true;
    if (event.metaKey || (event.key === "Tab" && !event.altKey && !event.ctrlKey)) return false;
    if (event.ctrlKey && (event.shiftKey || (term.hasSelection() && event.code === "KeyC"))) return false;
    if (!demo.attached) return true;
    let handled = false;
    if (event.altKey && !event.ctrlKey) handled = altKey(event);
    else if (event.ctrlKey && !event.altKey) handled = controlKey(event);
    if (handled) event.preventDefault();
    return !handled;
  });

  const interrupt = (data: string): boolean => {
    if (mode.startsWith("rename-")) {
      if (data === "\r" || data === "\u{3}") { finishRename(); return true; }
      if (data === ESC) { finishRename(true); return true; }
      return false;
    }
    if (data === "\u{3}" || (data === ESC && demo.attached && demo.currentPane.busy)) {
      if (demo.attached && demo.currentPane.busy) demo.cancel();
      else { input.clear(); render(); }
      return true;
    }
    if (data === ESC && mode !== "locked") { mode = "locked"; render(); return true; }
    return false;
  };

  const modeInput = (data: string): void => {
    if (data === "\r") { mode = "locked"; render(); return; }
    const directions: Partial<Record<string, FocusDirection>> = { [`${ESC}[A`]: "up", [`${ESC}[B`]: "down", [`${ESC}[C`]: "right", [`${ESC}[D`]: "left" };
    const direction = directions[data];
    if (!direction) { modeKey(data); return; }
    switch (mode) {
      case "resize": ratios.set(demo.currentTab, Math.max(0.2, Math.min(0.8, (ratios.get(demo.currentTab) ?? 0.5) + (direction === "left" || direction === "up" ? -0.05 : 0.05)))); render(); break;
      case "scroll": scroll = Math.max(0, scroll + (direction === "up" ? 1 : -1)); render(); break;
      case "locked": case "move": case "normal": case "pane": case "rename-pane": case "rename-tab": case "session": case "tab": focus(direction); break;
    }
  };

  const editInput = (data: string): void => {
    switch (data) {
      case "\r": submit(); return;
      case "\u{7F}": case "\b": input.backspace(); break;
      case `${ESC}[D`: input.move(-1); break;
      case `${ESC}[C`: input.move(1); break;
      case `${ESC}[A`: input.history(-1); break;
      case `${ESC}[B`: input.history(1); break;
      case `${ESC}[H`: case `${ESC}OH`: case "\u{1}": input.home(); break;
      case `${ESC}[F`: case `${ESC}OF`: case "\u{5}": input.end(); break;
      case `${ESC}[3~`: case "\u{4}": input.delete(); break;
      case "\u{15}": input.clear(); break;
      case "\u{B}": input.deleteToEnd(); break;
      case "\u{17}": input.deleteWord(); break;
      default: if (!data.startsWith(ESC)) input.insert(data); break;
    }
    switch (mode) {
      case "rename-tab": demo.renameTab(input.current.text); break;
      case "rename-pane": demo.renamePane(input.current.text); break;
      case "locked": case "move": case "normal": case "pane": case "resize": case "scroll": case "session": case "tab": render(); break;
    }
  };

  term.onData((data) => {
    if (interrupt(data)) return;
    const renaming = mode.startsWith("rename-");
    if (demo.attached && demo.currentPane.busy && !renaming) return;
    if (mode !== "locked" && !renaming) modeInput(data);
    else editInput(data);
  });

  host.addEventListener("click", (event) => {
    if (term.hasSelection()) return;
    const rectangle = host.querySelector(".xterm-screen")?.getBoundingClientRect();
    if (!rectangle) return;
    const x = Math.floor((event.clientX - rectangle.left) * term.cols / rectangle.width);
    const y = Math.floor((event.clientY - rectangle.top) * term.rows / rectangle.height);
    const target = targets.find((candidate) => x >= candidate.x && x < candidate.x + candidate.width && y >= candidate.y && y < candidate.y + candidate.height);
    switch (target?.action) {
      case "tab": lockAfter(() => { demo.selectTab(target.id); }); break;
      case "new-tab": lockAfter(() => { demo.newTab(); }); break;
      case "pane": demo.selectPane(target.id); break;
      case undefined: break;
    }
    focusTerminal();
  }, { signal: lifetime.signal });
  action.addEventListener("click", () => { if (mode.startsWith("rename-")) finishRename(); else submit(); focusTerminal(); }, { signal: lifetime.signal });
  detach.addEventListener("click", () => { lockAfter(() => { demo.detach(); }); focusTerminal(); }, { signal: lifetime.signal });
  root.querySelector("[data-reset]")?.addEventListener("click", () => { mode = "locked"; scroll = 0; input.reset(); demo.reset(); focusTerminal(); }, { signal: lifetime.signal });
  tabs.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const id = event.target.closest<HTMLButtonElement>("button[data-tab]")?.dataset.tab;
    if (id) { lockAfter(() => { demo.selectTab(id); }); focusTerminal(); }
  }, { signal: lifetime.signal });
  tabs.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const ordered = demo.orderedTabs;
    const current = ordered.findIndex((tab) => tab.id === demo.active);
    let next: number;
    switch (event.key) {
      case "ArrowLeft": next = (current - 1 + ordered.length) % ordered.length; break;
      case "ArrowRight": next = (current + 1) % ordered.length; break;
      case "Home": next = 0; break;
      case "End": next = ordered.length - 1; break;
      default: return;
    }
    event.preventDefault();
    lockAfter(() => { demo.selectTab(ordered[next].id); });
    tabs.querySelector<HTMLButtonElement>("[aria-selected=true]")?.focus({ preventScroll: true });
  }, { signal: lifetime.signal });
  const resize = (): void => {
    if (!root.isConnected || host.clientWidth === 0 || host.clientHeight === 0) return;
    const proposed = fit.proposeDimensions();
    if (!proposed || proposed.cols < 20 || proposed.rows < 12) return;
    dimensions = proposed;
    render();
  };
  const observer = new ResizeObserver(resize);
  const removal = new MutationObserver(() => {
    if (root.isConnected) return;
    removal.disconnect();
    observer.disconnect();
    lifetime.abort();
    demo.dispose();
    term.dispose();
  });
  observer.observe(host);
  removal.observe(document.documentElement, { childList: true, subtree: true });
  resize();
}
