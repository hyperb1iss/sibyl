# Troubleshooting Guide

Common issues and solutions for Sibyl deployments.

## Connection Issues

### Cannot Connect to API

**Symptoms:**

- Connection refused on port 3334
- Frontend shows "Cannot connect to server"

**Solutions:**

1. **Check if server is running:**

   ```bash
   # Docker Compose
   docker compose ps

   # Kubernetes
   kubectl get pods -n sibyl
   ```

2. **Check server logs:**

   ```bash
   # Docker Compose
   docker compose logs backend

   # Kubernetes
   kubectl logs -n sibyl -l app.kubernetes.io/component=backend
   ```

3. **Verify port binding:**

   ```bash
   # Check what's listening
   lsof -i :3334
   netstat -tlnp | grep 3334
   ```

4. **Check firewall rules** (if applicable)

### Database Connection Errors

**PostgreSQL Connection Refused:**

```
sqlalchemy.exc.OperationalError: connection refused
```

**Solutions:**

1. **Verify PostgreSQL is running:**

   ```bash
   docker compose ps postgres
   kubectl get pods -n sibyl -l app=postgres
   ```

2. **Check connection settings:**

   ```bash
   # Environment variables
   echo $SIBYL_POSTGRES_HOST
   echo $SIBYL_POSTGRES_PORT  # Should be 5433 for local dev
   ```

3. **Test direct connection:**

   ```bash
   # Docker Compose
   docker exec -it sibyl-postgres psql -U sibyl sibyl

   # From host
   psql -h localhost -p 5433 -U sibyl sibyl
   ```

**FalkorDB Connection Refused:**

```
redis.exceptions.ConnectionError: Connection refused
```

**Solutions:**

1. **Verify FalkorDB is running:**

   ```bash
   docker compose ps falkordb
   kubectl get pods -n sibyl -l app=falkordb
   ```

2. **Check connection settings:**

   ```bash
   echo $SIBYL_FALKORDB_HOST
   echo $SIBYL_FALKORDB_PORT  # Should be 6380 for local dev
   ```

3. **Test direct connection:**

   ```bash
   # Docker Compose
   docker exec -it sibyl-falkordb redis-cli -a sibyl_dev PING

   # From host
   redis-cli -h localhost -p 6380 -a sibyl_dev PING
   ```

## Graph Corruption

FalkorDB can occasionally become corrupted, especially after crashes or improper shutdowns.

### Symptoms

- Queries returning unexpected errors
- "Graph does not exist" errors
- Timeout errors on simple queries
- Server crash when accessing graph

### Recovery Options

**Option 1: Delete and Recreate Graph**

```bash
# Connect to FalkorDB
redis-cli -h localhost -p 6380 -a sibyl_dev

# List all graphs
GRAPH.LIST

# Delete corrupted graph (USE CAUTION - DATA LOSS)
GRAPH.DELETE <org-uuid>
```

After deletion, the graph will be recreated on first use, but all data is lost.

**Option 2: Restore from Backup**

If you have a backup (created via `/api/admin/backup`):

```bash
# Via API
curl -X POST https://sibyl.local/api/admin/restore \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @backup.json
```

**Option 3: Rebuild from PostgreSQL**

If graph data is lost but PostgreSQL data exists:

1. Re-run document ingestion from crawl sources
2. Entities will be re-extracted from documents

### Prevention

1. **Enable FalkorDB persistence:**

   ```bash
   # In compose or k8s, ensure data volume is mounted
   volumes:
     - falkordb_data:/var/lib/falkordb/data
   ```

2. **Graceful shutdowns:**
   ```bash
   docker compose stop  # Not docker compose kill
   kubectl delete pod <pod> --grace-period=30
   ```

## Authentication Issues

### JWT Token Invalid

**Symptoms:**

- 401 Unauthorized responses
- "Invalid token" errors

**Solutions:**

1. **Check JWT secret is set:**

   ```bash
   # Should be non-empty
   echo $SIBYL_JWT_SECRET
   ```

2. **Verify token hasn't expired:**
   - Default access token TTL: 60 minutes
   - Use refresh token to get new access token

3. **Check clock synchronization:**
   - JWT validation requires synchronized clocks
   - Server and client should have correct time

### GitHub OAuth Failing

**Symptoms:**

- "OAuth error" after GitHub redirect
- Missing callback URL

**Solutions:**

1. **Verify OAuth credentials:**

   ```bash
   echo $SIBYL_GITHUB_CLIENT_ID
   echo $SIBYL_GITHUB_CLIENT_SECRET
   ```

2. **Check callback URL in GitHub:**
   - Must match `SIBYL_PUBLIC_URL/api/auth/github/callback`
   - Example: `https://sibyl.local/api/auth/github/callback`

3. **Verify public URL:**
   ```bash
   echo $SIBYL_PUBLIC_URL
   ```

## Performance Issues

### Slow Queries

**Symptoms:**

- API responses taking > 5 seconds
- Timeouts on graph operations

**Solutions:**

