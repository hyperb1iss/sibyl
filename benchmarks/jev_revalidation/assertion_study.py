"""Factorial full-critic experiment with explicit contracts and independent Jev calls."""

from __future__ import annotations

import argparse
import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

from . import assertion_critic, heldout_fast, heldout_fast_analysis, quality_speed

VERSION = "jev-assertion-critic-study-v1"
SEED = 20260923
REPEATS = 2
MEDIAN_RATIO_MAX = 0.8
MODES = ("direct", "live_jev", "misleading")
ARMS = tuple(f"{contract}_{mode}" for contract in assertion_critic.CONTRACTS for mode in MODES)
COMPARISONS = {
    "prompt_effect": ["baseline_direct", "assertion_direct"],
    "jev_increment": ["assertion_direct", "assertion_live_jev"],
    "prompt_with_jev": ["baseline_live_jev", "assertion_live_jev"],
    "baseline_stress": ["baseline_direct", "baseline_misleading"],
    "assertion_stress": ["assertion_direct", "assertion_misleading"],
    "stress_prompt_effect": ["baseline_misleading", "assertion_misleading"],
}
PROTOCOL = {
    "design": "two fixed full-critic instruction contracts crossed with direct, independently acquired live Jev, and misleading hints",
    "comparisons": COMPARISONS,
    "live_comparison": "live versus live compares combined policies with independently acquired labels; direct versus direct isolates the prompt effect",
    "quality": "strict semantic-pass gain for prompt effect and Jev increment; no paired new false accept, lost valid concern, unsupported finding, or lost supported accept",
    "speed": {"service_p50_ratio_max": MEDIAN_RATIO_MAX, "service_p95_ratio_max": 1.0},
    "cost": {"total_cost_ratio_max": 1.0, "unknown_cost_calls": 0},
    "stress": "compare stress to its own direct contract and assertion stress to baseline stress; no pooling with normal-route metrics",
    "unit": "minimal-pair clusters; cases and repeats within a cluster are dependent",
    "timing": heldout_fast.PROTOCOL["timing"],
    "freeze": "one unchanged implementation and prompt through retained diagnostic and fresh confirmation; cohorts reported separately",
    "authority": "semantic review pending; no automatic acceptance or production publication",
}


@dataclass(frozen=True)
class StudyConfig:
    version: str
    seed: int
    contracts: tuple[str, ...]
    critic_version: str
    request_builder: Callable[..., dict[str, Any]]
    interpreter: heldout_fast.Interpreter
    comparisons: dict[str, list[str]]
    protocol: dict[str, Any]

    @property
    def arms(self) -> tuple[str, ...]:
        return tuple(f"{contract}_{mode}" for contract in self.contracts for mode in MODES)


DEFAULT = StudyConfig(
    VERSION,
    SEED,
    assertion_critic.CONTRACTS,
    assertion_critic.VERSION,
    assertion_critic.critic_request,
    assertion_critic.interpret,
    COMPARISONS,
    PROTOCOL,
)


def entries(
    cases: list[dict[str, Any]], *, repeats: int = REPEATS, config: StudyConfig = DEFAULT
) -> list[dict[str, Any]]:
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    base = heldout_fast.entries(cases, repeats=repeats, seed=config.seed, version=config.version)
    pairs = {(e["case_index"], e["repeat"]): e for e in base}
    rng = random.Random(config.seed)  # noqa: S311 - reproducible randomized paired schedule
    schedule = []
    for entry in pairs.values():
        prepared = PreparedMemoryValidation(entry["prepared_payload"])
        stress = heldout_fast.stress_labels(cases[entry["case_index"]], prepared)
        arms = list(config.arms)
        rng.shuffle(arms)
        for arm in arms:
            contract, mode = arm.split("_", 1)
            identifier = f"case-{entry['case_index']}-repeat-{entry['repeat']}-{arm}"
            # Physical Jev dispatch identity differs; semantic state and paired preparation do not.
            decision = {**entry["jev_request"], "request_id": identifier + ":jev"}
            schedule.append(
                {
                    **entry,
                    "id": identifier,
                    "arm": arm,
                    "contract": contract,
                    "hint_mode": mode,
                    "decision_request_id": decision["request_id"],
                    "jev_request": decision,
                    "direct_request": config.request_builder(prepared, [], contract=contract),
                    "stress_request": config.request_builder(prepared, stress, contract=contract),
                }
            )
    return schedule


