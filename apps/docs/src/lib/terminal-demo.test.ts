import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";

import { TerminalDemo } from "./terminal-demo.ts";

function fixture(context: TestContext): TerminalDemo {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const demo = new TerminalDemo(() => { /* No UI renderer in model tests. */ });
  context.after(() => { demo.dispose(); });
  return demo;
}

function connect(demo: TerminalDemo): void {
  demo.submit("sarj-connect terminal");
}

await test("initial session has three distinct sample tabs and two ready Codex conversations", (context) => {
  const demo = fixture(context);
  assert.equal(demo.attached, false);
  assert.deepEqual(demo.orderedTabs.map((tab) => tab.name), ["docs", "tests", "shell"]);
  assert.deepEqual(demo.orderedTabs.map((tab) => tab.panes[0]?.mode), ["codex", "codex", "shell"]);
  assert.equal(demo.currentPane.lines.length, 2);
  assert.equal(demo.suggestedInput, "sarj-connect terminal");
});

await test("all connection aliases attach to the same existing session", (context) => {
  const demo = fixture(context);
  const baseline = structuredClone(demo.orderedTabs);
  for (const command of ["sarj-connect terminal", "vm", "vm terminal", "sarj-vm", "sarj-vm terminal"]) {
    demo.submit(command);
    assert.equal(demo.attached, true);
    assert.equal(demo.suggestedInput, "");
    assert.deepEqual(demo.orderedTabs, baseline);
    demo.detach();
  }
});

await test("sample conversations never start background tasks automatically", (context) => {
  const demo = fixture(context);
  let callbacks = 0;
  const other = new TerminalDemo(() => { callbacks += 1; });
  context.after(() => { other.dispose(); });
  connect(demo);
  const before = structuredClone(demo.orderedTabs);
  context.mock.timers.runAll();
  assert.deepEqual(demo.orderedTabs, before);
  assert.equal(callbacks, 0);
  assert.equal(demo.currentPane.busy, false);
});

await test("plain shell starts Codex explicitly and slash exit returns to that shell", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.selectTab("shell");
  assert.equal(demo.suggestedInput, "");
  demo.submit("codex");
  assert.equal(demo.currentPane.mode, "codex");
  assert.equal(demo.suggestedInput, "");
  assert.equal(demo.currentPane.name, "Pane #1");
  assert.equal(demo.prompt, "›");
  demo.submit("/exit");
  assert.equal(demo.currentPane.mode, "shell");
  assert.equal(demo.currentPane.name, "Pane #1");
  assert.equal(demo.prompt, "❯");
});

await test("command chaining and local Codex remain inert unknown commands", (context) => {
  const demo = fixture(context);
  for (const command of ["codex", "vm; echo hacked", "sarj-connect terminal && codex", "$(vm)", "sarj-connect info"]) {
    demo.submit(command);
    assert.equal(demo.attached, false);
    assert.match(demo.localLines.at(-1)?.text ?? "", /command not found/u);
  }
  demo.submit("sarj-connect browser https://example.com");
  assert.equal(demo.attached, false);
  assert.match(demo.localLines.at(-1)?.text ?? "", /sample session/u);
});

await test("empty input is ignored and the exact 300 character limit is enforced", (context) => {
  const demo = fixture(context);
  demo.submit(" \t\n ");
  assert.equal(demo.localLines.length, 0);
  demo.submit("x".repeat(300));
  assert.equal(demo.localLines.at(-2)?.text.length, 302);
  const before = demo.localLines.length;
  demo.submit("x".repeat(301));
  assert.equal(demo.localLines.length, before + 1);
  assert.match(demo.localLines.at(-1)?.text ?? "", /300 characters/u);
  assert.equal(demo.localLines.some((line) => line.text.includes("x".repeat(301))), false);
});

await test("submitted terminal control sequences cannot survive in echoed user text", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.submit("say \u{1B}]52;c;clipboard\u{7} \u{9B}2J hello\nworld");
  const command = demo.currentPane.lines.at(-1)?.text ?? "";
  assert.equal(/\p{Cc}/u.test(command), false);
  assert.match(command, /hello world/u);
  context.mock.timers.runAll();
  assert.equal(demo.currentPane.busy, false);
});

