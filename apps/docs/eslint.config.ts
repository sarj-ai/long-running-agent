import sarj from "@sarj/eslint-plugin";
import astro from "eslint-plugin-astro";
import typescript from "typescript-eslint";

import strict from "./eslint.strict.ts";

const ASTRO_REACT_RULES: Record<string, "off"> = Object.fromEntries(
  strict.flatMap((config) => Object.keys(config.rules ?? {}))
    .filter((name) => name.startsWith("react/") || name.startsWith("react-hooks/"))
    .map((name): [string, "off"] => [name, "off"]),
);

export default [
  ...strict,
  ...astro.configs["flat/recommended"],
  {
    files: ["astro.config.ts", "scripts/verify-deployment.ts"],
    rules: {
      // These entrypoints refer to generated output directories, which are absent before a build.
      "unicorn/no-missing-local-resource": "off",
    },
  },
  {
    files: ["**/*.astro"],
    languageOptions: {
      parserOptions: {
        parser: typescript.parser,
        project: false,
        projectService: false,
      },
    },
    rules: {
      // Astro HTML uses its own renderer and virtual types; astro check owns those types.
      ...typescript.configs.disableTypeChecked.rules,
      ...ASTRO_REACT_RULES,
      ...sarj.configs.strict.rules,
      // Shared React/shadcn component conventions do not apply to native Astro markup.
      "@sarj/prefer-shadcn-primitives": "off",
    },
  },
] satisfies import("eslint").Linter.Config[];