def _assemble(cases_path: Path, rubric_path: Path, repeats: int, config: StudyConfig = DEFAULT):
    cases, _, current = heldout_fast._assemble(cases_path, rubric_path)
    schedule = entries(cases, repeats=repeats, config=config)
    current.update(
        version=config.version,
        schedule_sha256=quality_speed.digest(schedule),
        paths=len(schedule),
        critic_calls=len(schedule),
        jev_calls=sum(e["hint_mode"] == "live_jev" for e in schedule),
        repeats=repeats,
        seed=config.seed,
        protocol=config.protocol,
        contracts=list(config.contracts),
        critic_contract_version=config.critic_version,
    )
    return cases, schedule, current


def manifest(
    cases_path: Path, rubric_path: Path, *, repeats: int = REPEATS, config: StudyConfig = DEFAULT
) -> dict[str, Any]:
    return _assemble(cases_path, rubric_path, repeats, config)[2]


async def prepare(
    cases_path: Path, rubric_path: Path, *, repeats: int = REPEATS, config: StudyConfig = DEFAULT
):
    return await asyncio.to_thread(_assemble, cases_path, rubric_path, repeats, config)


def _identity(entry: dict[str, Any], config: StudyConfig = DEFAULT) -> None:
    if (
        entry.get("contract") not in config.contracts
        or entry.get("hint_mode") not in MODES
        or entry["arm"] != f"{entry['contract']}_{entry['hint_mode']}"
    ):
        raise ValueError("arm and critic contract binding changed")
    if (
        entry.get("decision_request_id") != entry["id"] + ":jev"
        or entry["jev_request"]["request_id"] != entry["decision_request_id"]
    ):
        raise ValueError("physical decision request identity changed")


async def execute(
    entry: dict[str, Any],
    case: dict[str, Any],
    *,
    client: httpx.AsyncClient | None,
    out: Path | None,
    archive: Path | None = None,
    config: StudyConfig = DEFAULT,
) -> dict[str, Any]:
    _identity(entry, config)
    return await heldout_fast.execute(
        entry,
        case,
        client=client,
        out=out,
        archive=archive,
        request_builder=partial(config.request_builder, contract=entry["contract"]),
        interpreter=config.interpreter,
    )


def _comparison(name, arms, indexed, cases, repeats, comparisons=COMPARISONS):
    before, after = comparisons[name]
    baseline, candidate = arms[before], arms[after]
    ratios = {
        field: candidate[field] / baseline[field] if baseline[field] else None
        for field in ("service_ms_p50", "service_ms_p95")
    }
    costs_known = baseline["total_cost_usd"] is not None and candidate["total_cost_usd"] is not None
    ratios["cost"] = (
        float(candidate["total_cost_usd"]) / float(baseline["total_cost_usd"])
        if costs_known and float(baseline["total_cost_usd"]) > 0
        else None
    )
    pairs = []
    for case in cases:
        safe = case["expected_action"] == "accept"
        for repeat in repeats:
            a, b = (indexed[case["id"], repeat, arm] for arm in (before, after))
            pairs.append(
                {
                    "case_id": case["id"],
                    "pair_group": case.get("pair_group", case["id"]),
                    "repeat": repeat,
                    "before_action": a["action"],
                    "after_action": b["action"],
                    "hint_labels_agree": {h["claim_path"]: h["value"] for h in a["hints"]}
                    == {h["claim_path"]: h["value"] for h in b["hints"]},
                    "service_delta_ms": b["service_ms"] - a["service_ms"],
                    "new_false_accept": not safe
                    and a["action"] != "accept"
                    and b["action"] == "accept",
                    "lost_supported_accept": safe
                    and a["action"] == "accept"
                    and b["action"] != "accept",
                }
            )
    return {
        "before": before,
        "after": after,
        "ratios": ratios,
        "pairs": pairs,
        "strict_action_gain": candidate["strict_correct"] - baseline["strict_correct"],
        "rubric_action_gain": candidate["rubric_correct"] - baseline["rubric_correct"],
        "mechanical_gates": {
            "median_20_percent_faster": ratios["service_ms_p50"] is not None
            and ratios["service_ms_p50"] <= MEDIAN_RATIO_MAX,
            "p95_not_worse": ratios["service_ms_p95"] is not None and ratios["service_ms_p95"] <= 1,
            "cost_not_worse": ratios["cost"] is not None and ratios["cost"] <= 1,
            "all_cost_known": costs_known,
            "no_new_false_accept": not any(p["new_false_accept"] for p in pairs),
            "no_lost_supported_accept": not any(p["lost_supported_accept"] for p in pairs),
        },
        "semantic_gates": {
            "status": "pending",
            "strict_pass_gain": None,
            "no_lost_valid_concern": None,
            "no_new_unsupported_finding": None,
        },
    }


