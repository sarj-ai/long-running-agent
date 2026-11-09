import { defineConfig } from "oxlint";

import { createConfig, ignorePatterns } from "./oxlint.strict.mts";

// xterm 6.0.0 generates three style elements for the single landing-page terminal.
// These exact hashes cover its fixed Mocha theme, 14px font, dimensions CSS, and scrollbar.
// Recheck in a browser under the production CSP when upgrading xterm or changing its options.
const managed = createConfig({ testFrameworks: ["node"] });
const astroReactRules: Record<string, "off"> = Object.fromEntries(
  Object.keys(managed.rules ?? {})
    .filter(
      (name) => name.startsWith("react/") || name.startsWith("react-hooks/"),
    )
    .map((name) => [name, "off"]),
);

export default defineConfig({
  extends: [managed],
  settings: managed.settings,
  ignorePatterns: [...ignorePatterns],
  overrides: [
    {
      files: ["astro.config.ts", "scripts/verify-deployment.ts"],
      rules: {
        // These entrypoints reference output directories created by the build.
        "unicorn-js/no-missing-local-resource": "off",
      },
    },
    {
      files: ["**/*.astro"],
      rules: {
        // Astro markup uses its own renderer rather than React components.
        ...astroReactRules,
        "@sarj/prefer-shadcn-primitives": "off",
      },
    },
  ],
});
