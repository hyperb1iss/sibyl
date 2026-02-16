---
title: Sandbox Architecture
description: Isolated execution environments for AI agents
---

# Sandbox Architecture

Sibyl's sandbox system provides isolated, ephemeral execution environments for AI agents. The
architecture splits into two planes: a **control plane** (the API server) that manages sandbox
lifecycle and task routing, and an **execution plane** (runner daemons) that actually runs tasks
inside sandboxed environments. Runtime lifecycle is delegated to the
`kubernetes-sigs/agent-sandbox` `Sandbox` CRD.

## Architecture Overview

```
┌─────────────────────────────────────────┐
│           Sibyl API Server              │
│  ┌──────────────┐ ┌──────────────────┐  │
│  │ Controller   │ │   Dispatcher      │  │
│  │ (CRD client) │ │  (task queue)     │  │
│  └──────┬───────┘ └────────┬─────────┘  │
│         │                  │            │
│  ┌──────┴──────────────────┴─────────┐  │
│  │      WebSocket Protocol           │  │
│  └──────────────┬────────────────────┘  │
└─────────────────┼───────────────────────┘
                  │
     ┌────────────┼──────────────┐
     │            │              │
 ┌───┴───┐  ┌───┴───┐     ┌────────────────┐
 │Runner │  │Runner │ ... │ agent-sandbox  │
 │(pod)  │  │(pod)  │     │ controller/CRD │
 └───────┘  └───────┘     └────────────────┘
```

- **Controller** manages sandbox lifecycle by creating/updating/deleting `agents.x-k8s.io/v1alpha1`
  `Sandbox` resources.
- **Dispatcher** routes tasks to runners based on availability, capabilities, and warm worktree
  proximity.
- **WebSocket Protocol** provides bidirectional communication between server and runners.
- **Runners** are stateless daemons that register with the server and execute assigned tasks.

## Compute Tiers

Sibyl supports multiple isolation levels, chosen per deployment:

| Tier       | Isolation       | Use Case                  |
| ---------- | --------------- | ------------------------- |
| Local      | Process-level   | Development, testing      |
| Docker     | Container-level | CI/CD, staging            |
| Kubernetes | Pod-level       | Production                |
| vCluster   | Cluster-level   | Multi-tenant production   |

Higher tiers provide stronger isolation at the cost of provisioning latency.

## BYOD Model

Sibyl uses a **Bring Your Own Device** model for runner infrastructure. Runners self-register with
the API server, declaring:

- **Capabilities** — What the runner can do (e.g., `docker`, `gpu`, `high-memory`)
- **Project affinity** — Which projects have warm worktrees on this runner

The task router scores candidate runners on three axes:

1. **Availability** — Is the runner idle or near capacity?
2. **Capability match** — Does the runner have the required capabilities?
3. **Warm worktree proximity** — Does the runner already have the project cloned and ready?

This scoring model minimizes cold-start time by preferring runners that already have the right
environment cached.

## Runner Daemon Protocol

Communication between the API server and runners uses a WebSocket-based bidirectional protocol.

### Server-to-Runner Messages

| Message        | Description                              |
| -------------- | ---------------------------------------- |
| `heartbeat`    | Periodic liveness check                  |
| `task_assign`  | Assign a queued task to this runner      |
| `task_cancel`  | Cancel a running task                    |

### Runner-to-Server Messages

| Message            | Description                                     |
| ------------------ | ----------------------------------------------- |
| `heartbeat_ack`    | Acknowledge liveness check                      |
| `status`           | Report runner load, capabilities, health        |
| `task_ack`         | Confirm task assignment accepted                |
| `task_complete`    | Report task finished (with result/artifacts)     |
| `task_reject`      | Decline task assignment (capacity, mismatch)     |
| `agent_update`     | Stream agent progress/logs during execution      |
| `project_register` | Register or update project affinity              |

The protocol is designed to be resilient to transient disconnects. Runners automatically reconnect
and re-register on connection loss. Tasks that were in-flight during a disconnect enter a grace
period before being reassigned.

## Sandbox Lifecycle

```
pending → starting → running → suspending → suspended → deleted
                        │                       │
                        └── failed ◄────────────┘
```

