import assert from "node:assert/strict";
import { hash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import { extname } from "node:path";
import { parseArgs } from "node:util";

import metadata from "../package.json" with { type: "json" };
import { ENGINEERING_PROJECTS } from "../src/lib/engineering-projects.ts";
import { SOURCE_REVISION } from "../src/lib/source-revision.ts";
import { TERMINAL_STYLE_HASHES } from "../src/lib/terminal-style-hashes.ts";

const SITE_URL = new URL(metadata.homepage);
const DIST_ROOT = new URL("../dist/", import.meta.url);
const ARTIFACT_ROOT = new URL(`.${SITE_URL.pathname}`, DIST_ROOT);
const REQUEST_TIMEOUT_MS = 15_000;
const HTTP_OK = 200;
const ASSET_CONTENT_TYPES: ReadonlyMap<string, readonly string[]> = new Map([
  [".css", ["text/css"]],
  [".js", ["text/javascript", "application/javascript"]],
  [".png", ["image/png"]],
  [".svg", ["image/svg+xml"]],
  [".webp", ["image/webp"]],
  [".woff2", ["font/woff2"]],
]);

type VerificationMode = "artifact" | "live";

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
  await verifyEngineering(mode);
  if (mode === "artifact") await verifyTerminalStyles();
  process.stdout.write("Deployment verified.\n");
}

