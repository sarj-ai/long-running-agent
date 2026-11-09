export const WIKI_DESCRIPTION =
  "Find, read, and polish Google Drive documents.";

export function setupWikiTabs(): void {
  const tabList = document.querySelector("[data-wiki-tabs]");
  if (!(tabList instanceof HTMLElement)) return;
  const tabs = [...tabList.querySelectorAll("button")];
  if (tabs.length === 0) return;
  for (const [index, tab] of tabs.entries()) {
    tab.addEventListener("click", () => activateTab(tab, tabs));
    tab.addEventListener("keydown", (event) => moveTab(event, tabs, index));
  }
  const desktop = window.matchMedia("(min-width: 48rem)");
  const orient = (): void => {
    tabList.ariaOrientation = desktop.matches ? "vertical" : "horizontal";
  };
  orient();
  desktop.addEventListener("change", orient);
}

function moveTab(
  event: KeyboardEvent,
  tabs: readonly HTMLButtonElement[],
  index: number,
): void {
  let next: number;
  switch (event.key) {
    case "ArrowUp":
    case "ArrowLeft":
      next = index - 1;
      break;
    case "ArrowDown":
    case "ArrowRight":
      next = index + 1;
      break;
    case "Home":
      next = 0;
      break;
    case "End":
      next = tabs.length - 1;
      break;
    default:
      return;
  }
  const active = tabs[(next + tabs.length) % tabs.length];
  event.preventDefault();
  activateTab(active, tabs);
  active.focus();
}

function activateTab(
  active: HTMLButtonElement,
  tabs: readonly HTMLButtonElement[],
): void {
  for (const tab of tabs) {
    const selected = tab === active;
    tab.ariaSelected = String(selected);
    tab.tabIndex = selected ? 0 : -1;
    const panel = document.getElementById(
      tab.getAttribute("aria-controls") ?? "",
    );
    if (panel) panel.hidden = !selected;
  }
}
