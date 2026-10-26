import { hash } from "node:crypto";

import type { APIRoute } from "astro";

import generated from "../generated/reference.json";
import { SOURCE_REVISION } from "../lib/source-revision";

export const GET: APIRoute = () =>
  new Response(
    `${JSON.stringify({
      status: "ok",
      schemaVersion: generated.schemaVersion,
      packageVersion: generated.package.version,
      referenceSha256: hash("sha256", `${JSON.stringify(generated)}\n`, "hex"),
      commit: SOURCE_REVISION,
    })}\n`,
    { headers: { "Content-Type": "application/json; charset=utf-8" } },
  );