async function verifyEngineering(mode: VerificationMode): Promise<void> {
  const response = mode === "live" ? await deployedResponse(new URL("/", SITE_URL), "text/html") : undefined;
  const document = response ? await response.text() : await readFile(new URL("index.html", DIST_ROOT), "utf8");
  const headers = response ? "" : await readFile(new URL("_headers", DIST_ROOT), "utf8");
  const policy = response?.headers.get("Content-Security-Policy")
    ?? /(?:^|\n\n)\/\n {2}Content-Security-Policy: ([^\n]+)/u.exec(headers)?.[1];
  assert.ok(policy);
  assert.ok(policy.includes("default-src 'none'") && policy.includes("font-src 'self'"));
  for (const match of document.matchAll(/<(script|style)\b(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/\1>/gu)) {
    assert.ok(policy.includes(`'sha256-${hash("sha256", match[2], "base64")}'`));
  }
  verifyEngineeringMarkup(document);
  await verifyEngineeringAssets(mode, document);
  if (mode === "artifact") {
    const files = await readdir(ARTIFACT_ROOT);
    const sitemapFiles = files.filter((file) => /^sitemap-\d+\.xml$/u.test(file));
    assert.ok(sitemapFiles.length > 0);
    const sitemapDocuments = await Promise.all(sitemapFiles.map(async (file) => readFile(new URL(file, ARTIFACT_ROOT), "utf8")));
    const sitemap = sitemapDocuments.join("\n");
    assert.ok(sitemap.includes(`<loc>${SITE_URL.origin}/</loc>`));
    assert.doesNotMatch(sitemap, /\/engineering-home\//u);
    assert.doesNotMatch(await readFile(new URL("_redirects", DIST_ROOT), "utf8"), /\/engineering-home/u);
    await assert.rejects(readFile(new URL("engineering-home/index.html", ARTIFACT_ROOT)), { code: "ENOENT" });
  }
}

function verifyEngineeringMarkup(document: string): void {
  assert.match(document, /<header\b[^>]*aria-label="Sarj Engineering"/u);
  const mainContent = /<main\b[^>]*id="main-content"[^>]*>([\s\S]*?)<\/main>/u.exec(document)?.[1];
  assert.ok(mainContent);
  assert.match(mainContent, /<h1\b[^>]*>\s*Sarj Engineering\s*<\/h1>/u);
  const projects = /<ul\b[^>]*aria-label="Engineering projects"[^>]*>([\s\S]*?)<\/ul>/u.exec(mainContent)?.[1];
  assert.ok(projects);
  const cards = projects.matchAll(/<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gu).toArray();
  assert.equal(cards.length, ENGINEERING_PROJECTS.length);
  for (const project of ENGINEERING_PROJECTS) {
    const card = cards.find(([, href]) => href === project.path);
    assert.ok(card);
    assert.ok(card[2].includes(project.name));
    assert.ok(card[2].includes(project.description));
  }
  const canonical = document.matchAll(/<link\b[^>]*>/gu)
    .find(([tag]) => /\brel="canonical"/u.test(tag))?.[0];
  assert.ok(canonical?.includes(`href="${SITE_URL.origin}/"`));
  assert.doesNotMatch(document, /http-equiv="content-security-policy"|\/engineering-home\//u);
}

async function verifyEngineeringAssets(mode: VerificationMode, document: string): Promise<void> {
  const stylesheets = stylesheetHrefs(document).map((href) => new URL(href, SITE_URL));
  assert.ok(stylesheets.length > 0);
  const fonts = new Set<string>();
  await Promise.all(stylesheets.map(async (url) => {
    const stylesheetBytes = await assetBytes(mode, url);
    const stylesheet = stylesheetBytes.toString("utf8");
    for (const match of stylesheet.matchAll(/url\(["']?([^"')\s]+\.woff2)["']?\)/gu)) {
      fonts.add(new URL(match[1], url).href);
    }
  }));
  assert.ok(fonts.size > 0);
  await Promise.all([...fonts].map(async (href) => assetBytes(mode, new URL(href))));
  const assets = new Set(document.matchAll(/<(?:script|img)\b[^>]*\bsrc="([^"]+)"[^>]*>/gu).map((match) => match[1]));
  for (const match of document.matchAll(/<(?:source|img)\b[^>]*\bsrcset="([^"]+)"[^>]*>/gu)) {
    for (const source of match[1].split(",")) assets.add(source.trim().split(/\s+/u, 1)[0]);
  }
  await Promise.all([...assets].map(async (href) => assetBytes(mode, new URL(href, SITE_URL))));
}

function stylesheetHrefs(document: string): string[] {
  return document.matchAll(/<link\b[^>]*>/gu)
    .filter(([tag]) => /\brel="stylesheet"/u.test(tag))
    .map(([tag]) => /\bhref="([^"]+)"/u.exec(tag)?.[1])
    .filter((href) => href !== undefined)
    .toArray();
}

async function assetBytes(mode: VerificationMode, url: URL): Promise<Buffer> {
  assert.equal(url.origin, SITE_URL.origin);
  assert.equal(url.username, "");
  assert.equal(url.password, "");
  assert.ok(url.pathname.startsWith(SITE_URL.pathname));
  const contentTypes = ASSET_CONTENT_TYPES.get(extname(url.pathname));
  assert.ok(contentTypes);
  if (mode === "artifact") {
    const bytes = await readFile(new URL(`.${url.pathname}`, DIST_ROOT));
    assert.ok(bytes.length > 0);
    return bytes;
  }
  const response = await deployedResponse(url, contentTypes.join(", "));
  const contentType = response.headers.get("Content-Type")?.split(";", 1)[0]?.trim().toLowerCase();
  assert.ok(contentType && contentTypes.includes(contentType));
  const bytes = Buffer.from(await response.arrayBuffer());
  assert.ok(bytes.length > 0);
  return bytes;
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

async function responseBytes(mode: VerificationMode, name: string): Promise<Buffer> {
  if (mode === "artifact") return readFile(new URL(name, ARTIFACT_ROOT));
  const response = await deployedResponse(new URL(name, SITE_URL), "application/json");
  return Buffer.from(await response.arrayBuffer());
}

async function deployedResponse(url: URL, accept: string): Promise<Response> {
  const response = await fetch(url, {
    cache: "no-store",
    headers: {
      Accept: accept,
      "User-Agent": "sarj-lra-deployment-verifier/1.0 (+https://github.com/sarj-ai/long-running-agent)",
    },
    redirect: "error",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  assert.equal(response.status, HTTP_OK);
  return response;
}

await main().catch(() => {
  process.stderr.write("Deployment verification failed: check mode, source revision, and reference integrity.\n");
  process.exitCode = 1;
});
