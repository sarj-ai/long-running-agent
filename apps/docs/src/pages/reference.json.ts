import type { APIRoute } from "astro";

import generated from "../generated/reference.json";

export const GET: APIRoute = () =>
  new Response(`${JSON.stringify(generated)}\n`, {
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
