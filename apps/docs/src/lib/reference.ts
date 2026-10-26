import type { StarlightUserConfig } from "@astrojs/starlight/types";

import generated from "../generated/reference.json";
import { siteHref } from "./paths";

interface Parameter {
  default: unknown;
  defaultKind: string;
  envvar: null | string | string[];
  help: string;
  hidden: boolean;
  isFlag: boolean;
  kind: string;
  metavar: string;
  multiple: boolean;
  name: string;
  nargs: number;
  options: string[];
  required: boolean;
  secondaryOptions: string[];
  type: string;
}

export interface Command {
  commands: Command[];
  deprecated: boolean | string;
  help: string;
  hidden: boolean;
  name: string;
  parameters: Parameter[];
  path: string[];
}

interface Entrypoint {
  aliasOf: null | string;
  command: Command;
  defaultCommand: null | string;
  name: string;
  target: string;
}

interface Projection {
  entrypoints: Entrypoint[];
  package: {
    name: string;
    version: string;
    description: string;
    requiresPython: string;
    license: string;
    repository: string;
    site: string;
    dependencies: string[];
    scripts: Record<string, string>;
    install: string[];
  };
  schemaVersion: number;
  settings: {
    schema: Record<string, unknown>;
    profilePath: string;
    example: string;
  };
  terraform: {
    roots: {
      name: string;
      path: string;
      inputs: {
        name: string;
        type: string;
        description: string;
        default: unknown;
        required: boolean;
        sensitive: boolean;
      }[];
      outputs: { name: string; description: string; sensitive: boolean }[];
      resources: { address: string; type: string; source: string }[];
    }[];
  };
}

export const REFERENCE: Projection = generated;

export interface CommandEntry {
  command: Command;
  entrypoint: Entrypoint;
}

function collect(entrypoint: Entrypoint, command: Command): CommandEntry[] {
  if (command.hidden) return [];
  return [
    { entrypoint, command },
    ...command.commands.flatMap((child) => collect(entrypoint, child)),
  ];
}

export const COMMAND_ENTRIES: CommandEntry[] = REFERENCE.entrypoints
  .filter((entrypoint) => !entrypoint.aliasOf)
  .flatMap((entrypoint) => collect(entrypoint, entrypoint.command));

export const CONNECTION_COMMANDS = REFERENCE.entrypoints
  .filter((entrypoint) => entrypoint.name === "sarj-connect")
  .flatMap((entrypoint) => entrypoint.command.commands);

export function commandHref(command: Command): string {
  return siteHref(commandPath(command));
}

function commandPath(command: Command): string {
  const [entrypoint, ...rest] = command.path;
  const canonical = REFERENCE.entrypoints.find((item) => item.name === entrypoint)?.aliasOf;
  return `/cli/${[canonical ?? entrypoint, ...rest].map(encodeURIComponent).join("/")}/`;
}

export function defaultLabel(
  kind: string,
  value: unknown,
  expression = "",
): string {
  if (kind === "factory") return expression || kind;
  if (kind === "unset") return "—";
  return JSON.stringify(value);
}

export function sourceHref(path: string): string {
  return `${REFERENCE.package.repository}/blob/main/${path.split("/").map(encodeURIComponent).join("/")}`;
}

export type Sidebar = NonNullable<StarlightUserConfig["sidebar"]>;

export const SIDEBAR: Sidebar = [
  { label: "Start", link: "/" },
  { label: "Commands", link: "/cli/" },
  { label: "Setup", link: "/setup/" },
  { label: "Settings", link: "/settings/" },
];
