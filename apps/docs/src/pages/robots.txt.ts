import type { APIRoute } from "astro";

import { SITE_BASE_PATH, SITE_HOMEPAGE } from "../lib/paths";

export const GET: APIRoute = () =>
  new Response(`User-agent: *\nAllow: ${SITE_BASE_PATH}\n\nSitemap: ${new URL("sitemap-index.xml", SITE_HOMEPAGE).href}\n`, {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
