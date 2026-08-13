"""Identifier-flank regression probe (plan §4 A2).

Dense-first retrieval shipped with an exact-key escape hatch (P2 retrieval
keys + the identifier classifier). This probe measures that flank end to end:
for a set of identifier-shaped queries whose target entity is known, it runs
each query twice against a live corpus — verbatim, where the exact-key lane
fires, and shape-mangled, where the identifier token is rewritten into prose
shape so the classifier stays silent and retrieval is dense-alone. The probe's
purpose is a query set that dense-alone measurably misses: the verbatim arm
must reach the target at a rate the mangled arm does not.

Cases are mined from the corpus itself (entities carrying declared
retrieval_keys, read through the admin debug endpoint, which needs an
owner-scoped token — the isolated bench stack qualifies) or supplied via
--cases JSON: [{"query": ..., "expected_uuid": ..., "key": ...}].

The mangled control shares the query's semantics but not its token shape;
embeddings of the two variants differ slightly, which is the price of having
no per-request lane toggle. The margin, not the absolute rate, is the signal.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "python" / "sibyl-core" / "src"))

from sibyl_core.retrieval.identifier_query import (  # noqa: E402
    identifier_probe_tokens,
)


def _post(url: str, token: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def mangle_identifier(token: str) -> str:
    """Rewrite one identifier-shaped token into prose shape.

    Underscores and separators become spaces, camel humps split, and the
    result lowercases, so no predicate in the classifier fires while the
    words survive for dense retrieval.
    """
    text = token.strip('"`')
    text = text.replace("::", " ").replace("_", " ").replace("--", " ")
    text = re.sub(r"\.(?=\w{2})", " ", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[a-zA-Z])(?=\d)|(?<=\d)(?=[a-zA-Z])", " ", text)
    # A pure digit run of eight or more matches the classifier's hex-run
    # predicate, so long numbers must break into short groups too.
    text = re.sub(r"\d{8,}", lambda m: " ".join(re.findall(r"\d{1,4}", m.group())), text)
    return re.sub(r"\s+", " ", text).lower().strip()


def mangle_query(query: str) -> str:
    tokens = identifier_probe_tokens(query)
    mangled = query
    for token in tokens:
        mangled = mangled.replace(token, mangle_identifier(token))
    return mangled


def mine_cases(api_url: str, token: str, count: int, timeout: float) -> list[dict[str, str]]:
    rows = _post(
        f"{api_url}/admin/debug/query",
        token,
        {
            "cypher": (
                "SELECT uuid, name, retrieval_keys FROM entity "
                "WHERE retrieval_keys != NONE AND array::len(retrieval_keys) > 0 "
                f"LIMIT {int(count) * 3};"
            )
        },
        timeout,
    ).get("rows", [])
    cases: list[dict[str, str]] = []
    for row in rows:
        keys = [k for k in (row.get("retrieval_keys") or []) if isinstance(k, str)]
        shaped = [k for k in keys if identifier_probe_tokens(k)]
        if not shaped:
            continue
        key = shaped[0]
        cases.append(
            {
                "query": f"what do we know about {key}",
                "expected_uuid": str(row.get("uuid")),
                "key": key,
            }
        )
        if len(cases) >= count:
            break
    return cases


def mine_content_cases(
    api_url: str, token: str, count: int, timeout: float
) -> list[dict[str, str]]:
    """Fallback mining when the corpus declares no retrieval keys.

    Benchmark corpora ingest through /memory/experience, which declares no
    retrieval keys, so the exact-key lane never fires there and key-based
    mining returns nothing. Content mining measures the flank's current
    exposure instead: identifier-shaped tokens are pulled from entity names,
    and the verbatim arm exercises fulltext+dense retrieval of that token.
    """
    rows = _post(
        f"{api_url}/admin/debug/query",
        token,
        {
            "cypher": (
                "SELECT uuid, name FROM entity "
                "WHERE entity_type IN ['procedure', 'error_pattern', 'artifact'] "
                f"LIMIT {int(count) * 20};"
            )
        },
        timeout,
    ).get("rows", [])
    cases: list[dict[str, str]] = []
    seen_tokens: set[str] = set()
    for row in rows:
        name = str(row.get("name") or "")
        shaped = identifier_probe_tokens(name)
        fresh = [t for t in shaped if t not in seen_tokens and len(t) >= 6]
        if not fresh:
            continue
        token_text = fresh[0]
        seen_tokens.add(token_text)
        cases.append(
            {
                "query": f"what do we know about {token_text}",
                "expected_uuid": str(row.get("uuid")),
                "key": token_text,
            }
        )
        if len(cases) >= count:
            break
    return cases


def run_probe(
    cases: list[dict[str, str]],
    *,
    api_url: str,
    token: str,
    limit: int,
    timeout: float,
) -> dict[str, Any]:
    results = []
    verbatim_hits = 0
    mangled_hits = 0
    for case in cases:
        query = case["query"]
        control = mangle_query(query)
        if identifier_probe_tokens(control):
            raise RuntimeError(f"control still identifier-shaped: {control!r}")
        arms = {}
        for arm, arm_query in (("verbatim", query), ("dense_control", control)):
            payload = {"query": arm_query, "limit": limit}
            response = _post(f"{api_url}/search", token, payload, timeout)
            found = [
                str(item.get("uuid") or item.get("id") or "")
                for item in (response.get("results") or [])
            ]
            arms[arm] = case["expected_uuid"] in found
        verbatim_hits += arms["verbatim"]
        mangled_hits += arms["dense_control"]
        results.append({**case, "control_query": control, **arms})
    return {
        "cases": results,
        "case_count": len(cases),
        "verbatim_hit_rate": verbatim_hits / len(cases) if cases else 0.0,
        "dense_control_hit_rate": mangled_hits / len(cases) if cases else 0.0,
        "flank_margin": (verbatim_hits - mangled_hits) / len(cases) if cases else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--api-token-file", required=True)
    parser.add_argument("--cases", default=None)
    parser.add_argument("--mine", type=int, default=20)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    token = Path(args.api_token_file).read_text().strip()
    if args.cases:
        cases = json.loads(Path(args.cases).read_text())
    else:
        cases = mine_cases(args.api_url, token, args.mine, args.timeout_seconds)
        if not cases:
            print("no declared retrieval keys; falling back to content-mined cases")
            cases = mine_content_cases(args.api_url, token, args.mine, args.timeout_seconds)
    if not cases:
        print("no probe cases found (no identifier-shaped keys or content tokens)")
        return 1
    report = run_probe(
        cases,
        api_url=args.api_url,
        token=token,
        limit=args.limit,
        timeout=args.timeout_seconds,
    )
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(
        f"identifier probe: {report['case_count']} cases | "
        f"verbatim {report['verbatim_hit_rate']:.2%} vs "
        f"dense-alone {report['dense_control_hit_rate']:.2%} | "
        f"flank margin {report['flank_margin']:+.2%}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
