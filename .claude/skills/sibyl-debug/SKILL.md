---
name: sibyl-debug
description: Debug FalkorDB, Graphiti, and connection issues in Sibyl. Use when encountering graph crashes, connection drops, missing entities, or query failures.
---

# Sibyl Debugging Guide

## Common Issues & Solutions

### 1. FalkorDB Connection Issues

**Symptoms:**
- "Connection closed by server"
- Intermittent failures

**Debugging:**
```bash
# Check if FalkorDB is running
docker ps | grep sibyl-falkordb

# Check FalkorDB logs
docker logs sibyl-falkordb --tail 100

# Test connection directly
docker exec sibyl-falkordb redis-cli -a conventions ping
```

**Common fixes:**
- Restart FalkorDB: `docker compose restart`
- Verify port 6380 is correct (not 6379)

### 2. Entities Not Found / Empty Stats

**Symptoms:**
- `sibyl stats` shows 0 for entity types
- Entity detail view fails with 404
- Search returns no results

**Root Cause:** Query using wrong node label. Graphiti creates:
- `Episodic` nodes (from `add_episode`)
- `Entity` nodes (from LLM extraction)

**Fix:** Ensure queries check both labels:
```cypher
MATCH (n)
WHERE (n:Episodic OR n:Entity) AND n.entity_type = $type
RETURN n
```

### 3. Session ID Errors (MCP)

**Symptom:** "Bad Request: No valid session ID provided"

**Cause:** MCP server not running or session expired.

**Fix:**
```bash
# Restart Sibyl server
sibyl serve

# Or for development with reload
sibyl dev
```

### Debug Queries

```bash
# Count all nodes
docker exec sibyl-falkordb redis-cli -a conventions \
  GRAPH.QUERY conventions "MATCH (n) RETURN count(n)"

# Check node labels
docker exec sibyl-falkordb redis-cli -a conventions \
  GRAPH.QUERY conventions "MATCH (n) RETURN labels(n), count(*) ORDER BY count(*) DESC"

# Check entity_type distribution
docker exec sibyl-falkordb redis-cli -a conventions \
  GRAPH.QUERY conventions "MATCH (n) WHERE n.entity_type IS NOT NULL RETURN n.entity_type, count(*)"

# Find specific entity
docker exec sibyl-falkordb redis-cli -a conventions \
  GRAPH.QUERY conventions "MATCH (n {uuid: 'your-uuid'}) RETURN n"

# Check node structure
docker exec sibyl-falkordb redis-cli -a conventions \
  GRAPH.QUERY conventions "MATCH (n) RETURN labels(n), keys(n) LIMIT 5"
```

### 4. Write Lock Issues

**Symptom:** Deadlocks or very slow writes

**Debug:**
```python
# Add logging to see lock acquisition
import logging
logging.getLogger("sibyl.graph.client").setLevel(logging.DEBUG)
```

**Fix:** Ensure all writes use `execute_write()`:
```python
# Good
await client.execute_write("CREATE (n:Entity {...})", ...)

# Bad - bypasses lock
await client.client.driver.execute_query("CREATE ...")
```

### Health Check

```bash
# Quick health check
sibyl health

# Detailed stats
sibyl stats

# Check Docker
docker ps | grep sibyl
docker logs sibyl-falkordb --tail 50
```

### Check Sibyl Knowledge Base

Before debugging from scratch, search Sibyl for known solutions:

```bash
# Via CLI
sibyl search "your error message"

# Via MCP (if available)
search("connection error FalkorDB", types=["episode", "error_pattern"])
```

Sibyl contains documentation for all components and past debugging sessions.
