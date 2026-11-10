The engineering gateway maintains the shared documentation shell in `v1/`:

- `docs.css`: navigation, logo sizing, GitHub alignment, reading defaults, breadcrumbs, compact filters, and print styles.
- `navigation.js`: the accessible project chooser used by Code Standards, Repo Standards, Long Running Agent, and Workspace Sync.
- `sarj-logo.svg`: the complete logo with padding around its artwork.

Each project's small `engineering-head.astro` adapter loads these same-origin assets after its project styles. Its `project-navigation.astro` provides a working home link and project label without JavaScript. The shared script adds a native details menu, Escape handling, and dismissal on outside clicks or focus changes.

The existing `@sarj/design` package (formerly docs-ui) still supplies the theme, reference-page, breadcrumb, code-comparison, and rule-pager components. Project CSS should contain only project-specific presentation. Change the shared files here to update all documentation apps together; avoid copying the shell back into the consumers.

Deploy the gateway before introducing a new asset path in a consumer. The `v1` paths are backward compatible and revalidate after five minutes. Introduce another versioned path for a breaking markup contract, retaining the previous version until its consumers have moved.
