---
title: Bot Accounts
description: Giving an agent its own identity, capped by the person who sponsors it
---

# Bot Accounts

A bot account is an identity for an agent, such as an on-call bot or a CI job, so it stops running
on a person's API key. A bot is a member of your organization that you sponsor. It writes as itself,
it lives and dies with its own keys, and it never reads your private memories.

## Why Not Use My Key

An agent running on your API key is you, as far as Sibyl can tell:

- Its writes are credited to you on the Team page and in the audit log.
- Its access ends when your account does, or lingers on a key nobody owns.
- A key narrowed only by project still reads your private memories.

A bot fixes all three. Its writes carry its own name, its keys belong to it, and its private scope
is its own.

## The Rule

A bot never holds more on a project than its sponsor does, and never more than contributor.

On every request, a bot's role on a project is the lowest of:

1. The role the bot was granted on that project.
2. Its sponsor's current role on that project.
3. Contributor.

An org-visible project's default role counts as a grant, so a bot reads org-visible projects like
any member does, capped the same way. The cap is read on each request, not when the bot was granted,
so if your role on a project drops, your bot's drops on its next call.

The same holds in the organization. A bot is never more than a member, and never more than you are
there right now: if you become an org viewer, your bot becomes one on its next request, writes
nowhere, and you can neither grant it access nor issue it keys until your role comes back. If you
leave the organization, every key your bot holds is refused until an org admin transfers the bot to
someone else.

## Who Can Do What

| Action                                  | Who                                                                   |
| --------------------------------------- | --------------------------------------------------------------------- |
| Create a bot                            | Any owner, admin or member, sponsoring it themselves (not a viewer)   |
| Grant a bot project access              | Its sponsor, up to min(their own role on the project, contributor)    |
| Issue a bot's keys                      | Its sponsor only                                                      |
| Remove a bot's project access           | Its sponsor or an org admin                                           |
| Revoke a bot's keys                     | Its sponsor or an org admin                                           |
| List bots                               | An org admin sees every bot; anyone else sees the bots they sponsor   |
| Transfer a bot to a new sponsor         | Org admins only                                                       |
| Delete a bot                            | Its sponsor or an org admin; its keys stop working at once            |
| Add a bot through project member routes | A project owner or maintainer, at contributor or viewer, still capped |

Granting your own bot access is the one way to add a project member without being that project's
owner or maintainer, and it reaches only bots you sponsor. A sponsor who is an org viewer can do
neither.

Granting access and issuing keys both widen what a bot can do, so only its sponsor does them. An org
admin who needs to transfers the bot to themselves first, which is recorded in the audit log. A key
is the bot's whole identity, private memory included, so it belongs with the person who answers for
the bot.

## What A Bot Cannot Do

- Sign in. It has no password and no single sign-on identity; it authenticates only with API keys.
- Hold admin or owner in the organization, or maintainer or owner on a project. Every role route
  refuses those roles for a bot.
- Join a team or a memory space, or any organization other than the one it was made in.
- Own an organization, including a personal one, or act above member in one.
- Create projects, organizations, API keys or other bots, or approve a device sign-in.

## Set One Up

Create the bot, give it a project, and issue it a key:

```bash
sibyl bot create oncall
sibyl bot grant oncall project_abc123 --role contributor
sibyl bot key create oncall --name pager --scopes mcp,api:read,api:write
```

The key prints once. Store it where the agent reads its secrets, then run the agent with it:

```bash
export SIBYL_API_URL=https://sibyl.example.com/api
export SIBYL_AUTH_TOKEN=sk_live_...
sibyl remember "Paged for disk pressure" "Rotated logs on db-2" --project project_abc123
```

Captures land in the project's default scope, the same as anyone's. A project whose default is
`private` keeps the bot's captures to the bot; set the project to `project` scope with
`sibyl project update <project> --default-scope project` if the team should read them.

Keys narrow and expire the same way user keys do: `--projects`, `--memory-spaces` and
`--expires-days` all apply.

## Attribution

Everything a bot writes is credited to the bot. The Team page lists it beside people with a Bot
label, on its card and on each update it made. The audit log marks each event's actor as a person or
a bot, and every change a person makes to a bot (creation, grants, transfers, key issuance and
deletion) is recorded under that person.

## When A Sponsor Leaves

A bot whose sponsor is deleted or removed from the organization is suspended: its keys are refused
with `bot_suspended`. Nothing is lost. An org admin transfers it to a new sponsor and the same keys
work again, capped by the new sponsor's roles:

```bash
sibyl bot list
sibyl bot transfer oncall --to <user-id>
```

A bot removed from the organization through the member list, rather than deleted, is suspended the
same way: none of its keys resolve anywhere.

## Deleting A Bot

`sibyl bot delete oncall` revokes every key the bot holds, removes it from the organization and its
projects, and schedules its private memories for purge after 30 days, the same as a deleted account.
