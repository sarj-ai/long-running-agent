const projects = [
  ["code-standards", "Code Standards"],
  ["repo-standards", "Repo Standards"],
  ["lra", "Long Running Agent"],
  ["wiki", "Wiki"],
  ["workspace-sync", "Workspace Sync"],
];

for (const host of document.querySelectorAll(
  "engineering-project-navigation",
)) {
  const current = projects.find(
    ([slug]) => slug === host.getAttribute("project"),
  );
  if (!current || host.querySelector("details")) continue;

  const menu = document.createElement("details");
  menu.className = "project-menu";
  const summary = document.createElement("summary");
  summary.setAttribute(
    "aria-label",
    `${current[1]}: choose engineering project`,
  );
  const label = document.createElement("span");
  label.textContent = current[1];
  summary.append(label);
  const arrow = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  arrow.setAttribute("aria-hidden", "true");
  arrow.setAttribute("viewBox", "0 0 24 24");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "m7 10 5 5 5-5");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.5");
  arrow.append(path);
  summary.append(arrow);
  const nav = document.createElement("nav");
  nav.setAttribute("aria-label", "Engineering projects");
  const list = document.createElement("ul");
  for (const [slug, name] of [
    ["", "All projects"],
    ...projects,
    ["blog", "Sarj Blog"],
  ]) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href =
      slug === "blog"
        ? "https://sarj.ai/blog/"
        : slug
          ? `/projects/${slug}/`
          : "/";
    link.textContent = name;
    if (slug === current[0]) link.setAttribute("aria-current", "true");
    item.append(link);
    list.append(item);
  }
  nav.append(list);
  menu.append(summary, nav);
  host.querySelector(".project-name")?.replaceWith(menu);

  document.addEventListener("click", (event) => {
    if (event.target instanceof Node && !menu.contains(event.target))
      menu.open = false;
  });
  menu.addEventListener("focusout", (event) => {
    if (
      event.relatedTarget instanceof Node &&
      !menu.contains(event.relatedTarget)
    )
      menu.open = false;
  });
  menu.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu.open) {
      menu.open = false;
      summary.focus();
      event.stopPropagation();
    }
  });
}
