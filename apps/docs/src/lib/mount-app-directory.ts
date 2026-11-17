export function mountAppDirectory(): void {
  const root = document.querySelector<HTMLElement>("[data-app-directory]");
  if (!root) return;
  const search = root.querySelector<HTMLInputElement>("#app-search");
  const count = root.querySelector<HTMLElement>("[data-app-count]");
  const empty = root.querySelector<HTMLElement>("[data-app-empty]");
  const reset = root.querySelector<HTMLButtonElement>("[data-app-reset]");
  if (!search || !count || !empty || !reset) return;
  const cards = [...root.querySelectorAll<HTMLElement>("[data-app]")];
  const groups = [...root.querySelectorAll<HTMLElement>("[data-app-group]")];
  const filters = [
    ...root.querySelectorAll<HTMLButtonElement>("[data-app-category]"),
  ];
  let category = "";

  const update = (): void => {
    const query = search.value
      .trim()
      .toLowerCase()
      .split(/\s+/u)
      .filter(Boolean);
    let visible = 0;
    for (const card of cards) {
      const matchesCategory = !category || card.dataset.category === category;
      const text = card.dataset.search ?? "";
      card.hidden =
        !matchesCategory || !query.every((word) => text.includes(word));
      if (!card.hidden) visible++;
    }
    for (const group of groups)
      group.hidden = !group.querySelector("[data-app]:not([hidden])");
    for (const button of filters)
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.appCategory === category),
      );
    count.textContent =
      visible === cards.length
        ? `${String(visible)} apps`
        : `${String(visible)} of ${String(cards.length)} apps`;
    empty.hidden = visible > 0;
  };

  search.disabled = false;
  search.addEventListener("input", update);
  for (const button of filters) {
    button.disabled = false;
    button.addEventListener("click", () => {
      category = button.dataset.appCategory ?? "";
      update();
    });
  }
  reset.addEventListener("click", () => {
    search.value = "";
    category = "";
    update();
    search.focus();
  });
}
