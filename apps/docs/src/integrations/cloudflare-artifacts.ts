import { hash } from "node:crypto";
import { readdir, readFile, writeFile } from "node:fs/promises";

import type { AstroIntegration } from "astro";

const POLICY_PATTERN = /<meta http-equiv="content-security-policy" content="([^"]+)">/u;

export default function cloudflareArtifacts(): AstroIntegration {
  return {
    name: "sarj-cloudflare-static-artifacts",
    hooks: {
      "astro:build:done": async ({ dir }) => {
        const files = await readdir(dir, { recursive: true });
        const paths = files
          .filter((path) => path.endsWith(".html"))
          .toSorted();
        const documents = await Promise.all(
          paths.map(async (path) => ({ path, document: await readFile(new URL(path, dir), "utf8") })),
        );
        const policy = POLICY_PATTERN.exec(documents[0]?.document ?? "")?.[1];
        if (!policy) throw new Error("Astro did not emit a Content Security Policy");
        const hashes = new Set<string>();
        for (const { document } of documents) {
          for (const match of document.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gu)) {
            const body = match[1];
            hashes.add(`'sha256-${hash("sha256", body, "base64")}'`);
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
        await Promise.all(documents.map(({ path, document }) =>
          writeFile(new URL(path, dir), document.replace(POLICY_PATTERN, ""), "utf8"),
        ));
        const headersUrl = new URL("_headers", dir);
        const headers = await readFile(headersUrl, "utf8");
        if (!headers.startsWith("/*\n")) throw new Error("Cloudflare headers must begin with the global route");
        await writeFile(headersUrl, headers.replace("/*\n", () => `/*\n${header}\n`), "utf8");
      },
    },
  };
}
