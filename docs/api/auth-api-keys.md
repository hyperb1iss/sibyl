# Authentication: API Keys

Scoped API keys for programmatic access to Sibyl.

## Overview

API keys provide:

- Long-lived credentials for automation
- Scoped permissions (read, write, MCP)
- Organization-bound access
- Revocation support

**Key Format:** `sk_live_<random>` or `sk_test_<random>`

## Key Scopes

| Scope       | Description                                      |
| ----------- | ------------------------------------------------ |
| `mcp`       | Access to `/mcp` MCP endpoints                   |
| `api:read`  | Read operations on `/api/*` (GET, HEAD, OPTIONS) |
| `api:write` | All operations on `/api/*` (implies read)        |

Beyond these access scopes, a key can also be bound to specific projects and memory spaces. Those
bindings further restrict which projects and memory scopes the key can read or write, independent of
the access scope. A key with no project or memory-space binding inherits the creating user's full
access within the organization.

## Creating API Keys

### Via CLI

```bash
sibyl auth api-key create --name "CI/CD Pipeline" --scopes mcp,api:read
```

### Via REST API

```http
POST /api/auth/api-keys
```

**Request:**

```json
{
  "name": "CI/CD Pipeline",
  "scopes": ["mcp", "api:read"],
  "expires_days": 365,
  "live": true
}
```

**Response:**

```json
{
  "id": "key_uuid",
  "name": "CI/CD Pipeline",
  "prefix": "sk_live_abc123...",
  "scopes": ["mcp", "api:read"],
  "created_at": "2024-12-30T10:00:00Z",
  "expires_at": "2025-12-31T23:59:59Z",
  "api_key": "sk_live_abc123def456ghi789..."
}
```

> **Important:** The full key is only returned once on creation. Store it securely.

## Using API Keys

### Authorization Header

```bash
curl -X GET "https://api.example.com/api/entities" \
  -H "Authorization: Bearer sk_live_abc123def456ghi789..."
```

### MCP Endpoint

```bash
curl -X POST "https://api.example.com/mcp" \
  -H "Authorization: Bearer sk_live_abc123..." \
  -H "Content-Type: application/json" \
  -d '{
    "method": "tools/call",
    "params": {
      "name": "search",
      "arguments": {"query": "OAuth patterns"}
    }
  }'
```

## Key Storage

API keys are stored securely:

1. **Prefix stored in plaintext** - For lookup (`sk_live_abc123...`)
2. **Full key hashed with PBKDF2** - For verification
   - Algorithm: SHA-256
   - Iterations: 210,000
   - Salt: 16 random bytes

## Key Management

### List Keys

```http
GET /api/auth/api-keys
```

**Response:**

```json
{
  "keys": [
    {
      "id": "key_uuid",
      "name": "CI/CD Pipeline",
      "prefix": "sk_live_abc123...",
      "scopes": ["mcp", "api:read"],
      "created_at": "2024-12-30T10:00:00Z",
      "expires_at": "2025-12-31T23:59:59Z",
      "last_used_at": "2024-12-30T15:30:00Z",
      "revoked_at": null
    }
  ]
}
```

### Revoke Key

```http
POST /api/auth/api-keys/{api_key_id}/revoke
```

**Response:**

```json
{
  "success": true,
  "id": "key_uuid"
}
```

Revoked keys immediately stop working.

## Scope Enforcement

### REST API Scopes

| HTTP Method              | Required Scope            |
| ------------------------ | ------------------------- |
| GET, HEAD, OPTIONS       | `api:read` OR `api:write` |
| POST, PUT, PATCH, DELETE | `api:write`               |

### MCP Scope

The `mcp` scope is required for `/mcp` endpoint access:

```json
{
  "scopes": ["mcp"]
}
```

Without `mcp` scope, MCP requests return 403.

### Combined Scopes

For full access:

```json
{
  "scopes": ["mcp", "api:write"]
}
```

## Authentication Flow

1. Extract token from Authorization header
2. Check if token starts with `sk_`
3. Look up key by prefix in database
4. Verify key hasn't been revoked
5. Check key hasn't expired
6. Verify full key hash matches
7. Update last_used_at timestamp
8. Return user and organization context

## Organization Binding

API keys are bound to:

- **User** who created them
- **Organization** active at creation time

All operations use the bound organization context.

## Expiration

Keys can have optional expiration:

```json
{
  "expires_at": "2025-12-31T23:59:59Z"
}
```

Expired keys return 401 with the standard error envelope:

```json
{
  "error": "authentication_required",
  "message": "Authentication failed.",
  "request_id": "req_a1b2c3d4e5f6",
  "remediation": "Run 'sibyl auth login' or set SIBYL_AUTH_TOKEN."
}
```

## Error Responses

API key failures use the standard error envelope and echo an `X-Request-ID` header.

| Status | Error code                | Cause                            |
| ------ | ------------------------- | -------------------------------- |
| 401    | `authentication_required` | Invalid, expired, or revoked key |
| 403    | `forbidden`               | Insufficient scope for operation |

**Invalid Key:**

```json
{
  "error": "authentication_required",
  "message": "Authentication failed.",
  "request_id": "req_a1b2c3d4e5f6",
  "remediation": "Run 'sibyl auth login' or set SIBYL_AUTH_TOKEN."
}
```

**Insufficient Scope:**

```json
{
  "error": "forbidden",
  "message": "You don't have permission to perform this action.",
  "request_id": "req_a1b2c3d4e5f6",
  "remediation": "Check your organization and project permissions."
}
```

## Best Practices

### Scope Minimization

Grant only required scopes:

```json
// Read-only automation
{"scopes": ["api:read"]}

// MCP agent only
{"scopes": ["mcp"]}

// Full access (use sparingly)
{"scopes": ["mcp", "api:write"]}
```

### Key Rotation

1. Create new key with same scopes
2. Update integrations to use new key
3. Verify new key works
4. Revoke old key

### Naming Pattern

Use descriptive names:

```
"CI/CD - GitHub Actions"
"Agent - Claude Code"
"Monitoring - Datadog"
```

### Expiration Policy

Set expiration for temporary access:

```json
{
  "name": "Contractor Access",
  "expires_at": "2025-03-31T23:59:59Z"
}
```

## Environment Variables

For CI/CD and automation, use environment variables:

```bash
# GitHub Actions
- name: Query Sibyl
  env:
    SIBYL_API_KEY: ${{ secrets.SIBYL_API_KEY }}
  run: |
    curl -H "Authorization: Bearer $SIBYL_API_KEY" \
      https://api.example.com/api/entities
```

## Related

- [auth-jwt.md](./auth-jwt.md) - JWT session authentication
- [index.md](./index.md) - API overview
