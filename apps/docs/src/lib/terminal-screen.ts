import type { Line, Pane, TerminalDemo } from "./terminal-demo";
import { printable, type TerminalInput } from "./terminal-input.ts";

export type DemoMode = "locked" | "move" | "normal" | "pane" | "rename-pane" | "rename-tab" | "resize" | "scroll" | "session" | "tab";

interface Cell {
  background: string;
  bold: boolean;
  color: string;
  text: string;
}

export interface HitTarget {
  action: "new-tab" | "pane" | "tab";
  height: number;
  id: string;
  width: number;
  x: number;
  y: number;
}

export interface ScreenState {
  columns: number;
  demo: TerminalDemo;
  input: TerminalInput;
  mode: DemoMode;
  ratio: number;
  rows: number;
  scroll: number;
}

export interface TerminalFrame {
  ansi: string;
  targets: HitTarget[];
}

const COLORS = { background: "#1e1e2e", bar: "#181825", text: "#cdd6f4", muted: "#7f849c", green: "#a6e3a1", cyan: "#94e2d5", blue: "#89b4fa", yellow: "#f9e2af", pink: "#f38ba8" } as const;
const SEGMENTS = new Intl.Segmenter(undefined, { granularity: "grapheme" });
const ESC = "\u{1B}";

function segments(text: string): string[] {
  return [...SEGMENTS.segment(printable(text))].map(({ segment }) => segment);
}

function characterWidth(text: string): number {
  if (/^\p{Mark}+$/u.test(text)) return 0;
  if (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Extended_Pictographic}\p{Regional_Indicator}]/u.test(text)) return 2;
  const code = text.codePointAt(0) ?? 0;
  return code >= 0xFF01 && code <= 0xFF60 ? 2 : 1;
}

function width(text: string): number {
  return segments(text).reduce((total, character) => total + characterWidth(character), 0);
}

function wrap(text: string, columns: number): string[] {
  const result: string[] = [];
  let line: string[] = [];
  let used = 0;
  for (const character of segments(text)) {
    const size = characterWidth(character);
    if (used + size > columns && line.length > 0) {
      result.push(line.join(""));
      line = [];
      used = 0;
    }
    line.push(character);
    used += size;
  }
  result.push(line.join(""));
  return result;
}

function colorCode(color: string, background = false): string {
  const value = Number.parseInt(color.slice(1), 16);
  return `${background ? "48" : "38"};2;${String(value >> 16)};${String((value >> 8) & 255)};${String(value & 255)}`;
}

class Screen {
  readonly cells: Cell[][];
  readonly columns: number;
  readonly rows: number;
  readonly targets: HitTarget[] = [];
  cursor = { x: 0, y: 0 };
  showCursor = true;

  constructor(columns: number, rows: number) {
    this.columns = columns;
    this.rows = rows;
    this.cells = Array.from({ length: rows }, () => Array.from({ length: columns }, () => ({ text: " ", color: COLORS.text, background: COLORS.background, bold: false })));
  }

  text(x: number, y: number, text: string, color: string = COLORS.text, background: string = COLORS.background, bold = false, limit = this.columns - x): number {
    if (y < 0 || y >= this.rows) return x;
    const row = this.cells[y];
    const end = Math.min(this.columns, x + limit);
    let column = x;
    for (const character of segments(text)) {
      const size = characterWidth(character);
      if (column < 0 || column + size > end) break;
      row[column] = { text: character, color, background, bold };
      if (size === 2) row[column + 1] = { text: "", color, background, bold };
      column += size;
    }
    return column;
  }

  bar(y: number): void {
    this.text(0, y, " ".repeat(this.columns), COLORS.text, COLORS.bar);
  }

  ribbon(x: number, y: number, text: string, active: boolean): number {
    const background = active ? COLORS.green : COLORS.text;
    const start = this.text(x, y, "", COLORS.bar, background, true);
    const end = this.text(start, y, ` ${text} `, COLORS.bar, background, true);
    return this.text(end, y, "", background, COLORS.bar, true);
  }

