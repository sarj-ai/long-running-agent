import assert from "node:assert/strict";
import { test } from "node:test";

import { TerminalInput } from "./terminal-input.ts";

await test("editing preserves full Unicode characters and supports insertion at the cursor", () => {
  const input = new TerminalInput();
  input.replace("hello 👩‍💻 café");
  input.backspace();
  assert.equal(input.current.text, "hello 👩‍💻 caf");
  input.home();
  input.insert("Say ");
  assert.equal(input.beforeCursor, "Say ");
  assert.equal(input.afterCursor, "hello 👩‍💻 caf");
  input.end();
  input.move(-4);
  input.delete();
  assert.equal(input.current.text, "Say hello 👩‍💻caf");
});

await test("draft and history position survive switching panes without replacing unsent work", () => {
  const input = new TerminalInput();
  input.context("docs", "first command");
  assert.equal(input.submit(), "first command");
  input.insert("unsent work");
  input.history(-1);
  input.context("tests", "run tests");
  input.context("docs");
  assert.equal(input.current.text, "first command");
  input.history(1);
  assert.equal(input.current.text, "unsent work");
  input.context("tests");
  assert.equal(input.current.text, "run tests");
});

await test("paste is printable, bounded text and never submits a pasted newline", () => {
  const input = new TerminalInput();
  input.clear();
  input.insert("echo first\necho second\r\n\u{1B}]52;c;secret\u{7}");
  assert.equal(input.current.text, "echo first echo second ]52;c;secret");
  input.replace("a".repeat(299));
  input.insert("👩‍💻");
  assert.equal(input.current.text.length, 299);
});

await test("local reconnect is suggested once on return while reset removes old history", () => {
  const input = new TerminalInput();
  assert.equal(input.submit(), "sarj-connect terminal");
  input.context("local");
  assert.equal(input.current.text, "");
  input.context("docs");
  input.context("local");
  assert.equal(input.current.text, "sarj-connect terminal");
  input.reset();
  input.history(-1);
  assert.equal(input.current.text, "sarj-connect terminal");
});
