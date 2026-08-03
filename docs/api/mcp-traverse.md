# MCP Tools: expand_neighbors and fetch_slice

Bounded traversal verbs for an agent that steers its own retrieval. `expand_neighbors` widens a set
of known memories into their graph neighborhood; `fetch_slice` widens one memory into the span
window it was cut into.

## The traversal contract

These verbs are steps in a loop of **at most three rounds**:

1. `search` or `context` finds a starting set.
2. `expand_neighbors` or `fetch_slice` widens it.
3. One more widening round, then answer.

Two retrieval iterations capture nearly all of the available gain, so a fourth round costs latency
and buys noise. The verbs are stateless and bounded per call; the round budget lives in the tool
docstrings rather than in server state, because there is no session to enforce it in.

**Skip traversal entirely when one hop answers the question.** For "what do we know about X", call
[`context`](./mcp-context.md) and read the pack it composed. Traversal is for when you hold specific
memories and need what sits next to them: the tasks blocking this one, the decision a plan
supersedes, the spans of a memory a search only matched part of.

**Composition is not yours.** Both verbs return adjacency and previews so you can choose what to
gather. `context` still renders the evidence and keeps control of ordering and the reserved note
lane.

## Authorization

Every row either verb returns is authorized for the calling reader individually, not once per seed.
Project membership does not authorize a private memory that happens to sit in that project, so a
neighbor missing from a result may exist and belong to someone else.

A seed that is absent and a seed the reader may not see are reported identically, and `fetch_slice`
raises the same 404 for a denied entity as for one that does not exist. Distinguishing them would
confirm the existence of a row the caller has no right to know about.

## expand_neighbors

### Input

```typescript
interface ExpandNeighborsInput {
  entity_ids: string[]; // Seeds, at most 8
  relationship_types?: string[]; // Restrict hops, e.g. ["DEPENDS_ON"]
  types?: string[]; // Restrict neighbors to these entity types
  depth?: number; // 1-3, default 1
  limit?: number; // 1-24, default 8
  content_max_chars?: number; // Preview characters per neighbor, default 500
  include_incoming?: boolean; // default true
  project?: string; // Scope the walk to one readable project
}
```

`include_incoming` defaults to true because the interesting neighbors are usually on that side. The
graph writes edges from span to memory and from dependent to dependency, so an outgoing-only walk
reports those neighbors as absent.

### Output

```typescript
interface ExpandNeighborsOutput {
  origins: string[]; // Seeds that resolved for this reader
  unresolved: string[]; // Seeds that did not, without saying why
  neighbors: {
    id: string;
    type: string;
    name: string;
    relationship: string; // Edge name, or SHARES_COMMUNITY / MENTIONS
    direction: "outgoing" | "incoming";
    distance: number; // Hop count from the nearest seed
    score: number; // Relationship weight with depth decay
    content: string; // Preview, bounded by content_max_chars
    project_id: string | null;
    metadata: Record<string, unknown>;
  }[];
  total: number;
  depth: number;
  limit: number;
  truncated: boolean; // More neighbors existed than the limit returned
  filters: Record<string, unknown>;
}
```

A neighbor that is itself a span carries `passage_index`, `parent_entity_id`, and
`widen_with: "fetch_slice"`, naming the next move rather than making the caller infer it.

### Examples

```python
expand_neighbors(["task_abc"], relationship_types=["DEPENDS_ON"])
expand_neighbors(["decision_1", "decision_2"], depth=2, limit=16)
expand_neighbors(["artifact_abc"], relationship_types=["PART_OF"])  # its spans
```

## fetch_slice

### Input

```typescript
interface FetchSliceInput {
  entity_id: string; // A passage ID, or the memory it was cut from
  window?: number; // 1-64, default 3
  content_max_chars?: number; // Budget for the whole window, default 18000
  project?: string; // Scope the read to one readable project
}
```

The default window of three is the measured adjacency: three adjacent spans reach the same answer
exposure as sending the whole memory, where a lone span reaches noticeably less.

Given a passage, the window centers on it. Given a memory, the window starts at its first span.

### Output

```typescript
interface FetchSliceOutput {
  entity_id: string; // What was asked for
  parent_id: string; // What a citation resolves to
  parent_name: string;
  parent_type: string;
  passages: {
    id: string;
    name: string;
    content: string;
    passage_index: number | null;
    passage_total: number | null;
    breadcrumb: string | null;
    truncated: boolean;
  }[];
  window: number;
  sliced: boolean; // false when the memory was never cut into spans
  total: number;
  window_start: number | null;
  passage_total: number | null;
  covers_parent: boolean; // Whether these spans account for the whole body
  project_id: string | null;
  content_chars: number;
}
```

**Cite `parent_id`, not a span id.** Spans are re-minted whenever their memory is edited, so a
citation pointing at one goes stale silently. `covers_parent` says whether the returned spans
account for the whole body; when it is false, text lives only on the parent.

A memory short enough never to have been cut comes back whole with `sliced: false`. That is the
answer, not an error to retry.

### Examples

```python
fetch_slice("passage_9f2c1b")            # window centered on this span
fetch_slice("decision_abc", window=5)    # first five spans of this memory
```

## REST equivalents

| Verb               | Endpoint                  |
| ------------------ | ------------------------- |
| `expand_neighbors` | `POST /api/search/expand` |
| `fetch_slice`      | `POST /api/search/slice`  |

Both accept the same fields as the MCP tools and resolve the caller's readable projects from their
session before the walk. See [rest-search.md](./rest-search.md) for the shared search surface.