  frame(): TerminalFrame {
    const output = [`${ESC}[?25l${ESC}[?6l${ESC}[r${ESC}[?7l${ESC}[2J${ESC}[H`];
    let style = "";
    for (const [y, row] of this.cells.entries()) {
      output.push(`${ESC}[${String(y + 1)};1H`);
      for (const cell of row) {
        if (!cell.text) continue;
        const next = `${cell.bold ? "1" : "22"};${colorCode(cell.color)};${colorCode(cell.background, true)}`;
        if (style !== next) {
          output.push(`${ESC}[${next}m`);
          style = next;
        }
        output.push(cell.text);
      }
    }
    output.push(`${ESC}[0m${ESC}[${String(this.cursor.y + 1)};${String(this.cursor.x + 1)}H${ESC}[?25${this.showCursor ? "h" : "l"}`);
    return { ansi: output.join(""), targets: this.targets };
  }
}

export function terminalScreen(state: ScreenState): TerminalFrame {
  const screen = new Screen(state.columns, state.rows);
  if (!state.demo.attached) {
    const lines = transcript(state.demo.localLines, screen.columns);
    const visible = lines.slice(-Math.max(0, screen.rows - 4));
    for (const [index, line] of visible.entries()) screen.text(0, index, line.text, line.color);
    screen.text(0, visible.length + 1, "your-mac ~", COLORS.cyan, COLORS.background, true);
    drawInput(screen, state, 0, visible.length + 2, screen.columns, "❯ ");
    return screen.frame();
  }
  tabBar(screen, state);
  for (const area of paneAreas(screen, state)) drawPane(screen, state, area);
  keyBar(screen, state.mode);
  if (state.mode === "rename-tab" || state.mode === "rename-pane") {
    const label = state.mode === "rename-tab" ? "Rename tab: " : "Rename pane: ";
    screen.bar(screen.rows - 1);
    drawInput(screen, state, 1, screen.rows - 1, screen.columns - 1, label);
  }
  return screen.frame();
}

function tabBar(screen: Screen, state: ScreenState): void {
  screen.bar(0);
  let x = screen.columns >= 70 ? screen.text(1, 0, "Zellij (sarj-home) ", COLORS.text, COLORS.bar, true) : 0;
  const tabs = state.demo.orderedTabs;
  const badgeWidth = state.demo.currentTab.panes.length > 1 && !state.demo.currentTab.fullscreen && screen.columns >= 50 ? 8 : 0;
  const available = Math.max(5, screen.columns - x - 9 - badgeWidth);
  const labels = tabs.map((tab) => shortened(tab.name, Math.max(1, available - 4)));
  let first = tabs.findIndex((tab) => tab.id === state.demo.active);
  let last = first + 1;
  let used = width(labels[first]) + 4;
  while (first > 0 && used + width(labels[first - 1]) + 4 <= available) {
    first -= 1;
    used += width(labels[first]) + 4;
  }
  while (last < tabs.length && used + width(labels[last]) + 4 <= available) {
    used += width(labels[last]) + 4;
    last += 1;
  }
  if (first > 0) {
    screen.targets.push({ action: "tab", id: tabs[first - 1].id, x, y: 0, width: 2, height: 1 });
    x = screen.text(x, 0, "‹ ", COLORS.text, COLORS.bar, true);
  }
  for (let index = first; index < last; index += 1) {
    const tab = tabs[index];
    const start = x;
    x = screen.ribbon(x, 0, labels[index], tab.id === state.demo.active);
    screen.targets.push({ action: "tab", id: tab.id, x: start, y: 0, width: x - start, height: 1 });
  }
  if (last < tabs.length) {
    screen.targets.push({ action: "tab", id: tabs[last].id, x, y: 0, width: 2, height: 1 });
    x = screen.text(x, 0, " ›", COLORS.text, COLORS.bar, true);
  }
  if (x < screen.columns - 3) {
    const end = screen.ribbon(x, 0, "+", false);
    screen.targets.push({ action: "new-tab", id: "", x, y: 0, width: end - x, height: 1 });
  }
  if (badgeWidth) screen.ribbon(screen.columns - badgeWidth, 0, "BASE", false);
}

