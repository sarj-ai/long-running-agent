import { hash } from "node:crypto";
import { mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";

import type { AstroIntegration } from "astro";

import { SITE_BASE_PATH, SITE_HOMEPAGE, siteHref } from "../lib/paths";

export const ENGINEERING_ROUTE = "engineering-home/";
export const WIKI_ROUTE = "wiki-project/";

const POLICY_PATTERN = /<meta http-equiv="content-security-policy" content="([^"]+)">/u;

function securityHeader(documents: readonly string[]): string {
  const policy = POLICY_PATTERN.exec(documents[0] ?? "")?.[1];
  if (!policy) throw new Error("Astro did not emit a Content Security Policy");
  const hashes = new Set<string>();
  for (const document of documents) {
    for (const match of document.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gu)) {
      hashes.add(`'sha256-${hash("sha256", match[1], "base64")}'`);
    }
  }
  const directive = /(script-src-elem [^;]*)(;)/u;
  const current = directive.exec(policy)?.[1];
  if (!current) throw new Error("Content Security Policy is missing script-src-elem");
  const additions = [...hashes].toSorted().filter((value) => !current.includes(value));
  const updated = policy.replace(directive, (_match, prefix: string, suffix: string) => `${prefix} ${additions.join(" ")}${suffix}`);
  const header = `  Content-Security-Policy: ${updated}`;
  if (Buffer.byteLength(header, "utf8") > 2_000) {
    throw new Error("Cloudflare Content Security Policy exceeds the header size limit");
  }
  return header;
}

async function writeLegacyRedirects(projectDirectory: URL, assetRoot: URL): Promise<void> {
  if (SITE_BASE_PATH === "/") return;
  const entries = await readdir(projectDirectory, { withFileTypes: true });
  const redirects = new Set<string>();
  for (const entry of entries) {
    if (entry.name === "_headers" || entry.name === "_redirects" || SITE_BASE_PATH.startsWith(`/${entry.name}/`)) continue;
    const legacy = `/${entry.name}`;
    if (entry.isDirectory()) {
      const destination = new URL(`${entry.name}/`, SITE_HOMEPAGE).href;
      redirects.add(`${legacy} ${destination} 301`);
      redirects.add(`${legacy}/ ${destination} 301`);
      redirects.add(`${legacy}/* ${destination}:splat 301`);
    } else if (entry.isFile() && entry.name !== "index.html") {
      redirects.add(`${legacy} ${new URL(entry.name, SITE_HOMEPAGE).href} 301`);
    }
  }
  await writeFile(new URL("_redirects", assetRoot), `${[...redirects].toSorted().join("\n")}\n`, "utf8");
}

export default function cloudflareArtifacts(assetRoot: URL): AstroIntegration {
  return {
    name: "sarj-cloudflare-static-artifacts",
    hooks: {
      "astro:build:start": () => rm(assetRoot, { recursive: true, force: true }),
      "astro:build:done": async ({ dir }) => {
        const files = await readdir(dir, { recursive: true });
        const paths = files
          .filter((path) => path.endsWith(".html"))
          .toSorted();
        const documents = await Promise.all(
          paths.map(async (path) => ({ path, document: await readFile(new URL(path, dir), "utf8") })),
        );
        const homepage = documents.find(({ path }) => path === `${ENGINEERING_ROUTE}index.html`);
        if (!homepage) throw new Error("Astro did not render the Engineering homepage");
        const wiki = documents.find(({ path }) => path === `${WIKI_ROUTE}index.html`);
        if (!wiki) throw new Error("Astro did not render the Wiki project page");
        const projectDocuments = documents.filter((page) => page !== homepage && page !== wiki);
        const header = securityHeader(projectDocuments.map(({ document }) => document));
        const homepageHeader = securityHeader([homepage.document]);
        const wikiHeader = securityHeader([wiki.document]);
        await Promise.all(projectDocuments.map(({ path, document }) =>
          writeFile(new URL(path, dir), document.replace(POLICY_PATTERN, ""), "utf8"),
        ));
        const headersUrl = new URL("_headers", dir);
        const headers = await readFile(headersUrl, "utf8");
        if (!headers.startsWith("/*\n")) throw new Error("Cloudflare headers must begin with the global route");
        const scoped = headers.replace("/*\n", () => `/*\n${header}\n`).split("\n")
          .map((line) => line.startsWith("/") ? siteHref(line) : line).join("\n");
        const [globalHeaders = ""] = headers.split("\n\n", 1);
        const homepageHeaders = globalHeaders.replace("/*\n", () => `/\n${homepageHeader}\n`);
        const wikiHeaders = globalHeaders.replace("/*\n", () => `/projects/wiki/*\n${wikiHeader}\n`);
        await writeFile(new URL("_headers", assetRoot), `${scoped}\n${homepageHeaders}\n\n${wikiHeaders}\n`, "utf8");
        await writeFile(new URL("index.html", assetRoot), homepage.document.replace(POLICY_PATTERN, ""), "utf8");
        await rm(new URL(ENGINEERING_ROUTE, dir), { recursive: true });
        await mkdir(new URL("projects/wiki/", assetRoot), { recursive: true });
        await writeFile(new URL("projects/wiki/index.html", assetRoot), wiki.document.replace(POLICY_PATTERN, ""), "utf8");
        await rm(new URL(WIKI_ROUTE, dir), { recursive: true });
        await writeLegacyRedirects(dir, assetRoot);
        if (dir.href !== assetRoot.href) await rm(headersUrl);
      },
    },
  };
}
