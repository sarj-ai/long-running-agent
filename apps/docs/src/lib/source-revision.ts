export const SOURCE_REVISION: string =
  // eslint-disable-next-line @sarj/no-raw-env -- Cloudflare supplies immutable build provenance.
  process.env["WORKERS_CI_COMMIT_SHA"] ??
  // eslint-disable-next-line @sarj/no-raw-env -- GitHub supplies the exact source revision for the build.
  process.env["GITHUB_SHA"] ??
  "local";
