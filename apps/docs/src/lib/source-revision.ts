import { env } from "./env.ts";

export const SOURCE_REVISION: string =
  env.WORKERS_CI_COMMIT_SHA ?? env.GITHUB_SHA ?? "local";
