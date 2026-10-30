export const WIKI_DESCRIPTION = "A focused, open source workspace for Google Drive documents, search, and knowledge sharing.";

export const WIKI_FEATURES = [
  { title: "Find your way", description: "Navigate shared drives and folders, search documents, and return to recent pages." },
  { title: "Keep context", description: "Read documents alongside the workspace tree, organize folders, and share short document links." },
  { title: "Work with agents", description: "Connect through the MCP endpoint with explicit OAuth consent and manage connections from the workspace." },
  { title: "Make it yours", description: "A Next.js interface, a FastAPI backend, PostgreSQL, and Terraform deployment configuration." },
] as const;

export const WIKI_SETUP = `git clone https://github.com/sarj-ai/wiki.git
cd wiki
mise install
mise exec -- uv run scripts/wiki.py setup
cp backend/.env.example backend/.env`;

export const WIKI_KEYS = `mise exec -- uv --directory backend run python -c 'import secrets; from cryptography.fernet import Fernet; print("SECRET_KEY=" + secrets.token_urlsafe(48)); print("FERNET_KEY=" + Fernet.generate_key().decode())'`;

export const WIKI_MIGRATE = "mise exec -- uv --directory backend run alembic upgrade head";

export const WIKI_DEVELOP = `mise exec -- uv --directory backend run uvicorn app.main:app --reload
mise exec -- npm --prefix frontend run dev`;

export const WIKI_CHECK = `mise exec -- uv run scripts/wiki.py check
mise exec -- uv run scripts/wiki.py audit`;
