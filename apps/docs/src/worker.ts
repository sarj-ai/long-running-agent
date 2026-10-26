import { ENGINEERING_PROJECTS } from "./lib/engineering-projects.ts";
import { SITE_BASE_PATH, SITE_HOMEPAGE } from "./lib/paths.ts";

interface Environment {
  ASSETS: { fetch(request: Request): Promise<Response> };
}

export default {
  fetch(request: Request, environment: Environment): Promise<Response> | Response {
    const url = new URL(request.url);
    if (url.hostname === "lra.sarj.ai") {
      const destination = new URL(SITE_HOMEPAGE);
      destination.pathname = url.pathname.startsWith(SITE_BASE_PATH) ? url.pathname : `${SITE_BASE_PATH}${url.pathname.slice(1)}`;
      if (url.pathname === SITE_BASE_PATH.slice(0, -1)) destination.pathname = SITE_BASE_PATH;
      destination.search = url.search;
      return Response.redirect(destination.href, 301);
    }
    if (ENGINEERING_PROJECTS.some((project) => url.pathname === project.path.slice(0, -1))) {
      url.pathname += "/";
      return Response.redirect(url.href, 308);
    }
    return environment.ASSETS.fetch(request);
  },
};