def summarize(
    cases: list[dict[str, Any]],
    schedule: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    config: StudyConfig = DEFAULT,
) -> dict[str, Any]:
    expected = {e["id"]: e for e in schedule}
    if (
        len(rows) != len(schedule)
        or len(expected) != len(schedule)
        or {r["id"] for r in rows} != expected.keys()
    ):
        raise ValueError("complete unique factorial path coverage required")
    for entry in schedule:
        _identity(entry, config)
    for row in rows:
        if any(
            row[k] != expected[row["id"]][k]
            for k in ("case_id", "repeat", "arm", "contract", "hint_mode")
        ):
            raise ValueError("row critic contract binding changed")
    contracts = {}
    for contract in config.contracts:
        contract_schedule = [
            {**e, "arm": e["hint_mode"]} for e in schedule if e["contract"] == contract
        ]
        contract_rows = [{**r, "arm": r["hint_mode"]} for r in rows if r["contract"] == contract]
        contracts[contract] = heldout_fast_analysis.summarize(
            cases, contract_schedule, contract_rows
        )
    arms = {
        f"{contract}_{mode}": report["arms"][mode]
        for contract, report in contracts.items()
        for mode in MODES
    }
    indexed = {(r["case_id"], r["repeat"], r["arm"]): r for r in rows}
    repeats = sorted({e["repeat"] for e in schedule})
    if len(indexed) != len(cases) * len(repeats) * len(config.arms):
        raise ValueError("complete factorial grid required")
    return {
        "arms": arms,
        "contracts": contracts,
        "comparisons": {
            name: _comparison(name, arms, indexed, cases, repeats, config.comparisons)
            for name in config.comparisons
        },
        "independent_clusters": len({c.get("pair_group", c["id"]) for c in cases}),
        "all_arms_acquisition": heldout_fast_analysis._metrics(rows, {c["id"]: c for c in cases}),
        "semantic_review_status": "pending",
        "production_qualified": False,
        "limits": [
            "Mechanical action matches do not establish finding correctness; semantic review is pending.",
            "Retained diagnostic and fresh confirmation cohorts must remain separate; prompt and implementation stay fixed between them.",
            "Six arms use independent fresh critic calls and each live arm independently pays for its own Jev acquisition.",
            "Misleading hints are a robustness diagnostic, not a normal-route latency or cost comparison.",
            "Minimal pairs and repeats are dependent clusters; synthetic critique is not downstream task or publication evidence.",
        ],
    }


async def run(
    cases_path: Path,
    rubric_path: Path,
    out: Path,
    *,
    repeats: int = REPEATS,
    freeze: Path | None = None,
    live: bool = False,
    replay_path: Path | None = None,
    config: StudyConfig = DEFAULT,
):
    return await heldout_fast.run(
        cases_path,
        rubric_path,
        out,
        freeze=freeze,
        live=live,
        replay_path=replay_path,
        prepare_fn=partial(prepare, repeats=repeats, config=config),
        execute_fn=partial(execute, config=config),
        summarize_fn=partial(summarize, config=config),
    )


def main(config: StudyConfig = DEFAULT) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("cases", "rubric", "output-dir"):
        parser.add_argument(f"--{flag}", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--freeze", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", type=Path)
    args = parser.parse_args()
    asyncio.run(
        run(
            args.cases,
            args.rubric,
            args.output_dir,
            repeats=args.repeats,
            freeze=args.freeze,
            live=args.live,
            replay_path=args.replay,
            config=config,
        )
    )


if __name__ == "__main__":
    main()