await test("HTML and shell syntax stay literal text in a fake Codex prompt", (context) => {
  const demo = fixture(context);
  connect(demo);
  const prompt = "<img src=x onerror=alert(1)> $(curl example.com)";
  demo.submit(prompt);
  assert.equal(demo.currentPane.lines.at(-1)?.text, `› ${prompt}`);
  context.mock.timers.runAll();
  assert.match(demo.currentPane.lines.at(-1)?.text ?? "", /demo README/u);
});

await test("local and pane transcripts stay bounded after repeated commands", (context) => {
  const demo = fixture(context);
  for (let index = 0; index < 50; index += 1) demo.submit(`unknown ${String(index)}`);
  assert.equal(demo.localLines.length, 32);
  connect(demo);
  demo.selectTab("shell");
  for (let index = 0; index < 50; index += 1) demo.submit("pwd");
  assert.equal(demo.currentPane.lines.length, 32);
  assert.equal(demo.currentPane.lines[0]?.text, "❯ pwd");
});

await test("background completion keeps selection and the other transcript unchanged", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.submit("Simplify the README");
  const docs = demo.currentPane;
  demo.selectTab("tests");
  const before = structuredClone(demo.currentPane);
  context.mock.timers.tick(2199);
  assert.equal(docs.busy, true);
  context.mock.timers.tick(1);
  assert.equal(docs.busy, false);
  assert.equal(demo.active, "tests");
  assert.deepEqual(demo.currentPane, before);
});

await test("detach and reconnect preserve active tab and pending task deadlines", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.selectTab("tests");
  demo.submit("Run the tests");
  context.mock.timers.tick(400);
  const before = structuredClone(demo.orderedTabs);
  demo.detach();
  demo.submit("  sarj-connect   terminal  ");
  assert.equal(demo.active, "tests");
  assert.deepEqual(demo.orderedTabs, before);
  context.mock.timers.tick(1799);
  assert.equal(demo.currentPane.busy, true);
  context.mock.timers.tick(1);
  assert.equal(demo.currentPane.busy, false);
  demo.detach();
  assert.equal(demo.attached, false);
});

await test("cancel stops only the active pane while another pane keeps working", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.submit("Simplify the README");
  const original = demo.currentPane;
  demo.splitPane("vertical");
  demo.submit("codex");
  demo.submit("Run the tests");
  demo.cancel();
  const cancelled = demo.currentPane;
  context.mock.timers.runAll();
  assert.match(cancelled.lines.at(-1)?.text ?? "", /interrupted/u);
  assert.match(original.lines.at(-1)?.text ?? "", /Updated/u);
  assert.equal(cancelled.mode, "codex");
});

await test("reset clears every pending pane task and restores the sample session", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.submit("Simplify the README");
  demo.selectTab("tests");
  demo.submit("Run the tests");
  demo.splitPane("horizontal");
  demo.renameTab("Changed");
  demo.reset();
  const before = structuredClone(demo.orderedTabs);
  context.mock.timers.runAll();
  assert.deepEqual(demo.orderedTabs, before);
  assert.deepEqual(demo.orderedTabs.map((tab) => tab.name), ["docs", "tests", "shell"]);
  assert.equal(demo.attached, false);
  assert.equal(demo.connectedBefore, false);
  assert.equal(demo.currentTab.panes.length, 1);
});

await test("dispose clears timers and makes later actions inert", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.submit("Simplify the README");
  demo.dispose();
  const before = structuredClone(demo.orderedTabs);
  context.mock.timers.runAll();
  demo.newTab(); demo.closeTab(); demo.reset(); demo.detach(); demo.splitPane("vertical"); demo.renameTab("Changed");
  assert.deepEqual(demo.orderedTabs, before);
  assert.equal(demo.currentPane.busy, false);
});

