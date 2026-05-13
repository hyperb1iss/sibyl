# Screenshots Needed for Documentation

This file tracks all screenshots required for the documentation. Each screenshot should be captured
and placed in `docs/public/screenshots/`.

## Naming Pattern

`{section}-{description}.png` - e.g., `cli-task-list-output.png`

## Screenshots to Capture

### Guide Section

- [ ] `guide-architecture-diagram.png` - High-level architecture overview
- [ ] `guide-web-dashboard.png` - Web UI dashboard view
- [ ] `guide-knowledge-graph-visualization.png` - SurrealDB graph visualization

### CLI Section

- [ ] `cli-task-list.png` - Task list table output
- [ ] `cli-search-results.png` - Search results with semantic matches
- [ ] `cli-task-show.png` - Task detail view
- [ ] `cli-project-list.png` - Project listing
- [ ] `cli-context.png` - Context command showing linked project

### Web UI Section

- [ ] `web-dashboard.png` - Main dashboard
- [ ] `web-task-detail.png` - Task detail page
- [ ] `web-entity-explorer.png` - Entity explorer view
- [ ] `web-search.png` - Search interface

### API Section

- [ ] `api-swagger.png` - OpenAPI docs at /api/docs
- [ ] `api-health-check.png` - Health endpoint response

### Deployment Section

- [ ] `deploy-tilt-ui.png` - Tilt dashboard showing all resources
- [ ] `deploy-k8s-pods.png` - kubectl get pods output
- [ ] `deploy-kong-routes.png` - Kong gateway routes

---

## Capture Instructions

1. Start the dev environment: `moon run dev`
2. Wait for services to be healthy
3. Use macOS screenshot tool (Cmd+Shift+4) or similar
4. Crop to relevant content
5. Save to `docs/public/screenshots/`
6. Update this file marking items as complete

## Post-Processing

Consider running through ImageOptim or similar to reduce file sizes before committing.
