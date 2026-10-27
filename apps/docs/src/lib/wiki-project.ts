import { hash } from "node:crypto";

const STYLES = `
  :root { color-scheme: light dark; font: 1rem/1.65 system-ui, sans-serif; }
  * { box-sizing: border-box; }
  body { max-width: 56rem; margin: 0 auto; padding: 2rem 1.5rem 5rem; }
  a { color: inherit; text-underline-offset: .2em; }
  a:focus-visible { outline: 2px solid currentColor; outline-offset: 5px; }
  nav, .links { display: flex; flex-wrap: wrap; gap: 1.25rem; }
  nav { align-items: center; }
  nav a, .links a { display: inline-flex; align-items: center; min-height: 44px; }
  header { margin-block: 4rem 3rem; }
  h1 { margin: 0; font-size: clamp(2.5rem, 8vw, 4.5rem); line-height: 1.1; letter-spacing: -.05em; }
  .intro { max-width: 40rem; font-size: 1.25rem; }
  .muted { opacity: .7; }
  section { margin-top: 3rem; }
  .features { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr)); gap: 1.5rem; }
  .features h3 { margin-bottom: .25rem; }
  .features p { margin-top: 0; }
  pre { max-width: 100%; padding: 1rem; overflow-x: auto; border: 1px solid; border-radius: .5rem; font-size: .875rem; }
  code { overflow-wrap: anywhere; }
  pre code { overflow-wrap: normal; }
  footer { margin-top: 4rem; padding-top: 1rem; border-top: 1px solid; }
`;

export const WIKI_PROJECT_POLICY: string = `default-src 'none'; base-uri 'none'; frame-ancestors 'none'; style-src 'sha256-${hash("sha256", STYLES, "base64")}'`;

export const WIKI_PROJECT: string = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Sarj Wiki: an open source workspace for Google Drive documents, search, and knowledge sharing.">
  <link rel="canonical" href="https://engineering.sarj.ai/projects/wiki/">
  <title>Wiki · Sarj Engineering</title>
  <style>${STYLES}</style>
</head>
<body>
  <nav aria-label="Breadcrumb"><a href="/">Sarj Engineering</a><span aria-current="page">Wiki</span></nav>
  <main>
    <header>
      <p class="muted">Open source · MIT license</p>
      <h1>Sarj Wiki</h1>
      <p class="intro">Your Google Drive documents, in one focused workspace. Browse shared drives, find a page, and keep team knowledge close at hand.</p>
      <div class="links"><a href="https://wiki.sarj.ai/demo">Try the demo ↗</a><a href="https://github.com/sarj-ai/wiki">Browse the source ↗</a><a href="https://wiki.sarj.ai/">Open Sarj Wiki ↗</a></div>
      <p class="muted">Explore a read-only sample workspace with no sign-in. Browse Word, PowerPoint, Excel, and HTML files, and compare an AI-pasted draft with the same document clearly formatted.</p>
      <p class="muted">The full hosted workspace requires an authorized Google account. Self-host with your own Google OAuth application and infrastructure.</p>
    </header>
    <section aria-labelledby="features">
      <h2 id="features">A workspace around your documents</h2>
      <div class="features">
        <div><h3>Find your way</h3><p>Navigate shared drives and folders, search documents, and return to recent pages.</p></div>
        <div><h3>Keep context</h3><p>Read documents alongside the workspace tree, organize folders, and share short document links.</p></div>
        <div><h3>Work with agents</h3><p>Connect through the MCP endpoint with explicit OAuth consent and manage connections from the workspace.</p></div>
        <div><h3>Make it yours</h3><p>A Next.js interface, a FastAPI backend, PostgreSQL, and Terraform deployment configuration.</p></div>
      </div>
    </section>
    <section aria-labelledby="start">
      <h2 id="start">Run it locally</h2>
      <p>Install <a href="https://mise.jdx.dev/">mise</a> and PostgreSQL. The repository pins Node.js, npm, Python tooling, and Terraform.</p>
      <pre tabindex="0" aria-label="Clone and install commands"><code>git clone https://github.com/sarj-ai/wiki.git
cd wiki
mise install
mise exec -- uv run scripts/wiki.py setup
cp backend/.env.example backend/.env</code></pre>
      <p>Create an empty PostgreSQL database matching <code>DATABASE_URL</code>. Set the allowed Google domain and your own OAuth client credentials in <code>backend/.env</code>; the example lists the supported settings.</p>
      <p>Generate the signing and encryption keys, then copy their values into <code>backend/.env</code>:</p>
      <pre tabindex="0" aria-label="Generate local signing and encryption keys"><code>mise exec -- uv --directory backend run python -c 'import secrets; from cryptography.fernet import Fernet; print("SECRET_KEY=" + secrets.token_urlsafe(48)); print("FERNET_KEY=" + Fernet.generate_key().decode())'</code></pre>
      <pre tabindex="0" aria-label="Database migration command"><code>mise exec -- uv --directory backend run alembic upgrade head</code></pre>
      <p>Start these commands in separate terminals from the repository root:</p>
      <pre tabindex="0" aria-label="Backend and frontend development commands"><code>mise exec -- uv --directory backend run uvicorn app.main:app --reload
mise exec -- npm --prefix frontend run dev</code></pre>
      <p>Open <a href="http://localhost:3000/">localhost:3000</a>. Configure Google OAuth to return to <code>http://localhost:8000/auth/callback</code>.</p>
    </section>
    <section aria-labelledby="checks">
      <h2 id="checks">Check a change</h2>
      <pre tabindex="0" aria-label="Verification commands"><code>mise exec -- uv run scripts/wiki.py check
mise exec -- uv run scripts/wiki.py audit</code></pre>
      <p>The checks cover source standards, types, tests, the production frontend build, API schema consistency, and offline Terraform validation. Deployment configuration is in <code>terraform/</code> and <code>.github/workflows/</code>.</p>
    </section>
  </main>
  <footer><a href="https://github.com/sarj-ai/wiki/blob/main/LICENSE">MIT license</a> · <a href="https://github.com/sarj-ai/wiki/issues">Report an issue</a></footer>
</body>
</html>
`;