1. **Check graph size:**

   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
     https://sibyl.local/api/admin/stats
   ```

2. **Limit query results:**
   - Use `limit` parameter on list endpoints
   - Paginate large result sets

3. **Check FalkorDB memory:**

   ```bash
   redis-cli -h localhost -p 6380 -a sibyl_dev INFO memory
   ```

4. **Increase Graphiti semaphore:**
   ```bash
   SIBYL_GRAPHITI_SEMAPHORE_LIMIT=20  # Default is 10
   ```

### High Memory Usage

**Symptoms:**

- OOMKilled pods in Kubernetes
- Container restarts

**Solutions:**

1. **Increase resource limits:**

   ```yaml
   backend:
     resources:
       limits:
         memory: 2Gi
       requests:
         memory: 512Mi
   ```

2. **Check for memory leaks:**

   ```bash
   kubectl top pods -n sibyl
   ```

3. **Reduce connection pool sizes:**
   ```bash
   SIBYL_POSTGRES_POOL_SIZE=5
   SIBYL_POSTGRES_MAX_OVERFLOW=10
   ```

### Worker Queue Backlog

**Symptoms:**

- Jobs not completing
- Crawl tasks stuck

**Solutions:**

1. **Check worker status:**

   ```bash
   kubectl logs -n sibyl -l app.kubernetes.io/component=worker -f
   ```

2. **Scale workers:**

   ```bash
   kubectl scale deployment sibyl-worker -n sibyl --replicas=3
   ```

3. **Check Redis (job queue):**
   ```bash
   redis-cli -h localhost -p 6380 -a sibyl_dev -n 1 KEYS "*"
   ```

## Port Conflicts

### FalkorDB Port Conflict

**Error:** Port 6380 already in use

This commonly happens if you have Redis running locally.

**Solutions:**

1. **Stop conflicting service:**

   ```bash
   # macOS
   brew services stop redis

   # Linux
   sudo systemctl stop redis
   ```

2. **Change port in compose:**

   ```yaml
   falkordb:
     ports:
       - "6381:6379" # Use different host port
   ```

3. **Update environment:**
   ```bash
   SIBYL_FALKORDB_PORT=6381
   ```

### PostgreSQL Port Conflict

**Error:** Port 5433 already in use

**Solutions:**

1. **Find conflicting process:**

   ```bash
   lsof -i :5433
   ```

2. **Change port in compose:**

   ```yaml
   postgres:
     ports:
       - "5434:5432"
   ```

3. **Update environment:**
   ```bash
   SIBYL_POSTGRES_PORT=5434
   ```

## Kubernetes-Specific Issues

### Pods Stuck in Pending

**Solutions:**

1. **Check node resources:**

   ```bash
   kubectl describe nodes
   kubectl top nodes
   ```

2. **Check resource requests:**

   ```bash
   kubectl describe pod <pod-name> -n sibyl
   ```

3. **Reduce resource requests:**
   ```yaml
   resources:
     requests:
       cpu: 50m # Reduce from 100m
       memory: 128Mi # Reduce from 256Mi
   ```

### Pods CrashLoopBackOff

**Solutions:**

1. **Check logs:**

   ```bash
   kubectl logs <pod-name> -n sibyl --previous
   ```

2. **Check events:**

   ```bash
   kubectl get events -n sibyl --sort-by='.lastTimestamp'
   ```

3. **Common causes:**
   - Missing secrets
   - Database not ready
   - Port already bound

### CNPG PostgreSQL Not Starting

**Solutions:**

1. **Check CNPG operator:**

   ```bash
   kubectl get pods -n cnpg-system
   kubectl logs -n cnpg-system deployment/cnpg-cloudnative-pg
   ```

2. **Check cluster status:**

   ```bash
   kubectl get cluster -n sibyl
   kubectl describe cluster sibyl-postgres -n sibyl
   ```

3. **Check PVC:**
   ```bash
   kubectl get pvc -n sibyl
   ```

### Kong Gateway Issues

**Solutions:**

1. **Check Kong operator:**

   ```bash
   kubectl get pods -n kong-system
   ```

2. **Check gateway:**

   ```bash
   kubectl get gateway -n kong
   kubectl describe gateway sibyl-gateway -n kong
   ```

3. **Check dataplane:**

   ```bash
   kubectl get pods -n kong
   kubectl logs -n kong -l app=dataplane-sibyl-gateway
   ```

4. **Check HTTPRoutes:**
   ```bash
   kubectl get httproute -n sibyl
   kubectl describe httproute sibyl-api -n sibyl
   ```

## Tilt-Specific Issues

### Tilt Stuck on Resource

**Solutions:**

1. **Check Tilt logs:**
   - Click on stuck resource in Tilt UI
   - Look for error messages

2. **Trigger manual rebuild:**

   ```bash
   tilt trigger <resource-name>
   ```

3. **Full reset:**
   ```bash
   tilt down
   # If using Minikube:
   minikube delete
   minikube start --cpus=4 --memory=8192
   # If using OrbStack: restart via OrbStack app or `orb restart`)
   tilt up
   ```

### Can't Access sibyl.local

**Solutions:**

1. **Check /etc/hosts:**

   ```bash
   grep sibyl.local /etc/hosts
   # Should show: 127.0.0.1 sibyl.local
   ```

2. **Check Caddy is running:**
   - Look for `caddy-proxy` in Tilt UI

3. **Check Kong port-forward:**
   - Look for `kong-port-forward` in Tilt UI

4. **Trust Caddy CA (macOS):**
   ```bash
   sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain \
     ~/.local/share/caddy/pki/authorities/local/root.crt
   ```

## Getting Help

If you're still stuck:

1. **Check logs thoroughly:**

   ```bash
   # All logs with timestamps
   kubectl logs -n sibyl --all-containers --timestamps -l app.kubernetes.io/name=sibyl
   ```

2. **Describe resources:**

   ```bash
   kubectl describe pod <pod-name> -n sibyl
   kubectl describe deployment <deploy-name> -n sibyl
   ```

3. **Check events:**

   ```bash
   kubectl get events -n sibyl --sort-by='.lastTimestamp' | tail -50
   ```

4. **File an issue:**
   - Include logs and error messages
   - Describe reproduction steps
   - Include environment details (OS, K8s version, etc.)
