import assert from "node:assert/strict";
import { test } from "node:test";

import { TerminalDemo } from "./terminal-demo.ts";

function ignoreChange(): void {
  // Model tests do not render a UI.
}

await test("connecting starts a shell and Codex runs only after an explicit command", (context) => {
  const demo = new TerminalDemo(ignoreChange);
  context.after(() => { demo.dispose(); });
  assert.equal(demo.suggestedInput, "sarj-connect terminal");
  assert.equal(demo.attached, false);
  demo.submit("sarj-connect terminal");
  assert.equal(demo.currentTab.mode, "shell");
  assert.equal(demo.currentTab.lines.length, 0);
  assert.equal(demo.suggestedInput, "codex");
  demo.submit("codex");
  assert.equal(demo.currentTab.mode, "codex");
  assert.equal(demo.currentTab.busy, false);
  assert.equal(demo.prompt, "›");
});

await test("both Codex tabs finish in the background and survive detach and reconnect", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const demo = new TerminalDemo(ignoreChange);
  context.after(() => { demo.dispose(); });
  demo.submit("vm");
  demo.submit("codex");
  demo.submit("Simplify the README");
  demo.selectTab("tests");
  demo.submit("codex");
  demo.submit("Run the tests");
  demo.detach();
  context.mock.timers.runAll();
  assert.equal(demo.attached, false);
  assert.equal(demo.tabs.docs.busy, false);
  assert.equal(demo.tabs.tests.busy, false);
  assert.match(demo.tabs.docs.lines.at(-1)?.text ?? "", /Simplified/u);
  assert.match(demo.tabs.tests.lines.at(-1)?.text ?? "", /12 passed/u);
  const before = structuredClone(demo.tabs);
  demo.submit("sarj-connect terminal");
  assert.equal(demo.active, "tests");
  assert.deepEqual(demo.tabs, before);
  assert.equal(demo.connectedBefore, true);
});

await test("cancel affects only the active tab and leaves the other task running", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const demo = new TerminalDemo(ignoreChange);
  context.after(() => { demo.dispose(); });
  demo.submit("vm");
  demo.submit("codex");
  demo.submit("Simplify the README");
  demo.selectTab("tests");
  demo.submit("codex");
  demo.submit("Run the tests");
  demo.cancel();
  context.mock.timers.runAll();
  assert.match(demo.tabs.tests.lines.at(-1)?.text ?? "", /Interrupted/u);
  assert.equal(demo.tabs.tests.mode, "codex");
  assert.match(demo.tabs.docs.lines.at(-1)?.text ?? "", /Simplified/u);
});

await test("reset and dispose cancel pending callbacks instead of leaking into later state", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let changes = 0;
  const demo = new TerminalDemo(() => { changes += 1; });
  demo.submit("sarj-vm");
  demo.submit("codex");
  demo.submit("Simplify the README");
  demo.reset();
  const afterReset = changes;
  context.mock.timers.runAll();
  assert.equal(changes, afterReset);
  assert.equal(demo.connectedBefore, false);
  assert.equal(demo.tabs.docs.lines.length, 0);
  demo.submit("vm");
  demo.submit("codex");
  demo.submit("Run the tests");
  demo.dispose();
  const afterDispose = changes;
  context.mock.timers.runAll();
  demo.submit("another prompt");
  assert.equal(changes, afterDispose);
  assert.equal(demo.currentTab.busy, false);
  assert.equal(demo.currentTab.lines.at(-1)?.kind, "command");
});

await test("commands stay scoped to the local shell, remote shell, and Codex", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const demo = new TerminalDemo(ignoreChange);
  context.after(() => { demo.dispose(); });
  demo.submit("codex");
  assert.equal(demo.attached, false);
  demo.submit("sarj-connect browser https://example.com");
  assert.equal(demo.attached, false);
  assert.match(demo.localLines.at(-1)?.text ?? "", /persistent VM browser/u);
  demo.submit("vm; echo hacked");
  assert.equal(demo.attached, false);
  demo.submit("vm");
  demo.submit("sarj-connect terminal");
  assert.equal(demo.currentTab.mode, "shell");
  assert.match(demo.currentTab.lines.at(-1)?.text ?? "", /This demo supports/u);
  demo.submit("pwd");
  assert.equal(demo.currentTab.lines.at(-1)?.text, "~/demo/docs");
  demo.submit("codex");
  demo.submit("/unknown");
  assert.equal(demo.currentTab.busy, false);
  demo.submit("/exit");
  assert.equal(demo.currentTab.mode, "shell");
});

await test("input is bounded, inert text and transcripts remain short", (context) => {
  const demo = new TerminalDemo(ignoreChange);
  context.after(() => { demo.dispose(); });
  demo.submit("vm");
  demo.submit("x".repeat(301));
  assert.equal(demo.currentTab.lines.length, 1);
  assert.match(demo.currentTab.lines[0]?.text ?? "", /300 characters/u);
  const markup = "<img src=x onerror=alert(1)>";
  demo.submit(markup);
  assert.equal(demo.currentTab.lines.at(-2)?.text, `$ ${markup}`);
  for (let index = 0; index < 20; index += 1) demo.submit("ls");
  assert.equal(demo.currentTab.lines.length, 16);
  demo.submit("clear");
  assert.equal(demo.currentTab.lines.length, 0);
});
