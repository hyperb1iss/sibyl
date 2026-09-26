# Single-Host Deployment (Ansible)

Deploy the full Sibyl stack to one Linux host with the bundled Ansible role. Suited to a personal
instance on a small cloud VM: one box, no Kubernetes, modest cost.

## Architecture

Four long-running containers plus a one-shot volume initializer (`surreal-init`), managed by a
`sibyl.service` systemd unit:

| Container   | Source                                              | Purpose                        |
| ----------- | --------------------------------------------------- | ------------------------------ |
| `surrealdb` | `surrealdb/surrealdb:v3.2.4`                        | Graph, content, and auth store |
| `backend`   | `ghcr.io/hyperb1iss/sibyl-api`                      | FastAPI + MCP server           |
| `frontend`  | `ghcr.io/hyperb1iss/sibyl-web`                      | Next.js web UI                 |
| `caddy`     | built from `caddy:2` with the Cloudflare DNS module | TLS + path routing             |

Caddy obtains a Let's Encrypt certificate over the Cloudflare DNS-01 challenge, so the host needs no
inbound HTTP or HTTPS from the public internet. Pair it with a private network (Tailscale,
WireGuard) and the instance stays unreachable except to you, while still serving a browser-trusted
certificate on a real domain.

The `backend` and `frontend` images are pulled pre-built from the registry, so nothing heavy
compiles on the host.

## The `sibyl` role

`infra/ansible/roles/sibyl/` provisions a host end to end:

- installs Docker Engine and the Compose plugin
- installs `ufw`: default-deny inbound, SSH allowed, HTTP and HTTPS reachable only on the proxy
  interface
- deploys the compose stack and a rendered `.env`
- runs the stack through a `sibyl.service` systemd unit

### Variables

| Variable                    | Default                   | Purpose                                         |
| --------------------------- | ------------------------- | ----------------------------------------------- |
| `sibyl_domain`              | `sibyl.example.com`       | Hostname Caddy serves                           |
| `sibyl_version`             | `1.4.1`                   | ghcr.io image tag                               |
| `sibyl_dir`                 | `/opt/sibyl`              | Deployment directory                            |
| `sibyl_proxy_interface`     | `tailscale0`              | Interface HTTP/HTTPS is exposed on              |
| `sibyl_mcp_auth_mode`       | `auto`                    | MCP bearer-token enforcement                    |
| `sibyl_network_prefix`      | `10.47.213`               | First three octets of the compose network's /24 |
| `sibyl_forwarded_allow_ips` | `10.47.213.10/32` (Caddy) | Proxies trusted to name the client              |

Secrets have no defaults and must be supplied, ideally through ansible-vault: `sibyl_jwt_secret`,
`sibyl_surreal_password`, `sibyl_openai_api_key`, `sibyl_anthropic_api_key`, `sibyl_cf_api_token`.
The role asserts each one is set before doing any work. It wires Anthropic for language models and
OpenAI for embeddings and has no switch for Gemini or Amazon Bedrock yet; the
[environment reference](./environment.md#llm-configuration) lists every provider the server itself
supports.

### Client addresses behind Caddy

Every request reaches the backend from Caddy, so the backend has to trust Caddy to learn who is
actually signing in. Without that trust, all users share one login rate-limit budget (five attempts
per minute) and audit logs record Caddy's address for everyone.

The role pins the compose network's address plan so that Caddy's address is known ahead of time.
`sibyl_network_prefix` names a private /24, `10.47.213.0/24` by default, laid out like this:

- `.1` is the bridge gateway. Every connection from the host itself arrives from this address.
- `.10` is Caddy, pinned.
- `.128` and up go to every other container, the frontend included, as Docker assigns them.

`sibyl_forwarded_allow_ips` renders into `SIBYL_FORWARDED_ALLOW_IPS` and defaults to Caddy's address
alone (`10.47.213.10/32`). Two properties of the stack make that safe:

- Caddy replaces any `X-Forwarded-For` a client sends with the address it saw, so a client cannot
  spoof its way past it.
- Nothing else can hold Caddy's address. The backend publishes no host port, the other containers
  draw from `.128` up, and anything running on the host (a local user, a container with
  `network_mode: host`, an `ssh -L` tunnel over the tailnet) reaches the backend from the gateway,
  which stays untrusted.

Do not widen the list to the network's subnet or to Docker's address pools. The gateway sits inside
both, so every process on the host would be trusted and could name any client address it liked: a
fresh rate-limit bucket for every login attempt, and a pass through the break-glass allowlist. Local
login has no account lockout, so the per-address limit is the only brute-force control.

Choose another `sibyl_network_prefix` if `10.47.213.0/24` collides with a host route, a VPN, or
another Docker network; Docker refuses to create the network when the subnet overlaps one it already
manages. The next restart recreates `sibyl-network` with the new plan. Set
`sibyl_forwarded_allow_ips` to an empty string to turn proxy trust off (loopback only). If you add
`trusted_proxies` to Caddy for a proxy in front of it, Caddy appends to `X-Forwarded-For` instead of
replacing it, so list that proxy's address as well. The
[environment reference](./environment.md#trusted-proxies) explains how the address resolves.

## Deploying

The role is provider-agnostic: any Ubuntu host reachable over SSH works.

### Prerequisites

The role is not published to Ansible Galaxy, so two things must be in place before the playbook runs
end to end:

- **`roles_path`**: your playbook has to resolve the `sibyl` role from a checkout of this
  repository. Point `ansible.cfg` at it:

  ```ini
  [defaults]
  roles_path = roles:/path/to/sibyl/infra/ansible/roles
  ```

- **Secrets**: the five vault variables listed under [Variables](#variables) have no defaults. The
  role asserts each one before it touches the host, so an unset secret aborts the run cleanly
  instead of leaving a half-built stack.

### 1. Inventory

Add the host to an inventory group:

```yaml
cloud:
  hosts:
    my-host:
      ansible_host: 203.0.113.10
      ansible_user: deploy
```

### 2. Bootstrap (once)

A fresh cloud host usually permits only `root`. Run a one-time play that creates your admin user and
authorizes its SSH key, so later runs connect as that user and host hardening can disable root SSH.

### 3. Provision

Apply the role, alongside a base-hardening role and optionally a Tailscale role, from your playbook:

```bash
ansible-playbook site.yml --limit my-host
```

The role is idempotent; re-run it to roll out config or image changes.

## Verifying

```bash
# on the host
systemctl status sibyl
docker compose --env-file /opt/sibyl/.env -f /opt/sibyl/docker-compose.yml ps
curl -sf https://<sibyl_domain>/api/health
```

## Operations

- **Logs:** `docker compose --env-file /opt/sibyl/.env -f /opt/sibyl/docker-compose.yml logs -f`
- **Update:** bump `sibyl_version` and re-run the role
- **Backups:** schedule `surreal export` against the running stack and copy the dump off-host. The
  graph is your memory; back it up.

## Reference

This project is deployed from a separate homelab Ansible repository that supplies the inventory, the
bootstrap play, ansible-vault secrets, and a Tailscale role. It is a worked example of the layout
above.
