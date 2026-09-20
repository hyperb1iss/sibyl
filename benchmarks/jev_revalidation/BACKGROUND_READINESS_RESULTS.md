# Background Jev readiness experiment

An ideal background Jev result improves modeled median validation latency by only
4.88% on the frozen quality-speed corpus. The same routing rule cannot reach the
original 20% median target on these samples, even with every answer ready and zero
lookup overhead. The ideal modeled cost falls 30.68%, but p95 does not improve.

The experiment ran offline on `stef-gradial-com-main`, validating the complete
[earlier quality-speed archive](results/quality-speed-2026-09-20/protocol.json)
before reusing any measurement. It made no new provider calls. The cohort contains
32 synthetic cases with two dependent repeats per arm; the conclusion concerns
these samples and the unchanged routing rule.

## Results

The direct critic baseline has 64 paths, a 4,463.89 ms median, a 6,931.71 ms p95,
and a measured total cost of $1.773265. The following costs are modeled from those
receipts, including every background Jev call, whether used or late.

| Assumed useful lead | Ready answers | Critic bypasses | Foreground median lower bound | Foreground p95 lower bound | Modeled cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0 ms | 0/64 | 0/64 | 4,463.89 ms | 6,931.71 ms | $1.779389 |
| 100 ms | 5/64 | 1/64 | 4,463.89 ms | 6,931.71 ms | $1.764764 |
| 150 ms | 29/64 | 11/64 | 4,261.79 ms | 6,931.71 ms | $1.558264 |
| 200 ms | 51/64 | 18/64 | 4,245.97 ms | 6,931.71 ms | $1.359004 |
| 250 ms | 60/64 | 19/64 | 4,245.97 ms | 6,931.71 ms | $1.300484 |
| 1,000 ms | 64/64 | 21/64 | 4,245.97 ms | 6,931.71 ms | $1.229164 |
| All ready, ideal ceiling | 64/64 | 21/64 | 4,245.97 ms | 6,931.71 ms | $1.229164 |

The assumed lead represents work already required by the caller. It is not an
instruction to sleep or delay validation. No actual overlap duration was measured.
The full analysis also includes 50 ms and 500 ms scenarios.

The 21 bypass-eligible paths already have a relatively fast direct-critic median
of 1,813.41 ms. The remaining 43 paths have a direct-critic median of 4,889.90 ms.
Removing the cheaper paths leaves the slower critic work determining overall
median and tail latency.

The retained critic receipts also show a median of 34 output tokens for the
bypass-eligible paths versus 341 for the remaining paths. Median input lengths
are similar (2,777 versus 2,696 tokens). Longer critiques are a plausible speed
bottleneck to investigate, but this observational split does not establish that
output length causes the latency difference.

Every scenario retains 24 supported accepts, zero unsafe accepts, 64/64 rubric
matches and 52/64 strict action matches. Those are conditional action counts,
not fresh quality observations. Earlier semantic review found errors in individual
critic findings despite action-level correctness. The replay does not test reader
answers, retrieval quality or general equivalence to the complete critic.

## Method and accounting

The runner first replays all 128 original paths. It checks the frozen inputs,
program hashes, exact request bodies, raw provider responses, dispatch records,
artifact coverage, derived path rows and original summary. Both arms must contain
identical critic requests for each paired case and repeat. Missing costs, malformed
timing and incomplete archives fail before creating an output directory.

Each scenario probes once at its assumed consumer boundary. A pending, absent,
invalid, stale or unauthorized observation cannot produce a hit. The separate
async regression proves that a pending task neither delays nor cancels the direct
critic. A later answer cannot change the selected foreground action.

A ready answer still needs the original source-support routing rule: grouped
questions, confidence threshold 0.99 and the numeric-text guard. A miss or a
ready answer that cannot bypass uses its paired **baseline** critic sample. Reusing
the old sequential fallback sample would mix in a different stochastic critic run.

The readiness proxy includes observed Jev preparation, transport and
interpretation time. It does not measure protected receipt completion, current
source checks, authorization lookup, scheduler delay or receipt lookup. Scenarios
assume unchanged requests and currently valid authority. Exact request and semantic
hash binding are tested; production freshness is not established by that test.

The report separates three timing boundaries:

- Foreground time starts at the probe. A bypass is assigned zero overhead, so this
  is an optimistic lower bound.
- Candidate-origin time includes the assumed lead in both the experimental arm
  and its direct-critic baseline.
- All-work completion includes the later of foreground completion and background
  completion. Every dispatched Jev call remains in cost accounting.

The ideal scenario has no invented finite lead and leaves candidate-origin and
all-work completion times unset. The original acquisition used 171 provider calls
and cost $3.009604356 across both arms. That historical acquisition cost is separate
from the modeled cost of selecting one policy for 64 paths.

## Where the product could overlap work

The [dream job](../../apps/api/src/sibyl/jobs/reflection.py) finishes source
reflection before draining candidates. Each ordinary cohort can persist a
candidate while later cohorts are still being processed. A freshly authorized
observation after persistence could overlap that remaining work. A single candidate,
the last candidate or a drain-only resume may have no useful lead.

Launching before persistence does not fit the current receipt contract. The
protected receipt needs a persisted parent, and stored-candidate reconstruction
changes the candidate view used to bind the decision request. The consumer must
reuse an exactly matching completed receipt and retain publication freshness checks.

The existing experimental shadow starts beside the critic and awaits its task
before returning success. That await can extend the tail. A future background
integration needs an explicit task owner and a single readiness probe at the
critic boundary; detaching an unowned task would not establish correct accounting
or durable completion.

The next speed investigation should profile the 43 retained critic paths and the
actual candidate-persisted-to-consumer window. Background Jev remains a possible
cost optimization, but this replay does not justify enabling critic bypass or
paying for another identical latency comparison.

## Reproduce and inspect

Run from this stack's checkout, with the original immutable archive available:

```sh
moon run root:jev-background-readiness -- \
  --cases benchmarks/jev_revalidation/quality_speed_cases.json \
  --archive /absolute/path/to/jev-quality-speed-20260920/live \
  --output-dir /absolute/path/to/new-background-analysis
```

The task has no live mode. It writes the complete per-path analysis and a completion
record binding that analysis by SHA-256. The committed
[summary](results/background-readiness-2026-09-20/summary.json) retains all aggregate
and per-repeat metrics, input provenance and hashes. It omits only scenario rows
and the repeated original-file inventory; the full retained analysis contains both.

The authoritative execution output is
`/home/dev/dev/eval-runs/jev-background-readiness-20260920-final` on the devbox.
The original archive remains under
`/home/dev/dev/eval-runs/jev-quality-speed-20260920/live`.

The devbox passed the benchmark suite (302 tests), lint and type checking.
The suite includes real async readiness tests and mocked-transport archive replay;
these checks do not exercise a deployed background scheduler.
