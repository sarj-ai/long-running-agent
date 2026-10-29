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

interface ActiveDocument {
  readonly inlineElements: ReadonlyMap<string, string>;
  readonly markup: string;
  readonly scriptAttributes: readonly string[];
}

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
  }, "Published source revision or reference digest differs");
  await verifyEngineering(mode);
  await verifyWiki(mode);
  if (mode === "artifact") await verifyTerminalStyles();
  process.stdout.write("Deployment verified.\n");
}

async function verifyWiki(mode: VerificationMode): Promise<void> {
  const artifact = await readFile(new URL("projects/wiki/index.html", DIST_ROOT), "utf8");
  const ownedDocument = activeDocument(artifact);
  const response = mode === "live" ? await deployedResponse(new URL("/projects/wiki/", SITE_URL), "text/html") : undefined;
  const document = response ? activeDocument(await response.text()) : ownedDocument;
  const headers = await readFile(new URL("_headers", DIST_ROOT), "utf8");
  const expectedPolicy = /(?:^|\n\n)\/projects\/wiki\/\*\n {2}Content-Security-Policy: ([^\n]+)/u.exec(headers)?.[1];
  assert.ok(expectedPolicy, "Immutable Wiki CSP missing");
  const policy = response ? response.headers.get("Content-Security-Policy") : expectedPolicy;
  assert.ok(policy, "Published Wiki CSP missing");
  verifyPolicy(policy, expectedPolicy, document);
  for (const [element, body] of ownedDocument.inlineElements) {
    assert.ok(document.inlineElements.has(element), "Owned Wiki script or style missing or changed");
    assert.ok(policy.includes(`'sha256-${hash("sha256", body, "base64")}'`), "Owned Wiki inline hash missing");
  }
  assert.match(document.markup, /<h1\b[^>]*>\s*Sarj Wiki\s*<\/h1>/u, "Wiki heading missing");
  assert.ok(document.markup.includes(`href="${SITE_URL.origin}/projects/wiki/"`), "Wiki canonical URL differs");
  for (const href of ["https://wiki.sarj.ai/demo", "https://github.com/sarj-ai/wiki", "https://wiki.sarj.ai/", "https://github.com/sarj-ai/wiki/blob/main/LICENSE"]) {
    assert.ok(document.markup.includes(`href="${href}"`), "Wiki project destination missing");
  }
  assert.doesNotMatch(document.markup, /http-equiv="content-security-policy"|\/wiki-project\//u, "Unpublished Wiki metadata survived promotion");
  await verifyEngineeringAssets(mode, ownedDocument.markup, document.markup);
  if (mode === "artifact") {
    await assert.rejects(readFile(new URL("wiki-project/index.html", ARTIFACT_ROOT)), { code: "ENOENT" });
  }
}

async function verifyEngineering(mode: VerificationMode): Promise<void> {
  const artifact = await readFile(new URL("index.html", DIST_ROOT), "utf8");
  const ownedDocument = activeDocument(artifact);
  const response = mode === "live" ? await deployedResponse(new URL("/", SITE_URL), "text/html") : undefined;
  const document = response ? activeDocument(await response.text()) : ownedDocument;
  const headers = await readFile(new URL("_headers", DIST_ROOT), "utf8");
  const expectedPolicy = /(?:^|\n\n)\/\n {2}Content-Security-Policy: ([^\n]+)/u.exec(headers)?.[1];
  assert.ok(expectedPolicy, "Immutable Engineering root CSP missing");
  const policy = response ? response.headers.get("Content-Security-Policy") : expectedPolicy;
  assert.ok(policy, "Published Engineering root CSP missing");
  verifyPolicy(policy, expectedPolicy, document);
  assert.ok(policy.includes("default-src 'none'") && policy.includes("font-src 'self'"), "Engineering root security directives missing");
  for (const [element, body] of ownedDocument.inlineElements) {
    assert.ok(document.inlineElements.has(element), "Owned inline script or style missing or changed");
    assert.ok(policy.includes(`'sha256-${hash("sha256", body, "base64")}'`), "Owned inline hash missing from Engineering root CSP");
  }
  verifyEngineeringMarkup(document.markup);
  await verifyEngineeringAssets(mode, ownedDocument.markup, document.markup);
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

function verifyPolicy(policy: string, expectedPolicy: string, document: ActiveDocument): void {
  const nonces = activeScriptNonces(document.scriptAttributes);
  const scriptNonces = new Set<string>();
  const expectedRows = policyRows(expectedPolicy);
  const normalized = policyRows(policy).map((sources, index) => {
    if (sources[0] !== "script-src" && sources[0] !== "script-src-elem") return sources;
    return sources.filter((source) => {
      if (expectedRows.at(index)?.includes(source)) return true;
      const nonce = /^'nonce-([A-Za-z0-9+/_-]+={0,2})'$/u.exec(source)?.at(1);
      if (!nonce) return true;
      assert.ok(nonces.has(nonce), "Engineering root CSP differs from immutable artifact");
      scriptNonces.add(nonce);
      return false;
    });
  });
  const coupled = normalized.map((sources, index) => {
    if (sources[0] !== "style-src") return sources;
    return sources.filter((source) => {
      if (expectedRows.at(index)?.includes(source)) return true;
      const nonce = /^'nonce-([A-Za-z0-9+/_-]+={0,2})'$/u.exec(source)?.at(1);
      if (!nonce) return true;
      assert.ok(scriptNonces.has(nonce), "Engineering root CSP differs from immutable artifact");
      return false;
    });
  });
  assert.deepEqual(coupled, expectedRows, "Engineering root CSP differs from immutable artifact");
}

function policyRows(policy: string): string[][] {
  return policy.split(";").map((directive) => directive.split(/[\t\n\f\r ]+/u).filter(Boolean));
}

function activeScriptNonces(sources: readonly string[]): ReadonlySet<string> {
  const nonces = new Set<string>();
  for (const source of sources) {
    const attributes = new Map<string, string>();
    let cursor = 0;
    for (const match of source.matchAll(/[\t\n\f\r ]*([^\t\n\f\r "'<>/=]+)(?:[\t\n\f\r ]*=[\t\n\f\r ]*(?:"([^"]*)"|'([^']*)'|([^\t\n\f\r "'=<>`]+)))?/guy)) {
      const name = match[1].toLowerCase();
      if (!attributes.has(name)) attributes.set(name, match.at(2) ?? match.at(3) ?? match.at(4) ?? "");
      cursor = match.index + match[0].length;
    }
    if (!/^[\t\n\f\r ]*\/?[\t\n\f\r ]*$/u.test(source.slice(cursor))) continue;
    const rawType = attributes.get("type") ?? "";
    const type = /^[A-Za-z/]*$/u.test(rawType) ? rawType.toLowerCase() : undefined;
    if (type === undefined || !["", "module", "text/javascript", "application/javascript"].includes(type)
      || (type !== "module" && attributes.has("nomodule"))
      || ["language", "for", "event"].some((name) => attributes.has(name))) continue;
    const nonce = attributes.get("nonce");
    if (nonce) nonces.add(nonce);
  }
  return nonces;
}

function activeDocument(document: string): ActiveDocument {
  const parts: string[] = [];
  const inlineElements = new Map<string, string>();
  const scriptAttributes: string[] = [];
  let templateDepth = 0;
  const tokens = document.matchAll(/<!--[\s\S]*?(?:--!?>|$)|<!\[CDATA\[[\s\S]*?(?:\]\]>|$)|<plaintext(?=[\t\n\f\r />])(?:"[^"]*"|'[^']*'|[^"'<>])*>[\s\S]*$|<(script|style|textarea|title|noscript|iframe|noembed|noframes|xmp)(?=[\t\n\f\r />])((?:"[^"]*"|'[^']*'|[^"'<>])*)>([\s\S]*?)(?:<\/\1\s*>|$)|<\/?template(?=[\t\n\f\r />])(?:"[^"]*"|'[^']*'|[^"'<>])*>|<(?:"[^"]*(?:"|$)|'[^']*(?:'|$)|[^"'>])*(?:>|$)|[^<]+|</giu);
  for (const match of tokens) {
    const [element, originalKind, attributes, body] = match;
    if (/^<(?:!|plaintext\b)/iu.test(element)) continue;
    if (/^<\/?template\b/iu.test(element)) {
      templateDepth = Math.max(0, templateDepth + (element.startsWith("</") ? -1 : 1));
      continue;
    }
    const kind = match.at(1)?.toLowerCase();
    if (templateDepth > 0 || (kind && !/^(?:script|style)$/u.test(kind))) continue;
    if (kind === "script") scriptAttributes.push(attributes);
    if (kind) {
      if (!/\bsrc\s*=/iu.test(attributes)) inlineElements.set(element, body);
      parts.push(`<${originalKind}${attributes}></${originalKind}>`);
      continue;
    }
    parts.push(element);
  }
  return { inlineElements, markup: parts.join(""), scriptAttributes };
}

function verifyEngineeringMarkup(document: string): void {
  assert.match(document, /<header\b[^>]*aria-label="Sarj Engineering"/u, "Engineering header missing");
  const mainContent = /<main\b[^>]*id="main-content"[^>]*>([\s\S]*?)<\/main>/u.exec(document)?.[1];
  assert.ok(mainContent, "Engineering main landmark missing");
  assert.match(mainContent, /<h1\b[^>]*>\s*Sarj Engineering\s*<\/h1>/u, "Engineering heading missing");
  const projects = /<ul\b[^>]*aria-label="Engineering projects"[^>]*>([\s\S]*?)<\/ul>/u.exec(mainContent)?.[1];
  assert.ok(projects, "Engineering projects list missing");
  const cards = projects.matchAll(/<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gu).toArray();
  assert.equal(cards.length, ENGINEERING_PROJECTS.length, "Engineering project card count differs");
  for (const project of ENGINEERING_PROJECTS) {
    const card = cards.find(([, href]) => href === project.path);
    assert.ok(card, `Engineering project link missing: ${project.path}`);
    assert.ok(card[2].includes(project.name), `Engineering project name missing: ${project.path}`);
    assert.ok(card[2].includes(project.description), `Engineering project description missing: ${project.path}`);
  }
  const canonical = document.matchAll(/<link\b[^>]*>/gu)
    .find(([tag]) => /\brel="canonical"/u.test(tag))?.[0];
  assert.ok(canonical?.includes(`href="${SITE_URL.origin}/"`), "Engineering canonical URL differs");
  assert.doesNotMatch(document, /http-equiv="content-security-policy"|\/engineering-home\//u, "Unpublished Engineering metadata survived promotion");
}

async function verifyEngineeringAssets(mode: VerificationMode, ownedDocument: string, document: string): Promise<void> {
  const resourcePattern = /<(?:link|script|img|source)\b[^>]*>/gu;
  const resourceElements = new Set(document.matchAll(resourcePattern).map(([element]) => element));
  for (const [element] of ownedDocument.matchAll(resourcePattern)) {
    assert.ok(resourceElements.has(element), "Owned resource element missing or changed");
  }
  const stylesheetReferences = new Set(stylesheetHrefs(document));
  const stylesheets = stylesheetHrefs(ownedDocument).map((href) => {
    assert.ok(stylesheetReferences.has(href), "Owned stylesheet reference missing");
    return new URL(href, SITE_URL);
  });
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
  const assets = referencedAssets(ownedDocument);
  const references = referencedAssets(document);
  for (const href of assets) assert.ok(references.has(href), "Owned script or image reference missing");
  await Promise.all([...assets].map(async (href) => assetBytes(mode, new URL(href, SITE_URL))));
}

function referencedAssets(document: string): ReadonlySet<string> {
  const assets = new Set(document.matchAll(/<(?:script|img)\b[^>]*\bsrc="([^"]+)"[^>]*>/gu).map((match) => match[1]));
  for (const match of document.matchAll(/<(?:source|img)\b[^>]*\bsrcset="([^"]+)"[^>]*>/gu)) {
    for (const source of match[1].split(",")) assets.add(source.trim().split(/\s+/u, 1)[0]);
  }
  return assets;
}

function stylesheetHrefs(document: string): string[] {
  return document.matchAll(/<link\b[^>]*>/gu)
    .filter(([tag]) => /\brel="stylesheet"/u.test(tag))
    .map(([tag]) => /\bhref="([^"]+)"/u.exec(tag)?.[1])
    .filter((href) => href !== undefined)
    .toArray();
}

async function assetBytes(mode: VerificationMode, url: URL): Promise<Buffer> {
  assert.equal(url.origin, SITE_URL.origin, "Owned asset origin differs");
  assert.equal(url.username, "", "Owned asset contains URL credentials");
  assert.equal(url.password, "", "Owned asset contains URL credentials");
  assert.ok(url.pathname.startsWith(SITE_URL.pathname), "Owned asset is outside the project namespace");
  const contentTypes = ASSET_CONTENT_TYPES.get(extname(url.pathname));
  assert.ok(contentTypes, `Unsupported owned asset: ${url.pathname}`);
  const bytes = await readFile(new URL(`.${url.pathname}`, DIST_ROOT));
  assert.ok(bytes.length > 0, `Empty immutable asset: ${url.pathname}`);
  if (mode === "live") {
    const response = await deployedResponse(url, contentTypes.join(", "));
    const contentType = response.headers.get("Content-Type")?.split(";", 1)[0]?.trim().toLowerCase();
    assert.ok(contentType && contentTypes.includes(contentType), `Asset content type differs: ${url.pathname}`);
    const deployedBytes = Buffer.from(await response.arrayBuffer());
    assert.ok(deployedBytes.equals(bytes), `Asset bytes differ: ${url.pathname}`);
  }
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
  assert.equal(response.status, HTTP_OK, `HTTP status differs: ${url.pathname}`);
  return response;
}

await main().catch((error: unknown) => {
  const context = error instanceof assert.AssertionError && !error.generatedMessage
    ? error.message.split("\n", 1)[0]
    : "check mode, source revision, reference integrity, and emitted homepage assets";
  process.stderr.write(`Deployment verification failed: ${context}.\n`);
  process.exitCode = 1;
});
