---
title: Multi-Tenancy
description: Organization scoping and graph isolation
---

# Multi-Tenancy

Sibyl supports multiple organizations with complete data isolation. Each organization gets its own
knowledge graph, so data stays separate and secure.

## How It Works

### Isolated Namespaces

Each organization gets its own isolated SurrealDB namespace:

- **SurrealDB:** per-org namespace named `org_<uuid_hex>` (hyphens stripped). All graph tables live
  inside that namespace.

```python
# Sibyl derives the namespace/graph name from the organization UUID
# All operations require the group_id context
```

### Organization Context

Every graph operation requires organization context. There are **no defaults** - callers must
explicitly provide org scope:

```python
# EntityManager requires group_id
manager = EntityManager(client, group_id=str(org.id))

# This will raise an error
manager = EntityManager(client, group_id="")  # ValueError!
```

## Organization Management

### Creating Organizations

Organizations are typically created through the web UI or API:

```bash
# Via API
curl -X POST http://localhost:3334/api/orgs \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "My Organization"}'
```

### Listing Organizations

```bash
sibyl org list
```

### Switching Organizations

```bash
# Set the active organization by slug
sibyl org switch my-org

# Create a new organization
sibyl org create

# Manage organization members
sibyl org members
```

`sibyl org list` shows the orgs you belong to and which one is active.

## Authentication and Authorization

### JWT Tokens

JWT tokens include organization context in the `org` claim:

```python
# Token payload
{
    "sub": "user_abc123",      # User ID
    "org": "org_xyz789",       # Organization ID
    "exp": 1234567890          # Expiration
}
```

### API Keys

API keys are scoped to an organization:

```bash
# Create org-scoped API key
sibyl auth api-key create --name "CI/CD" --scopes mcp,api:read
```

### MCP Authentication

MCP requests extract organization from the authenticated token:

```python
# In server.py
async def _require_org_id() -> str:
    org_id = await _get_org_id_from_context()
    if not org_id:
        raise ValueError("Organization context required")
    return org_id
```

## Code Patterns

### Graph Runtime Pattern

Always resolve graph helpers with explicit org context:

```python
from sibyl_core.services.graph import get_surreal_graph_runtime

runtime = await get_surreal_graph_runtime(str(org_id))

manager = runtime.entity_manager
```

### Query Pattern

Org scoping is namespace-level: an org-scoped driver runs every query inside that org's
`org_<uuid_hex>` namespace, so queries do not carry a `group_id` predicate. Get isolation by using
the org-scoped runtime or a cloned driver, then write plain SurrealQL:

```python
# CORRECT - the org-scoped runtime queries the org's namespace
runtime = await get_surreal_graph_runtime(str(org_id))
result = await runtime.driver.execute_query(
    "SELECT * FROM entity WHERE entity_type = $type",
    type="task",
)

# WRONG - a shared, unscoped driver queries the wrong namespace
result = await client.client.driver.execute_query(
    "SELECT * FROM entity WHERE entity_type = $type",
    type="task",
)
```

### Driver Cloning

For org-specific operations, clone the driver:

```python
# Clone driver for org-specific graph
org_driver = client.client.driver.clone(str(org_id))

# Now queries go to the org's graph
result = await org_driver.execute_query(query)
```

## API Routes

### Organization Context in Routes

API routes extract org context from the authenticated user:

```python
from sibyl.auth.dependencies import get_current_organization

@router.get("/entities")
async def list_entities():
    org = await get_current_org()
    manager = EntityManager(client, group_id=str(org.id))
    return await manager.list_all()
```

### Route Files

Key route files handling organization context:

| File                        | Purpose                        |
| --------------------------- | ------------------------------ |
| `routes/orgs.py`            | Organization CRUD              |
| `routes/org_members.py`     | Membership management          |
| `routes/org_invitations.py` | Invitation handling            |
| `routes/entities.py`        | Entity operations (org-scoped) |
| `routes/tasks.py`           | Task operations (org-scoped)   |

## CLI Organization Support

### Switching Organization

```bash
# Switch the active organization by slug
sibyl org switch my-org

# Commands now run in that org context
sibyl task list  # Lists tasks in my-org
```

### Per-Command Project Override

The `--context` flag and `SIBYL_CONTEXT` override the active **project** for a single command, not
the organization. Switch organizations with `sibyl org switch`.

```bash
# Override project context for a single command
sibyl --context proj_xyz task list
# Or
SIBYL_CONTEXT=proj_xyz sibyl task list
```

## Auth Schema

### Organization Table

```surql
DEFINE TABLE organizations SCHEMAFULL;
DEFINE TABLE organization_members SCHEMAFULL;
```

### Graph Storage

```mermaid
flowchart TD
    DB[("SurrealDB Instance")]
    subgraph OrgA["Namespace · org_550e8400… (Org A)"]
        A1["Entity records"]
        A2["Raw memories"]
        A3["Relationships"]
    end
    subgraph OrgB["Namespace · org_6fa459ea… (Org B)"]
        B1["Fully isolated graph"]
    end
    subgraph Auth["Namespace · sibyl_auth"]
        AU["Auth and control plane"]
    end
    DB --> OrgA
    DB --> OrgB
    DB --> Auth
```

## Security Considerations

### Data Isolation

Organizations are completely isolated:

- **No cross-org queries** - Queries only see their org's graph
- **No shared data** - Each org has its own nodes and relationships
- **No data leakage** - Embeddings are org-scoped

### Access Control

```python
# Verify user belongs to requested org
async def require_org_access(user: User, org_id: str) -> Organization:
    org = await get_org(org_id)
    if not await user.has_access_to(org):
        raise PermissionDenied("No access to organization")
    return org
```

### Audit Logging

Track organization access:

```python
log.info(
    "org_access",
    user_id=user.id,
    org_id=org.id,
    action="list_entities"
)
```

## Common Issues

### Wrong Organization Data

**Symptom:** Seeing data from another organization or no data at all.

**Cause:** Missing or incorrect `group_id` in queries.

**Fix:** Verify org context is being passed correctly:

```python
# Debug: log the org_id being used
log.debug("query_context", org_id=org_id)
```

### "Graph not found"

**Symptom:** Queries fail with graph not found errors.

**Cause:** New organization with no entities yet.

**Fix:** The graph is created automatically when first entity is added.

### Permission Denied

**Symptom:** User can't access organization resources.

**Fix:** Verify user is a member of the organization:

```bash
sibyl org list  # Shows orgs user belongs to
```

## Best Practices

### 1. Always Validate Org Context

```python
if not org_id:
    raise ValueError("Organization context required")
```

### 2. Use Type Hints

```python
async def my_function(*, group_id: str) -> None:
    """Forces callers to use keyword argument."""
    ...
```

### 3. Log Organization Context

```python
log.info("operation", org_id=org_id, action="create_entity")
```

### 4. Test Multi-Tenancy

```python
async def test_org_isolation():
    # Create entities in org A
    await create_entity(org_id=org_a)

    # Query from org B should not see them
    results = await search(org_id=org_b)
    assert len(results) == 0
```

## Next Steps

- [MCP Configuration](./mcp-configuration.md) - Configure organization-scoped MCP access
- [Knowledge Graph](./knowledge-graph.md) - Understand graph structure
- [Task Management](./task-management.md) - Org-scoped task operations
