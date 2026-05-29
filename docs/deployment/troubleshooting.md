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

**SurrealDB Connection Refused:**

```
surrealdb error: connection refused
```

**Solutions:**

1. **Verify SurrealDB is running:**

   ```bash
   docker compose ps surrealdb
   kubectl get pods -n sibyl -l app=surrealdb
   ```

2. **Check connection settings:**

   ```bash
   # Environment variables
   echo $SIBYL_SURREAL_URL
   ```

3. **Test direct connection:**

   ```bash
   curl http://localhost:8000/health
   ```

## Authentication Issues

### Forbidden After Login

**Symptoms:**

- API response includes `"error": "forbidden"`.
- Response body says `"Invalid request data."` with remediation to check organization or project
  permissions.
- Login or refresh succeeds, but the next project, search, admin, or MCP request fails.

**What it usually means:**

The client has a valid Sibyl session, but the request does not match the user's organization,
project, memory-space, or admin permissions. This is different from an OIDC provider problem. In the
default install, OIDC is off and local auth is expected.

**Solutions:**

1. **Confirm the intended auth mode:**

   ```bash
   echo $SIBYL_LOCAL_AUTH_ENABLED
   echo $SIBYL_PUBLIC_SIGNUPS_ENABLED
   echo $SIBYL_OIDC
   ```

   Default local installs should have local auth enabled, public signups disabled, and no OIDC
   providers.

2. **For a new default install, complete setup first.**

   The first setup signup creates the owner/admin user. After setup, additional users need an invite
   unless `SIBYL_PUBLIC_SIGNUPS_ENABLED=true`.

3. **Check the active CLI context and token owner:**

   ```bash
   sibyl context
   sibyl auth status
   sibyl whoami
   ```

4. **For project-scoped failures, switch to a project the user can access or ask an owner/admin to
   add the membership.**

5. **For enterprise SSO, verify the IdP role claim emits `Sibyl.Member`, `Sibyl.Admin`, or
   `Sibyl.Owner`.**

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

3. **Reduce worker or API concurrency for memory-constrained pods.**

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

3. **Check Redis when the opt-in coordination backend is enabled:**
   ```bash
   redis-cli -h localhost -p 6381 -n 1 KEYS "*"
   ```

## Port Conflicts

### Local Data-Service Port Conflict

**Error:** SurrealDB or the optional Redis coordination profile cannot bind its port

**Solutions:**

1. **Find conflicting process:**

   ```bash
   lsof -i :8000
   lsof -i :6381
   ```

2. **Change port in compose:**

   Set `SIBYL_SURREAL_PORT` or `SIBYL_REDIS_PORT` before starting Docker Compose.

3. **Update environment:**
   ```bash
   SIBYL_SURREAL_URL=ws://127.0.0.1:<surreal-port>/rpc
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

### Historical Archive Rehearsal Database Unavailable

**Solutions:**

1. **Confirm the external rehearsal database is reachable:**

   ```bash
   psql "postgresql://$SIBYL_POSTGRES_USER:$SIBYL_POSTGRES_PASSWORD@$SIBYL_POSTGRES_HOST:$SIBYL_POSTGRES_PORT/$SIBYL_POSTGRES_DB" -c 'select 1'
   ```

2. **Confirm the retained archive contains the database dump sidecar:**

   ```bash
   tar -tzf migration-archive.tar.gz | grep postgres.sql
   ```

3. **Run restore only in explicit rehearsal mode with
   `--restore-database-dump --source-type legacy-archive --target-mode postgres-rehearsal`.**

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
