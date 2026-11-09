import { z } from "astro/zod";

export const env = z
  .object({
    WORKERS_CI_COMMIT_SHA: z.string().optional(),
    GITHUB_SHA: z.string().optional(),
  })
  .parse(process.env);
