import { hash } from "node:crypto";

import { ENGINEERING_PROJECTS } from "./engineering-projects";
import { SITE_ORIGIN } from "./paths";

const STYLES = `
  :root { color-scheme: light dark; font: 1rem/1.6 system-ui, sans-serif; }
  body { max-width: 38rem; margin: 12vh auto; padding: 0 1.5rem; }
  h1 { margin-bottom: 0; font-size: 2rem; letter-spacing: -.04em; }
  p { margin-top: .25rem; opacity: .7; }
  ul { list-style: none; padding: 0; margin-top: 2.5rem; }
  li { margin-block: 1.5rem; }
  a { color: inherit; font-size: 1.125rem; font-weight: 600; text-underline-offset: .2em; }
`;

export const ENGINEERING_POLICY: string = `default-src 'none'; base-uri 'none'; frame-ancestors 'none'; style-src 'sha256-${hash("sha256", STYLES, "base64")}'`;

export const ENGINEERING_INDEX: string = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Sarj engineering projects and developer tools.">
  <link rel="canonical" href="${SITE_ORIGIN}/">
  <title>Sarj Engineering</title>
  <style>${STYLES}</style>
</head>
<body>
  <main>
    <h1>Sarj Engineering</h1>
    <p>Projects and developer tools.</p>
    <ul>${ENGINEERING_PROJECTS.map((project) => `
      <li><a href="${project.path}">${project.name}</a><p>${project.description}</p></li>`).join("")}
    </ul>
  </main>
</body>
</html>
`;
