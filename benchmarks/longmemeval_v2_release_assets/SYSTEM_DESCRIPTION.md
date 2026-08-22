# Sibyl live API memory for LongMemEval-V2

Sibyl is a SurrealDB-native memory system. The LongMemEval-V2 adapter writes
trajectory-derived memory through Sibyl's live API, then queries the same API
for compact context before the official fixed reader answers each question.

Each sealed arm has a distinct execution identity. Web and enterprise use
separate memory roots. An arm reuses saved memory only when its complete
manifest, deterministic chunk catalog, action spines, dataset identity, and
configuration match the stage plan.

The adapter receives an opaque query invocation identifier. Gold answers,
reference trajectory identifiers, and question metadata do not cross the
adapter boundary. The official reader and evaluator remain outside Sibyl.

The v1.3 release experiment compares the declared machine, naive, and render
configurations without changing the reader or judge. Every arm records the
official web and enterprise receipts, provider usage, cost, runtime geometry,
source commit, dataset revision, and official harness commit.
