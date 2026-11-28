import { ENGINEERING_PROJECTS } from "./lib/engineering-projects.ts";
import { SITE_BASE_PATH, SITE_HOMEPAGE } from "./lib/paths.ts";

const ENGINEERING_BASE = "/engineering";
const OLD_PROJECT_BASE = "/projects/lra/";

interface Environment {
  ASSETS: { fetch(request: Request): Promise<Response> };
}

const worker = {
  fetch(
    request: Request,
    environment: Environment,
  ): Promise<Response> | Response {
    const url = new URL(request.url);
    const legacyProject = url.hostname === "lra.sarj.ai";
    const legacyEngineering = url.hostname === "engineering.sarj.ai";
    if (!legacyProject && !legacyEngineering && url.hostname !== "sarj.ai")
      return new Response("Not found", { status: 404 });
    const pathname = url.pathname.replace(/\/$/u, "");
    if (
      pathname === `${SITE_BASE_PATH}settings` ||
      ((legacyProject || legacyEngineering) &&
        pathname === `${OLD_PROJECT_BASE}settings`) ||
      (legacyProject && pathname === "/settings")
    ) {
      const destination = new URL("setup/", SITE_HOMEPAGE);
      destination.search = url.search;
      destination.hash = "vm-details";
      return Response.redirect(destination.href, 301);
    }
    if (legacyProject)
      return Response.redirect(legacyProjectDestination(url).href, 301);
    if (
      legacyEngineering &&
      /^\/projects\/lra\/(?:ui\/v1\/|fonts\/)/u.test(url.pathname)
    ) {
      url.pathname = `${ENGINEERING_BASE}${url.pathname}`;
      return environment.ASSETS.fetch(new Request(url, request));
    }
    if (legacyEngineering) {
      url.hostname = "sarj.ai";
      if (
        url.pathname !== ENGINEERING_BASE &&
        !url.pathname.startsWith(`${ENGINEERING_BASE}/`)
      )
        url.pathname = `${ENGINEERING_BASE}${url.pathname}`;
    }
    if (
      url.pathname !== ENGINEERING_BASE &&
      !url.pathname.startsWith(`${ENGINEERING_BASE}/`)
    )
      return new Response("Not found", { status: 404 });
    const directory =
      url.pathname === ENGINEERING_BASE ||
      url.pathname === `${ENGINEERING_BASE}/internal` ||
      ENGINEERING_PROJECTS.some(
        (project) => url.pathname === project.path.slice(0, -1),
      );
    if (directory) url.pathname += "/";
    if (legacyEngineering || directory) return Response.redirect(url.href, 308);
    return environment.ASSETS.fetch(request);
  },
} as const;

export default worker;

function legacyProjectDestination(url: URL): URL {
  const destination = new URL(SITE_HOMEPAGE);
  if (
    url.pathname === SITE_BASE_PATH.slice(0, -1) ||
    url.pathname === OLD_PROJECT_BASE.slice(0, -1)
  )
    destination.pathname = SITE_BASE_PATH;
  else if (url.pathname.startsWith(SITE_BASE_PATH))
    destination.pathname = url.pathname;
  else if (url.pathname.startsWith(OLD_PROJECT_BASE))
    destination.pathname = `${SITE_BASE_PATH}${url.pathname.slice(OLD_PROJECT_BASE.length)}`;
  else destination.pathname = `${SITE_BASE_PATH}${url.pathname.slice(1)}`;
  destination.search = url.search;
  return destination;
}
