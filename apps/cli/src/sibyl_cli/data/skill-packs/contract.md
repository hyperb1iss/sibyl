# Sibyl Agent Contract

Sibyl is durable project memory and task coordination. Start every session with this card. Load the
full pack only when the work is about to write or mutate state:

```bash
sibyl skill get contract
sibyl skill get core  # before the first remember, correct, or task mutation
```

## Five verbs

```bash
# Read one agent-ready context pack before non-trivial work
sibyl context "<goal>" --intent build

# Store a durable fact, decision, procedure, plan, or learning
sibyl remember "<title>" "<body>" --kind decision

# Inspect a memory, then correct it with an explicit reason
sibyl correct <raw-memory-id>
sibyl correct <raw-memory-id> --action stale --reason "<why>"

# Read and mutate the task tree
sibyl task list --status doing,blocked
sibyl task start <id>
sibyl task complete <id> --learnings "<durable result>"

# Check reachability and runtime health
sibyl health
```

`search`, `recall`, `add`, and `blame` are compatibility aliases. Use the five verbs above in new
agent instructions. Human and operator surfaces live outside this compact contract.

## Small enums

- Intents: `build`, `plan`, `review`, `debug`, `general`.
- Kinds: `episode`, `decision`, `procedure`, `error_pattern`, `rule`, `plan`, `idea`, `claim`,
  `artifact`, `session`, `note`.
- Scopes: `private`, `project`, `team`, `org`. Scope policy still gates every read, write, and
  promotion; naming a scope never grants access.
- Epistemic basis: `observed`, `inferred`, `told`, `assumed`.

Useful write flags:

- `--pin` preserves a memory from ordinary decay.
- `--basis <value>` records how the claim was learned.
- `--propose-scope team` nominates a capture for audited promotion; it does not share immediately.
- `sibyl cite <id...> --misled` records negative feedback only when memory materially led the answer
  astray. Irrelevant or unused context is not misleading.

## Five hard rules

1. Context before action. Run `sibyl context` before non-trivial work and use returned IDs when you
   need to inspect or correct a specific memory.
2. Load `sibyl skill get core` before the first write intent. The full pack carries current flags,
   receipts, task transitions, correction actions, and conflict handling.
3. Raw memory is law. New knowledge goes through `sibyl remember`, even when the hidden `add` alias
   would work. This preserves verbatim provenance before graph projection.
4. Feedback must be material. Positive citation means the memory shaped the result. `--misled`
   means it shaped the result incorrectly. Mere exposure earns neither signal.
5. Trust receipts, not narration. A write is complete only when its mutation receipt says it was
   applied. On `not_found` or revision conflict, re-list or inspect current state before deciding;
   never blind-retry a stale mutation.

Keep durable learnings specific: what happened, why, the verified solution, and the caveat that
would save the next agent from rediscovery.
