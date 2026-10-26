import assert from "node:assert/strict";
import { hash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { parseArgs } from "node:util";

import metadata from "../package.json" with { type: "json" };
import { SOURCE_REVISION } from "../src/lib/source-revision.ts";

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
  process.stdout.write("Deployment verified.\n");
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