| State       | Description                                              |
| ----------- | -------------------------------------------------------- |
| `pending`   | Sandbox requested, waiting for resources                 |
| `starting`  | Provisioning environment (pulling images, cloning repos) |
| `running`   | Active and accepting tasks                               |
| `suspending`| Saving state before suspension                           |
| `suspended` | Idle, state preserved, resources released                |
| `deleted`   | Cleaned up, resources freed                              |
| `failed`    | Error state, reachable from any other state              |

Sandboxes auto-suspend after `SIBYL_SANDBOX_IDLE_TTL_SECONDS` of inactivity and are hard-deleted
after `SIBYL_SANDBOX_MAX_LIFETIME_SECONDS`. Suspend/resume maps to
`Sandbox.spec.replicas = 0|1` in the agent-sandbox CRD.

## Task Lifecycle

```
queued → dispatched → acked → running → completed
                                  │
                                  ├── failed → retry → queued
                                  └── canceled
```

| State        | Description                                        |
| ------------ | -------------------------------------------------- |
| `queued`     | Task submitted, waiting for runner assignment      |
| `dispatched` | Assigned to a runner, awaiting acknowledgment      |
| `acked`      | Runner confirmed receipt                           |
| `running`    | Actively executing                                 |
| `completed`  | Finished successfully                              |
| `failed`     | Execution error (may retry)                        |
| `retry`      | Scheduled for re-queue after failure               |
| `canceled`   | Explicitly canceled by user or system              |

Failed tasks are retried up to a configurable limit before being marked as permanently failed.

## Auth Model

Sandbox runners authenticate via JWT tokens with specific claims:

| Claim | Description                              |
| ----- | ---------------------------------------- |
| `org` | Organization ID (tenant isolation)       |
| `sub` | Subject (user or service account)        |
| `rid` | Runner ID                                |
| `sid` | Sandbox ID (if bound to a sandbox)       |
| `scp` | Scope — must include `sandbox:runner`    |

**Strict binding** is enforced for sandbox-bound runners: a runner token with a `sid` claim can only
execute tasks within that specific sandbox. This prevents a compromised runner from accessing other
sandboxes in the same organization.

## Configuration Reference

All sandbox configuration uses the `SIBYL_SANDBOX_` prefix:

| Variable                              | Default                                    | Description                            |
| ------------------------------------- | ------------------------------------------ | -------------------------------------- |
| `SIBYL_SANDBOX_MODE`                  | `off`                                      | Policy: `off`, `shadow`, `enforced`    |
| `SIBYL_SANDBOX_DEFAULT_IMAGE`         | `ghcr.io/hyperb1iss/sibyl-sandbox:latest`  | Default container image for sandboxes  |
| `SIBYL_SANDBOX_WORKTREE_BASE`         | `/tmp/sibyl/sandboxes`                     | Base path mounted for sandbox worktrees |
| `SIBYL_SANDBOX_IDLE_TTL_SECONDS`      | `1800`                                     | Auto-suspend after idle (seconds)      |
| `SIBYL_SANDBOX_MAX_LIFETIME_SECONDS`  | `14400`                                    | Maximum sandbox lifetime (seconds)     |
| `SIBYL_SANDBOX_K8S_NAMESPACE`         | `default`                                  | Kubernetes namespace for sandbox pods  |
| `SIBYL_SANDBOX_RECONCILE_ENABLED`     | `true`                                     | Enable background reconciliation loop  |

## Deployment Modes

The `SIBYL_SANDBOX_MODE` variable controls how sandboxes are enforced:

### `off` (Default)

Sandbox system is completely disabled. Tasks execute directly without isolation. Suitable for
single-user development or when external orchestration handles isolation.

### `shadow`

Sandbox operations are observed and logged but not enforced. Tasks can execute without a sandbox,
but the system tracks what _would_ have been sandboxed. Useful for:

- Validating sandbox configuration before enforcement
- Monitoring task patterns to tune runner capacity
- Gradual rollout of sandbox requirements

### `enforced`

All task execution requires a sandbox. Tasks submitted without a valid sandbox assignment are
rejected. This is the recommended mode for production deployments where isolation guarantees matter.

## Related Documentation

- [Orchestrator Architecture](./orchestrator-architecture.md) — Higher-level agent orchestration
- [Agent Harness Vision](./agent-harness-vision.md) — Autonomous agent execution model
- [Installation Guide](../guide/installation.md) — Sandbox setup instructions
