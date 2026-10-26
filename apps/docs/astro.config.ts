import sitemap from "@astrojs/sitemap";
import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";

import cloudflareArtifacts from "./src/integrations/cloudflare-artifacts";
import { SITE_BASE_PATH, SITE_ORIGIN } from "./src/lib/paths";
import { REFERENCE, SIDEBAR } from "./src/lib/reference";

const ASSET_ROOT = new URL("dist/", import.meta.url);

export default defineConfig({
  site: SITE_ORIGIN,
  base: SITE_BASE_PATH,
  outDir: `dist${SITE_BASE_PATH}`,
  output: "static",
  trailingSlash: "always",
  compressHTML: true,
  markdown: { syntaxHighlight: "prism" },
  security: {
    csp: {
      directives: [
        "base-uri 'self'",
        "connect-src 'self'",
        "default-src 'none'",
        "font-src 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "img-src 'self' data:",
        "object-src 'none'",
      ],
      scriptDirective: { resources: [{ resource: "'self'", kind: "element" }] },
      styleDirective: { resources: [{ resource: "'unsafe-inline'", kind: "attribute" }] },
    },
  },
  integrations: [
    sitemap(),
    starlight({
      title: REFERENCE.package.name,
      description: REFERENCE.package.description,
      favicon: "/favicon.ico",
      logo: {
        alt: "Sarj",
        dark: "./public/sarj-logo-dark.webp",
        light: "./public/sarj-logo-light.webp",
        replacesTitle: true,
      },
      disable404Route: true,
      customCss: ["@sarj/design/starlight.css", "./src/styles/global.css"],
      sidebar: SIDEBAR,
      social: [{ icon: "github", label: "GitHub", href: REFERENCE.package.repository }],
      pagefind: false,
      tableOfContents: false,
      credits: false,
      components: { PageTitle: "@sarj/design/PageAnchor.astro" },
    }),
    cloudflareArtifacts(ASSET_ROOT),
  ],
});
