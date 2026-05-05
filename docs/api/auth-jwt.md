# Authentication: JWT Sessions

JWT-based authentication for web clients and browser-based applications.

## Overview

Sibyl uses JWT (JSON Web Tokens) for session authentication:

- **Access Tokens**: Short-lived (default 60 minutes), used for API authentication
- **Refresh Tokens**: Long-lived (default 30 days), used to obtain new access tokens
- **OAuth Support**: GitHub OAuth integration for social login

## Token Types

### Access Token

Short-lived token for API authentication.

**Claims Schema:**

```json
{
  "sub": "user_uuid", // User ID
  "org": "org_uuid", // Organization ID (optional)
  "typ": "access", // Token type
  "iat": 1704067200, // Issued at (Unix timestamp)
  "exp": 1704070800 // Expires at (Unix timestamp)
}
```

**Default Expiry:** 60 minutes (configurable via `SIBYL_ACCESS_TOKEN_EXPIRE_MINUTES`)

### Refresh Token

Long-lived token for obtaining new access tokens.

**Claims Schema:**

```json
{
  "sub": "user_uuid", // User ID
  "org": "org_uuid", // Organization ID (optional)
  "sid": "session_uuid", // Session ID (for token rotation)
  "typ": "refresh", // Token type
  "jti": "unique_token_id", // Unique ID for revocation
  "iat": 1704067200, // Issued at
  "exp": 1706659200 // Expires at
}
```

**Default Expiry:** 30 days (configurable via `SIBYL_REFRESH_TOKEN_EXPIRE_DAYS`)

## Configuration

### Required

```bash
SIBYL_JWT_SECRET=your-secure-secret-key-at-least-32-chars
```

### Optional

```bash
SIBYL_JWT_ALGORITHM=HS256                    # Default: HS256
SIBYL_ACCESS_TOKEN_EXPIRE_MINUTES=60         # Default: 60
SIBYL_REFRESH_TOKEN_EXPIRE_DAYS=30           # Default: 30
```

## Authentication Methods

### Cookie-Based (Recommended for Web)

Access token is stored in an HTTP-only cookie:

```
Cookie: sibyl_access_token=eyJhbGciOiJIUzI1NiIs...
```

**Advantages:**

- Automatic CSRF protection (SameSite=Lax)
- No client-side token storage
- Works with browser redirect flows

### Header-Based

Access token passed via Authorization header:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
```

**Use Cases:**

- API clients
- CLI tools
- Mobile apps

## Auth Endpoints

### Local Signup

```http
POST /api/auth/local/signup
```

**Request:**

```json
{
  "email": "user@example.com",
  "password": "secure-password",
  "name": "User Name"
}
```

**Response:**

```json
{
  "user": {
    "id": "user_uuid",
    "email": "user@example.com",
    "name": "User Name"
  },
  "organization": {
    "id": "org_uuid",
    "name": "My Org",
    "slug": "my-org"
  },
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_in": 3600
}
```

### Local Login

```http
POST /api/auth/local/login
```

**Request:**

```json
{
  "email": "user@example.com",
  "password": "secure-password"
}
```

**Response:**

```json
{
  "user": {
    "id": "user_uuid",
    "email": "user@example.com",
    "name": "User Name"
  },
  "organization": {
    "id": "org_uuid",
    "name": "My Org",
    "slug": "my-org"
  },
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_in": 3600
}
```

Also sets `sibyl_access_token` cookie for web clients.

### GitHub OAuth

#### Start OAuth Flow

```http
GET /api/auth/github
```

Redirects to GitHub OAuth consent screen.

**Query Parameters:**

| Parameter      | Type   | Description             |
| -------------- | ------ | ----------------------- |
| `redirect_uri` | string | Post-login redirect URL |

#### OAuth Callback

```http
GET /api/auth/github/callback
```

Handles GitHub OAuth callback, creates/links user account.

**Query Parameters:**

| Parameter | Type   | Description              |
| --------- | ------ | ------------------------ |
| `code`    | string | OAuth authorization code |
| `state`   | string | CSRF state token         |

**Response:** Redirects to `SIBYL_FRONTEND_URL` with tokens set.

### Logout

```http
POST /api/auth/logout
```

Clears session and invalidates tokens.

**Response:** `204 No Content`

Also clears `sibyl_access_token` cookie.

### Current User

```http
GET /api/auth/me
```

Returns current authenticated user.

**Response:**

```json
{
  "user": {
    "id": "user_uuid",
    "github_id": 12345,
    "email": "user@example.com",
    "name": "User Name",
    "avatar_url": "https://avatars.githubusercontent.com/u/12345",
    "is_admin": false
  },
  "organization": {
    "id": "org_uuid",
    "name": "My Org",
    "slug": "my-org"
  },
  "org_role": "owner"
}
```

### Token Refresh

```http
POST /api/auth/refresh
```

Exchange refresh token for new access token.

**Request:**

```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