await test("slash help and clear are scoped to Codex and unknown slashes start no task", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.submit("/help");
  assert.match(demo.currentPane.lines.at(-1)?.text ?? "", /Ctrl \+ C/u);
  demo.submit("/unknown");
  assert.equal(demo.currentPane.busy, false);
  demo.submit("/clear");
  assert.deepEqual(demo.currentPane.lines, []);
  assert.equal(demo.currentPane.mode, "codex");
  demo.selectTab("shell");
  demo.submit("/exit");
  assert.equal(demo.currentPane.mode, "shell");
  assert.equal(demo.attached, true);
});

await test("shell commands use tab cwd and clear and exit match the focused pane", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.selectTab("shell");
  demo.submit("pwd");
  assert.equal(demo.currentPane.lines.at(-1)?.text, "~/demo/shell");
  demo.submit("ls");
  assert.match(demo.currentPane.lines.at(-1)?.text ?? "", /README\.md/u);
  demo.submit("clear");
  assert.deepEqual(demo.currentPane.lines, []);
  demo.submit("exit");
  assert.equal(demo.orderedTabs.length, 2);
  assert.equal(demo.active, "docs");
});

await test("new tabs allocate above current IDs and safely reuse a closed latest tab ID", (context) => {
  const demo = fixture(context);
  connect(demo);
  for (let index = 0; index < 8; index += 1) demo.newTab();
  assert.equal(demo.orderedTabs.length, 9);
  assert.equal(new Set(demo.orderedTabs.map((tab) => tab.id)).size, 9);
  assert.deepEqual(demo.orderedTabs.slice(0, 3).map((tab) => tab.id), ["docs", "tests", "shell"]);
  const old = demo.active;
  const oldPane = demo.currentPane.id;
  demo.closeTab();
  demo.newTab();
  assert.equal(demo.active, old);
  assert.notEqual(demo.currentPane.id, oldPane);
  assert.equal(demo.currentTab.name, "Tab #9");
  assert.equal(demo.currentPane.mode, "shell");
});

await test("closing a tab returns to the previous visited tab or first remaining tab", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.selectTab("shell");
  demo.selectTab("tests");
  demo.submit("Run the tests");
  const closed = demo.currentPane;
  demo.closeTab();
  const before = structuredClone(closed.lines);
  context.mock.timers.runAll();
  assert.deepEqual(closed.lines, before);
  assert.equal(closed.busy, false);
  assert.equal(demo.active, "shell");
  assert.equal(demo.tabs.tests, undefined);
  demo.reset();
  connect(demo);
  demo.closeTab();
  assert.equal(demo.active, "tests");
  demo.reset();
  connect(demo);
  demo.selectTab("shell");
  demo.selectTab("tests");
  demo.detach();
  connect(demo);
  demo.closeTab();
  assert.equal(demo.active, "docs");
});

await test("closing the final tab ends the session and reconnect creates a fresh empty shell", (context) => {
  const demo = fixture(context);
  connect(demo);
  for (const id of ["docs", "tests", "shell"]) { demo.selectTab(id); demo.closeTab(); }
  assert.equal(demo.attached, false);
  assert.equal(demo.orderedTabs.length, 1);
  assert.equal(demo.currentPane.mode, "shell");
  assert.deepEqual(demo.currentPane.lines, []);
  connect(demo);
  assert.equal(demo.attached, true);
  assert.deepEqual(demo.currentPane.lines, []);
  assert.match(demo.status, /new session/u);
});

await test("split creates a focused empty shell, records its axis and bounds pane count", (context) => {
  const demo = fixture(context);
  connect(demo);
  const original = demo.currentPane;
  demo.splitPane("horizontal");
  assert.equal(demo.currentTab.split, "horizontal");
  assert.equal(demo.currentTab.panes.length, 2);
  assert.notEqual(demo.currentPane.id, original.id);
  assert.equal(demo.currentPane.mode, "shell");
  assert.deepEqual(demo.currentTab.panes.map((pane) => pane.name), ["Pane #1", "Pane #2"]);
  demo.splitPane("vertical");
  assert.equal(demo.currentTab.panes.length, 2);
  assert.equal(demo.currentTab.split, "horizontal");
});

