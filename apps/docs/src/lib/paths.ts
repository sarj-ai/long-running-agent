import metadata from "../../package.json";

const SITE_URL = new URL(metadata.homepage);

export const SITE_HOMEPAGE: string = SITE_URL.href;
export const SITE_ORIGIN: string = SITE_URL.origin;
export const SITE_BASE_PATH: string = SITE_URL.pathname;

export function siteHref(path: string): string {
  return `${SITE_BASE_PATH}${path.replace(/^\//u, "")}`;
}