**Response:**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

## Token Validation

### Validation Flow

1. Extract token from cookie or Authorization header
2. Verify signature using `SIBYL_JWT_SECRET`
3. Check expiration (`exp` claim)
4. Validate token type (`typ` claim)
5. Load user from `sub` claim
6. Load organization from `org` claim

### Validation Errors

| Error                     | HTTP Status | Cause                         |
| ------------------------- | ----------- | ----------------------------- |
| `Not authenticated`       | 401         | Missing token                 |
| `Invalid token`           | 401         | Signature verification failed |
| `Token expired`           | 401         | Token past expiration         |
| `User not found`          | 401         | User ID not in database       |
| `No organization context` | 403         | Token missing org claim       |

## Organization Context

JWT tokens include organization context:

```json
{
  "sub": "user_uuid",
  "org": "org_uuid"
}
```

All API operations are scoped to this organization:

- Graph queries use org-specific SurrealDB namespaces
- Document queries filter by org ownership
- Resource access is validated against org membership

### Switching Organizations

Switch the active organization for the current session:

```
POST /api/orgs/{slug}/switch
```

Returns rotated access and refresh tokens scoped to the target organization. The user must be a
member of that organization.

## Security Considerations

### Token Storage

**Web Applications:**

- Store in HTTP-only cookies (Sibyl sets this automatically)
- Never store in localStorage (XSS vulnerable)

**Native Applications:**

- Use secure storage (Keychain, Keystore)
- Encrypt tokens at rest

### Token Rotation

Refresh tokens support rotation:

1. Use refresh token to get new access token
2. Server may issue new refresh token
3. Old refresh token is invalidated

### Revocation

Tokens can be revoked by:

- Logout (clears session)
- Password change (invalidates all tokens)
- Admin action

## MCP Authentication

For MCP endpoints, authentication follows the same pattern:

```bash
curl -X POST /mcp \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/call", ...}'
```

MCP auth mode is configurable:

```bash
SIBYL_MCP_AUTH_MODE=auto  # auto, on, or off
```

- `auto`: Enforce auth when `SIBYL_JWT_SECRET` is set
- `on`: Always require auth
- `off`: Disable auth (development only)

## Error Responses

```json
{
  "detail": "Not authenticated"
}
```

| Status | Error                     | Resolution                         |
| ------ | ------------------------- | ---------------------------------- |
| 401    | `Not authenticated`       | Provide valid token                |
| 401    | `Invalid token`           | Token may be corrupted or tampered |
| 401    | `Token expired`           | Refresh token or re-login          |
| 401    | `User not found`          | Account may be deleted             |
| 403    | `No organization context` | Token missing org claim            |
| 403    | `Forbidden`               | Insufficient role permissions      |

## Related

- [auth-api-keys.md](./auth-api-keys.md) - API key authentication
- [index.md](./index.md) - API overview