function shortened(text: string, columns: number): string {
  if (width(text) <= columns) return text;
  const characters = segments(text);
  while (width(characters.join("")) > columns - 1) characters.pop();
  return `${characters.join("")}…`;
}

function keyBar(screen: Screen, mode: DemoMode): void {
  const y = screen.rows - 1;
  screen.bar(y);
  let x = screen.text(1, y, "Ctrl + ", COLORS.text, COLORS.bar, true);
  if (mode === "locked") {
    screen.ribbon(x, y, "<g> UNLOCK", false);
    return;
  }
  if (mode === "rename-tab" || mode === "rename-pane") {
    screen.text(x, y, "Enter to finish · Esc to cancel", COLORS.yellow, COLORS.bar);
    return;
  }
  for (const [key, target] of [["g", "locked"], ["p", "pane"], ["t", "tab"], ["n", "resize"], ["h", "move"], ["s", "scroll"], ["o", "session"]] as const) {
    x = screen.ribbon(x, y, key, mode === target || (mode === "normal" && target === "locked"));
    if (x > screen.columns - 8) break;
  }
  if (x <= screen.columns - 8) screen.ribbon(x, y, "q", false);
  if (screen.columns > 95) screen.text(screen.columns - 7, y, "Alt +", "#fab387", COLORS.bar, true);
}

interface PaneArea { height: number; pane: Pane; width: number; x: number; y: number }

function paneAreas(screen: Screen, state: ScreenState): PaneArea[] {
  const tab = state.demo.currentTab;
  if (tab.panes.length === 1 || tab.fullscreen) return [{ pane: state.demo.currentPane, x: 0, y: 1, width: screen.columns, height: screen.rows - 2 }];
  const first = tab.panes[0];
  const second = tab.panes[1];
  if (tab.split === "vertical") {
    const middle = Math.floor(screen.columns * state.ratio);
    for (let y = 1; y < screen.rows - 1; y += 1) screen.text(middle, y, "│", COLORS.green);
    return [{ pane: first, x: 0, y: 1, width: middle, height: screen.rows - 2 }, { pane: second, x: middle + 1, y: 1, width: screen.columns - middle - 1, height: screen.rows - 2 }];
  }
  const middle = Math.floor((screen.rows - 2) * state.ratio) + 1;
  screen.text(0, middle, "─".repeat(screen.columns), COLORS.green);
  return [{ pane: first, x: 0, y: 1, width: screen.columns, height: middle - 1 }, { pane: second, x: 0, y: middle + 1, width: screen.columns, height: screen.rows - middle - 2 }];
}

function transcript(lines: Line[], columns: number): { color: string; text: string }[] {
  return lines.flatMap((line) => [
    ...line.text.split("\n").flatMap((text) => wrap(text, columns).map((wrapped) => ({ text: wrapped, color: COLORS.text }))),
    { text: "", color: COLORS.text },
  ]);
}

function drawInput(screen: Screen, state: ScreenState, x: number, y: number, columns: number, prompt: string): void {
  screen.text(x, y, prompt, state.demo.attached && state.demo.currentPane.mode === "codex" ? "#fab387" : COLORS.green);
  const inputX = x + width(prompt);
  const available = Math.max(1, columns - width(prompt));
  const before = segments(state.input.beforeCursor);
  while (width(before.join("")) >= available && before.length > 0) before.shift();
  const shown = before.join("");
  screen.text(inputX, y, `${shown}${state.input.afterCursor}`, COLORS.text, COLORS.background, false, available);
  if (!state.input.current.text && state.demo.attached && state.demo.currentPane.mode === "codex" && !state.mode.startsWith("rename-")) screen.text(inputX, y, "Ask Codex to do anything", COLORS.muted, COLORS.background, false, available);
  screen.cursor = { x: Math.min(screen.columns - 1, inputX + width(shown)), y };
}

