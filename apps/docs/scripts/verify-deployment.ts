import assert from "node:assert/strict";
import { hash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { parseArgs } from "node:util";

import metadata from "../package.json" with { type: "json" };
import { SOURCE_REVISION } from "../src/lib/source-revision.ts";
import { TERMINAL_STYLE_HASHES } from "../src/lib/terminal-style-hashes.ts";

const SITE_URL = new URL(metadata.homepage);
const DIST_ROOT = new URL("../dist/", import.meta.url);
const ARTIFACT_ROOT = new URL(`.${SITE_URL.pathname}`, DIST_ROOT);
const REQUEST_TIMEOUT_MS = 15_000;
const HTTP_OK = 200;

async function main(): Promise<void> {
  const { positionals } = parseArgs({ allowPositionals: true });
  const [mode] = positionals;
  assert.equal(positionals.length, 1);
  assert.ok(mode === "artifact" || mode === "live");
  if (mode === "live") assert.match(SOURCE_REVISION, /^[a-f0-9]{40}$/u);
  const [healthBytes, referenceBytes] = await Promise.all([
    responseBytes(mode, "health.json"),
    responseBytes(mode, "reference.json"),
  ]);
  const health: unknown = JSON.parse(healthBytes.toString("utf8"));
  const reference: unknown = JSON.parse(referenceBytes.toString("utf8"));
  assert.partialDeepStrictEqual(reference, {});
  assert.partialDeepStrictEqual(health, {
    status: "ok",
    commit: SOURCE_REVISION,
    referenceSha256: hash("sha256", referenceBytes, "hex"),
  });
  if (mode === "artifact") await verifyTerminalStyles();
  process.stdout.write("Deployment verified.\n");
}

async function verifyTerminalStyles(): Promise<void> {
  const [headers, homepage] = await Promise.all([
    readFile(new URL("_headers", DIST_ROOT), "utf8"),
    readFile(new URL("index.html", ARTIFACT_ROOT), "utf8"),
  ]);
  const policy = /Content-Security-Policy: ([^\n]+)/u.exec(headers)?.[1];
  assert.ok(policy);
  for (const digest of TERMINAL_STYLE_HASHES) assert.ok(policy.includes(`'${digest}'`));
  for (const style of homepage.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/gu)) {
    assert.ok(policy.includes(`'sha256-${hash("sha256", style[1], "base64")}'`));
  }
  const stylesheets = homepage.matchAll(/<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"/gu).toArray();
  assert.ok(stylesheets.length > 0);
  const styles = await Promise.all(stylesheets.map(async (link) => readFile(new URL(`.${link[1]}`, DIST_ROOT), "utf8")));
  assert.ok(styles.some((css) => css.includes(".demo-viewport") && css.includes(".xterm-helper-textarea")));
}

async function responseBytes(mode: "artifact" | "live", name: string): Promise<Buffer> {
  if (mode === "artifact") return readFile(new URL(name, ARTIFACT_ROOT));
  const response = await fetch(new URL(name, SITE_URL), {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "User-Agent": "sarj-lra-deployment-verifier/1.0 (+https://github.com/sarj-ai/long-running-agent)",
    },
    redirect: "error",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  assert.equal(response.status, HTTP_OK);
  return Buffer.from(await response.arrayBuffer());
}

await main().catch(() => {
  process.stderr.write("Deployment verification failed: check mode, source revision, and reference integrity.\n");
  process.exitCode = 1;
});
