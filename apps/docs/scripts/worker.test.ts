import assert from "node:assert/strict";
import test from "node:test";

import { ENGINEERING_PROJECTS } from "../src/lib/engineering-projects.ts";
import worker from "../src/worker.ts";

const LEGACY_URLS = [
  ["/", "/engineering/projects/lra/"],
  [
    "/cli/vm/browser/?from=old",
    "/engineering/projects/lra/cli/vm/browser/?from=old",
  ],
  ["/projects/lra", "/engineering/projects/lra/"],
  [
    "/projects/lra/setup/?from=old",
    "/engineering/projects/lra/setup/?from=old",
  ],
  [
    "/settings/archive/?from=old",
    "/engineering/projects/lra/settings/archive/?from=old",
  ],
] as const;

const SETTINGS_URLS = [
  "https://sarj.ai/engineering/projects/lra/settings",
  "https://engineering.sarj.ai/projects/lra/settings",
  "https://sarj.ai/engineering/projects/lra/settings/",
  "https://lra.sarj.ai/settings",
  "https://lra.sarj.ai/settings/",
  "https://lra.sarj.ai/projects/lra/settings",
  "https://lra.sarj.ai/projects/lra/settings/",
] as const;

for (const url of SETTINGS_URLS) {
  for (const query of ["", "?from=settings&value=a%2Fb&value=two+words"]) {
    await test(`${url}${query} redirects directly to the canonical VM details section`, async () => {
      const response = await worker.fetch(new Request(`${url}${query}`), {
        ASSETS: {
          fetch: () =>
            Promise.reject(new Error("Settings requests must redirect")),
        },
      });
      assert.equal(response.status, 301);
      assert.equal(
        response.headers.get("location"),
        `https://sarj.ai/engineering/projects/lra/setup/${query}#vm-details`,
      );
    });
  }
}

for (const [path, destination] of LEGACY_URLS) {
  await test(`legacy URL ${path} preserves its path and query without repeating the project prefix`, async () => {
    const response = await worker.fetch(
      new Request(`https://lra.sarj.ai${path}`),
      {
        ASSETS: {
          fetch: async () => {
            throw new Error("Legacy requests must redirect");
          },
        },
      },
    );
    assert.equal(response.status, 301);
    assert.equal(
      response.headers.get("location"),
      `https://sarj.ai${destination}`,
    );
  });
}

for (const { id: name } of ENGINEERING_PROJECTS) {
  await test(`${name} project root retains query strings when adding its trailing slash`, async () => {
    const response = await worker.fetch(
      new Request(`https://sarj.ai/engineering/projects/${name}?from=hub`),
      {
        ASSETS: {
          fetch: () => Promise.reject(new Error("Project roots must redirect")),
        },
      },
    );
    assert.equal(response.status, 308);
    assert.equal(
      response.headers.get("location"),
      `https://sarj.ai/engineering/projects/${name}/?from=hub`,
    );
  });
}

for (const path of [
  "/",
  "/projects/lra/",
  "/projects/lra/setup/",
  "/projects/wiki/",
  "/projects/unknown/",
  "/settings/",
  "/projects/lra/settings/archive/",
  "/projects/code-standards/settings/",
]) {
  await test(`${path} uses native asset handling`, async () => {
    const request = new Request(`https://sarj.ai/engineering${path}`);
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

for (const [path, expected] of [
  ["/", "/engineering/"],
  [
    "/projects/wiki?keep=one&keep=two",
    "/engineering/projects/wiki/?keep=one&keep=two",
  ],
  [
    "/projects/lra/cli/vm/browser/?from=old",
    "/engineering/projects/lra/cli/vm/browser/?from=old",
  ],
  ["/internal/", "/engineering/internal/"],
  [
    "/engineering/projects/lra/setup/?q=one",
    "/engineering/projects/lra/setup/?q=one",
  ],
] as const) {
  await test(`old engineering ${path} redirects once to the apex`, async () => {
    const response = await worker.fetch(
      new Request(`https://engineering.sarj.ai${path}`),
      {
        ASSETS: {
          fetch: async () => {
            throw new Error("Legacy requests must redirect");
          },
        },
      },
    );
    assert.equal(response.status, 308);
    assert.equal(
      response.headers.get("location"),
      `https://sarj.ai${expected}`,
    );
  });
}
for (const url of [
  "https://sarj.ai/engineering-other/",
  "https://sarj.ai/blog/",
  "https://unrelated.example/engineering/",
]) {
  await test(`refuses unrelated mount ${url}`, async () => {
    const response = await worker.fetch(new Request(url), {
      ASSETS: {
        fetch: async () => {
          throw new Error("Must not reach assets");
        },
      },
    });
    assert.equal(response.status, 404);
  });
}

await test("old shared shell assets remain same-origin during the coordinated release", async () => {
  const request = new Request(
    "https://engineering.sarj.ai/projects/lra/ui/v1/docs.css?v=1",
  );
  const response = await worker.fetch(request, {
    ASSETS: {
      fetch: async (received) => {
        assert.equal(
          received.url,
          "https://engineering.sarj.ai/engineering/projects/lra/ui/v1/docs.css?v=1",
        );
        return new Response("stylesheet");
      },
    },
  });
  assert.equal(await response.text(), "stylesheet");
});
