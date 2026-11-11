export function setupWikiNavigation(): void {
  const links = document.querySelectorAll<HTMLAnchorElement>(
    ".sidebar-content [data-wiki-section]",
  );
  for (const link of links) {
    link.setAttribute(
      "href",
      link.dataset.wikiSection === "details" ? "#details" : "#_top",
    );
  }
  const selectSection = (selected: "details" | "overview"): void => {
    for (const link of links) {
      if (link.dataset.wikiSection === selected) {
        link.setAttribute("aria-current", "location");
      } else {
        link.removeAttribute("aria-current");
      }
    }
  };
  const synchronize = (): void => {
    selectSection(location.hash === "#details" ? "details" : "overview");
  };
  synchronize();
  window.addEventListener("hashchange", synchronize);
  window.addEventListener("popstate", synchronize);
  for (const link of links) {
    link.addEventListener("click", () => {
      selectSection(
        link.dataset.wikiSection === "details" ? "details" : "overview",
      );
      const sidebar = document.getElementById("starlight__sidebar");
      if (sidebar?.matches(":popover-open")) sidebar.hidePopover();
    });
  }
}
