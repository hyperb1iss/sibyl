# Public screenshot capture

Public screenshots must come from the isolated `Sibyl Showcase` organization. Never capture a
personal organization and never rename live data to make it look generic. Project names are only one
place where private text can appear. Tasks, memories, graph labels, search results, and source names
can expose the same data.

## Prepare the showcase organization

Create the local filter at `.moon/cache/showcase-private-terms.json`. The cache directory is ignored
by Git, so organization and product names never enter the repository or pull request:

```json
{
  "forbidden_terms": ["private organization", "private product"]
}
```

Start the local app and seed the curated fixture:

```bash
moon run dev
moon run showcase-seed
```

The seed command only accepts a loopback server. It creates or reuses the `sibyl-showcase`
organization, then refuses to continue if the organization contains an unknown entity, an unknown
source, an unmarked entity, or a forbidden private term. A successful second run should create zero
rows.

Use the normal local owner account only to prepare the organization. Invite
`sibyl-showcase@localhost` to `Sibyl Showcase`, then accept the invitation in a clean browser
profile with the account name `Sibyl Showcase`. Keep the account out of every other team
organization. Its automatically created personal organization is safe because it carries the same
showcase name.

Run the capture task and finish signing in through the visible browser window:

```bash
moon run web:capture-showcase
```

The task reruns the seed gate, then binds the browser to the organization ID from the verified
manifest. Before every frame, it reads the full corpus through the authenticated browser and
compares it with the exact snapshot sealed by the seed. It refuses any other account identity, any
unexpected team organization, or forbidden text in the account menu and rendered page. Screenshots
stay in a temporary directory until every route passes, then the complete set replaces the public
files. Credentials stay in the browser and never enter a command line, environment variable, or
committed file.

## Capture set

The capture task uses a 1322 by 916 viewport at 2x pixel density. Browser chrome stays out of the
image. The task captures these routes:

| Route       | Documentation image                         | README image                |
| ----------- | ------------------------------------------- | --------------------------- |
| `/`         | `docs/public/screenshots/web-dashboard.png` | `docs/images/dashboard.png` |
| `/projects` | `docs/public/screenshots/web-projects.png`  | `docs/images/projects.png`  |
| `/graph`    | `docs/public/screenshots/web-graph.png`     | `docs/images/graph.png`     |
| `/tasks`    | `docs/public/screenshots/web-tasks.png`     | `docs/images/tasks.png`     |
| `/entities` | `docs/public/screenshots/web-entities.png`  |                             |
| `/search`   | `docs/public/screenshots/web-search.png`    |                             |
| `/sources`  | `docs/public/screenshots/web-sources.png`   |                             |

The task searches for `Project scope belongs on every graph write` before capturing `/search` and
lets the graph settle before capturing `/graph`. The four README images are copied from their
documentation versions so each pair stays identical.

## Safety gate

The capture task runs the seed command immediately before opening the browser:

```bash
moon run showcase-seed
```

Stop if the command creates rows or reports an isolation failure. After capture, inspect every image
at full size. Check project chips, task descriptions, graph nodes, tooltips, account menus, recent
activity, search snippets, and source URLs. The committed image set must contain only the curated
showcase corpus.