await test("left and right focus panes first then move between adjacent tabs with wrap", (context) => {
  const demo = fixture(context);
  connect(demo);
  const first = demo.currentPane.id;
  demo.splitPane("vertical");
  demo.moveFocus("left");
  assert.equal(demo.currentPane.id, first);
  demo.moveFocus("left");
  assert.equal(demo.active, "shell");
  demo.moveFocus("right");
  assert.equal(demo.active, "docs");
  demo.moveFocus("right");
  assert.notEqual(demo.currentPane.id, first);
  demo.moveFocus("right");
  assert.equal(demo.active, "tests");
  demo.splitPane("vertical");
  const testsLeft = demo.currentTab.panes.at(0)?.id;
  demo.selectTab("docs");
  const docsRight = demo.currentPane.id;
  demo.moveFocus("right");
  assert.equal(demo.currentPane.id, testsLeft);
  demo.moveFocus("left");
  assert.equal(demo.active, "docs");
  assert.equal(demo.currentPane.id, docsRight);
});

await test("up and down focus only along horizontal splits and never select another tab", (context) => {
  const demo = fixture(context);
  connect(demo);
  const first = demo.currentPane.id;
  demo.moveFocus("up");
  assert.equal(demo.active, "docs");
  demo.splitPane("horizontal");
  const second = demo.currentPane.id;
  demo.moveFocus("up");
  assert.equal(demo.currentPane.id, first);
  demo.moveFocus("up");
  assert.equal(demo.currentPane.id, first);
  demo.moveFocus("down");
  assert.equal(demo.currentPane.id, second);
  assert.equal(demo.active, "docs");
});

await test("closing one pane cancels only its task and preserves the other pane conversation", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.submit("Simplify the README");
  const first = demo.currentPane;
  demo.splitPane("vertical");
  demo.submit("codex");
  demo.submit("Run the tests");
  const second = demo.currentPane;
  demo.selectPane(first.id);
  demo.closePane();
  const closedLines = structuredClone(first.lines);
  context.mock.timers.runAll();
  assert.deepEqual(first.lines, closedLines);
  assert.match(second.lines.at(-1)?.text ?? "", /12 passed/u);
  assert.equal(demo.currentPane.id, second.id);
  assert.equal(demo.currentTab.panes.length, 1);
});

await test("fullscreen is tab-local and survives detach and reconnect", (context) => {
  const demo = fixture(context);
  connect(demo);
  demo.splitPane("vertical");
  demo.toggleFullscreen();
  demo.selectTab("tests");
  assert.equal(demo.currentTab.fullscreen, false);
  demo.selectTab("docs");
  assert.equal(demo.currentTab.fullscreen, true);
  const activePane = demo.currentPane.id;
  demo.detach();
  connect(demo);
  assert.equal(demo.currentPane.id, activePane);
  assert.equal(demo.currentTab.fullscreen, true);
  demo.closePane();
  assert.equal(demo.currentTab.fullscreen, false);
});

await test("renames are printable and bounded while invalid or detached selection is inert", (context) => {
  const demo = fixture(context);
  demo.selectTab("shell");
  assert.equal(demo.active, "docs");
  connect(demo);
  demo.renameTab(`\u{1B}]title\u{7} ${  "🦊".repeat(40)}`);
  demo.renamePane("\u{1B}[2J Agent\u{7}");
  assert.equal(/\p{Cc}/u.test(demo.currentTab.name + demo.currentPane.name), false);
  assert.equal([...new Intl.Segmenter().segment(demo.currentTab.name)].length, 30);
  demo.selectTab("missing");
  demo.selectPane("missing");
  assert.equal(demo.active, "docs");
  demo.submit("/exit");
  assert.equal(demo.currentPane.name, "[2J Agent");
  demo.detach();
  const before = structuredClone(demo.orderedTabs);
  demo.newTab(); demo.closePane(); demo.renamePane("ignored"); demo.renameTab("ignored"); demo.toggleFullscreen(); demo.moveFocus("right");
  assert.deepEqual(demo.orderedTabs, before);
});
