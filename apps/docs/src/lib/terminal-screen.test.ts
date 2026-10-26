import assert from "node:assert/strict";
import { test } from "node:test";

import { TerminalDemo } from "./terminal-demo.ts";
import { TerminalInput } from "./terminal-input.ts";
import { terminalScreen } from "./terminal-screen.ts";

function sample() {
  const demo = new TerminalDemo(() => { /* This renderer test does not mount a browser. */ });
  demo.submit("sarj-connect terminal");
  const input = new TerminalInput();
  input.context(demo.currentPane.id, "");
  return { demo, input, mode: "locked" as const, columns: 110, rows: 32, ratio: 0.5, scroll: 0 };
}

await test("the selected tab remains visible when nine tabs overflow a narrow terminal", () => {
  const state = sample();
  for (let index = 0; index < 6; index += 1) state.demo.newTab();
  for (const columns of [20, 28, 40, 110]) {
    for (const tab of state.demo.orderedTabs) {
      state.demo.selectTab(tab.id);
      const frame = terminalScreen({ ...state, columns });
      const target = frame.targets.find((candidate) => candidate.action === "tab" && candidate.id === tab.id);
      assert.ok(target, `${tab.name} must remain visible at ${String(columns)} columns`);
      assert.ok(target.x >= 0);
      assert.ok(target.width > 0);
      assert.ok(target.x + target.width <= columns);
    }
  }
  state.demo.dispose();
});

await test("split frame hit targets occupy the two real pane regions and fullscreen removes the split", () => {
  const state = sample();
  state.demo.splitPane("vertical");
  const split = terminalScreen({ ...state, ratio: 0.3 }).targets.filter((target) => target.action === "pane");
  assert.equal(split.length, 2);
  assert.equal(split[0]?.width, 33);
  assert.equal(split[1]?.x, 34);
  state.demo.toggleFullscreen();
  const fullscreen = terminalScreen(state).targets.filter((target) => target.action === "pane");
  assert.equal(fullscreen.length, 1);
  assert.equal(fullscreen[0]?.width, state.columns);
  state.demo.dispose();
});

await test("a paragraph wrapping in a narrow terminal continues on the next row without an inserted gap", () => {
  const state = sample();
  state.demo.currentPane.lines = [{ kind: "output", text: "ABCDEFGHIJKLMNOPQRSTUVWXyz" }];
  const frame = terminalScreen({ ...state, columns: 28, rows: 18 });
  const rows = ["ABCDEFGHIJKLMNOPQRSTUVWX", "yz"].map((fragment) => {
    const index = frame.ansi.indexOf(fragment);
    assert.notEqual(index, -1);
    const addresses = frame.ansi.slice(0, index).split("\u{1B}[").flatMap((command) => {
      const match = /^(\d+);1H/u.exec(command);
      return match ? Number(match[1]) : [];
    });
    return addresses.at(-1);
  });
  assert.equal(rows[1], (rows[0] ?? 0) + 1);
  state.demo.dispose();
});

await test("terminal frames never interpret user escape sequences and keep every cursor address inside the viewport", () => {
  const state = sample();
  state.demo.renameTab("\u{1B}]52;c;not-a-clipboard\u{7}");
  state.input.replace("界👩‍💻".repeat(60));
  const frame = terminalScreen({ ...state, columns: 28, rows: 12 });
  assert.ok(!frame.ansi.includes("\u{1B}]"));
  assert.ok(!frame.ansi.includes("\u{7}"));
  for (const command of frame.ansi.split("\u{1B}[")) {
    const match = /^(\d+);(\d+)H/u.exec(command);
    if (!match) continue;
    assert.ok(Number(match[1]) >= 1 && Number(match[1]) <= 12);
    assert.ok(Number(match[2]) >= 1 && Number(match[2]) <= 28);
  }
  state.demo.dispose();
});