function drawPane(screen: Screen, state: ScreenState, area: PaneArea): void {
  const { pane } = area;
  const active = pane.id === state.demo.currentPane.id;
  const framed = state.demo.currentTab.panes.length > 1 && !state.demo.currentTab.fullscreen;
  screen.targets.push({ action: "pane", id: pane.id, x: area.x, y: area.y, width: area.width, height: area.height });
  if (framed) {
    const title = pane.name || `Pane #${String(state.demo.currentTab.panes.indexOf(pane) + 1)}`;
    screen.text(area.x + Math.max(0, Math.floor((area.width - width(title)) / 2)), area.y, title, active ? COLORS.green : COLORS.text, COLORS.background, true, area.width);
  }
  const top = area.y + (framed ? 2 : 1);
  const bottom = area.y + area.height - 1;
  if (pane.mode === "codex") drawCodex(screen, state, area, top, bottom, active);
  else drawShell(screen, state, area, top, bottom, active, framed);
}

function drawCodex(screen: Screen, state: ScreenState, area: PaneArea, top: number, bottom: number, active: boolean): void {
  const { pane } = area;
  const left = area.x + 2;
  const columns = Math.max(1, area.width - 4);
  screen.text(left, top, ">_", COLORS.blue);
  screen.text(left + 3, top, "OpenAI Codex", COLORS.text, COLORS.background, true, columns - 3);
  screen.text(left + 16, top, "(v0.164.0)", COLORS.muted, COLORS.background, false, columns - 16);
  screen.text(left + 3, top + 1, state.demo.currentTab.path, COLORS.muted, COLORS.background, false, columns - 3);
  const inputY = Math.max(top + 3, bottom - 3);
  const available = Math.max(0, inputY - top - 4);
  const content = transcript(pane.lines, columns);
  const end = Math.max(0, content.length - (active ? state.scroll : 0));
  const visible = content.slice(Math.max(0, end - available), end);
  for (const [index, line] of visible.entries()) screen.text(left, top + 3 + index, line.text, line.color, COLORS.background, false, columns);
  if (active) {
    if (pane.busy) {
      screen.text(area.x, inputY, "• Working (esc to interrupt)", COLORS.muted, COLORS.background, false, area.width);
      screen.showCursor = false;
    } else drawInput(screen, state, area.x, inputY, area.width, "› ");
  } else screen.text(area.x, inputY, "› Ask Codex to do anything", COLORS.muted, COLORS.background, false, area.width);
  const modelEnd = screen.text(left, bottom - 1, "GPT-6.1-Sol max", COLORS.yellow, COLORS.background, true, columns);
  const pathStart = screen.text(modelEnd, bottom - 1, " · ", COLORS.text, COLORS.background, false, Math.max(0, left + columns - modelEnd));
  screen.text(pathStart, bottom - 1, state.demo.currentTab.path, COLORS.green, COLORS.background, true, Math.max(0, left + columns - pathStart));
  screen.text(left, bottom, "? for shortcuts", COLORS.text, COLORS.background, false, columns);
}

function drawShell(screen: Screen, state: ScreenState, area: PaneArea, top: number, bottom: number, active: boolean, framed: boolean): void {
  const content = transcript(area.pane.lines, Math.max(1, area.width));
  const available = Math.max(0, area.height - (framed ? 5 : 3));
  const end = Math.max(0, content.length - (active ? state.scroll : 0));
  const visible = content.slice(Math.max(0, end - available), end);
  for (const [index, line] of visible.entries()) screen.text(area.x, top + index, line.text, line.color, COLORS.background, false, area.width);
  const promptY = Math.min(bottom - 1, top + visible.length);
  screen.text(area.x, promptY, `sarj-vm ${state.demo.currentTab.path}`, COLORS.cyan, COLORS.background, true, area.width);
  if (active) drawInput(screen, state, area.x, promptY + 1, area.width, "❯ ");
  else screen.text(area.x, promptY + 1, "❯", COLORS.green);
}
