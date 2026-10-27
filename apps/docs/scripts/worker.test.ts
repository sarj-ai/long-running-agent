import assert from "node:assert/strict";
import test from "node:test";

import worker from "../src/worker.ts";

const LEGACY_URLS = [
  ["/", "/projects/lra/"],
  ["/cli/vm/browser/?from=old", "/projects/lra/cli/vm/browser/?from=old"],
  ["/projects/lra", "/projects/lra/"],
  ["/projects/lra/setup/?from=old", "/projects/lra/setup/?from=old"],
  ["/settings/archive/?from=old", "/projects/lra/settings/archive/?from=old"],
] as const;

const SETTINGS_URLS = [
  "https://engineering.sarj.ai/projects/lra/settings",
  "https://engineering.sarj.ai/projects/lra/settings/",
  "https://lra.sarj.ai/settings",
  "https://lra.sarj.ai/settings/",
  "https://lra.sarj.ai/projects/lra/settings",
  "https://lra.sarj.ai/projects/lra/settings/",
] as const;

for (const url of SETTINGS_URLS) {
  for (const query of ["", "?from=settings&value=a%2Fb&value=two+words"]) {
    await test(`${url}${query} redirects directly to the canonical VM details section`, async () => {
      const response = await worker.fetch(new Request(`${url}${query}`), {
        ASSETS: { fetch: () => Promise.reject(new Error("Settings requests must redirect")) },
      });
      assert.equal(response.status, 301);
      assert.equal(response.headers.get("location"), `https://engineering.sarj.ai/projects/lra/setup/${query}#vm-details`);
    });
  }
}

for (const [path, destination] of LEGACY_URLS) {
  await test(`legacy URL ${path} preserves its path and query without repeating the project prefix`, async () => {
    const response = await worker.fetch(new Request(`https://lra.sarj.ai${path}`), {
      ASSETS: { fetch: () => Promise.reject(new Error("Legacy requests must redirect")) },
    });
    assert.equal(response.status, 301);
    assert.equal(response.headers.get("location"), `https://engineering.sarj.ai${destination}`);
  });
}

for (const name of ["code-standards", "repo-standards", "lra"]) {
  await test(`${name} project root retains query strings when adding its trailing slash`, async () => {
    const response = await worker.fetch(new Request(`https://engineering.sarj.ai/projects/${name}?from=hub`), {
      ASSETS: { fetch: () => Promise.reject(new Error("Project roots must redirect")) },
    });
    assert.equal(response.status, 308);
    assert.equal(response.headers.get("location"), `https://engineering.sarj.ai/projects/${name}/?from=hub`);
  });
}

for (const path of ["/", "/projects/lra/", "/projects/lra/setup/", "/projects/unknown/", "/settings/", "/projects/lra/settings/archive/", "/projects/code-standards/settings/"]) {
  await test(`${path} uses native asset handling`, async () => {
    const request = new Request(`https://engineering.sarj.ai${path}`);
    const assetResponse = new Response("Asset response");
    const response = await worker.fetch(request, {
      ASSETS: {
        fetch: (received) => {
          assert.equal(received, request);
          return Promise.resolve(assetResponse);
        },
      },
    });
    assert.equal(response, assetResponse);
  });
}
