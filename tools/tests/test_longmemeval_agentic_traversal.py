"""A3 agentic traversal: bounded widening loop, converters, and admission lane.

The controller is tested without a network or a model: every hook is a fake.
The composer tests pin the two invariants the additive-arm campaign paid for,
applied to the traversal lane: gathered evidence never evicts a seed, and the
arm disabled composes byte-identically to the pre-arm geometry.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclass field resolution reads sys.modules[cls.__module__], so the
    # module has to be registered before its body executes.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_traversal_module() -> ModuleType:
    return _load_module(
        Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_memory" / "agentic_traversal.py",
        "agentic_traversal",
    )


def _load_memory_module() -> ModuleType:
    return _load_module(
        Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_memory" / "sibyl_memory.py",
        "sibyl_memory",
    )


def _load_runner_module() -> ModuleType:
    return _load_module(
        Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_official.py",
        "longmemeval_v2_official",
    )


def _pool_candidate(candidate_id: str, *, content: str = "State 0\nEvidence") -> dict[str, Any]:
    return {
        "id": candidate_id,
        "type": "session",
        "name": f"candidate {candidate_id}",
        "content": content,
        "score": 0.5,
        "metadata": {"longmemeval_v2_trajectory_id": "t1"},
    }


def _search_result(
    trajectory_id: str,
    *,
    chunk_index: int,
    score: float,
    content: str = "State 0\nEvidence",
) -> dict[str, Any]:
    return {
        "id": f"entity:{trajectory_id}:{chunk_index}",
        "type": "session",
        "name": f"Trajectory {trajectory_id} chunk {chunk_index}",
        "content": content,
        "score": score,
        "result_origin": "graph",
        "metadata": {
            "longmemeval_v2_trajectory_id": trajectory_id,
            "longmemeval_v2_chunk_index": chunk_index,
        },
    }


def _traversal_candidate(
    candidate_id: str, *, content: str = "gathered evidence"
) -> dict[str, Any]:
    return {
        "id": candidate_id,
        "type": "session",
        "name": f"gathered {candidate_id}",
        "content": content,
        "score": None,
        "metadata": {"traversal_verb": "expand_neighbors"},
        "_selection_origin": "traversal:expand",
    }


# --- decision parsing -------------------------------------------------------


def test_parse_decision_sufficient() -> None:
    module = _load_traversal_module()
    decision = module.parse_traversal_decision(
        '{"sufficient": true}',
        max_actions=4,
        known_ids={"a"},
        followup_searches_left=1,
    )
    assert decision.sufficient is True
    assert decision.parse_error == ""


def test_parse_decision_malformed_json_degrades_to_sufficient() -> None:
    module = _load_traversal_module()
    decision = module.parse_traversal_decision(
        "the pool looks thin, let me think...",
        max_actions=4,
        known_ids=set(),
        followup_searches_left=1,
    )
    assert decision.sufficient is True
    assert "invalid JSON" in decision.parse_error


def test_parse_decision_fenced_json_is_accepted() -> None:
    module = _load_traversal_module()
    raw = '```json\n{"sufficient": false, "actions": [{"verb": "fetch_slice", "entity_id": "a"}]}\n```'
    decision = module.parse_traversal_decision(
        raw,
        max_actions=4,
        known_ids={"a"},
        followup_searches_left=1,
    )
    assert decision.sufficient is False
    assert decision.actions[0].verb == "fetch_slice"
    assert decision.actions[0].entity_id == "a"


def test_parse_decision_drops_unknown_verbs_and_unknown_ids() -> None:
    module = _load_traversal_module()
    raw = json.dumps(
        {
            "sufficient": False,
            "actions": [
                {"verb": "delete_everything", "entity_id": "a"},
                {"verb": "expand_neighbors", "entity_ids": ["ghost", "a"]},
                {"verb": "fetch_slice", "entity_id": "ghost"},
            ],
        }
    )
    decision = module.parse_traversal_decision(
        raw,
        max_actions=4,
        known_ids={"a"},
        followup_searches_left=0,
    )
    assert decision.sufficient is False
    assert len(decision.actions) == 1
    assert decision.actions[0].verb == "expand_neighbors"
    assert decision.actions[0].entity_ids == ["a"]
    assert any("unknown verb" in item for item in decision.dropped_actions)
    assert any("fetch_slice" in item for item in decision.dropped_actions)


def test_parse_decision_enforces_action_cap_and_search_budget() -> None:
    module = _load_traversal_module()
    raw = json.dumps(
        {
            "sufficient": False,
            "actions": [
                {"verb": "search", "query": "first"},
                {"verb": "search", "query": "second"},
                {"verb": "fetch_slice", "entity_id": "a"},
                {"verb": "fetch_slice", "entity_id": "b"},
                {"verb": "fetch_slice", "entity_id": "c"},
            ],
        }
    )
    decision = module.parse_traversal_decision(
        raw,
        max_actions=3,
        known_ids={"a", "b", "c"},
        followup_searches_left=1,
    )
    assert [action.verb for action in decision.actions] == [
        "search",
        "fetch_slice",
        "fetch_slice",
    ]
    assert any("search budget exhausted" in item for item in decision.dropped_actions)
    assert any("action cap reached" in item for item in decision.dropped_actions)


def test_parse_decision_all_actions_dropped_degrades_to_sufficient() -> None:
    module = _load_traversal_module()
    raw = json.dumps(
        {"sufficient": False, "actions": [{"verb": "expand_neighbors", "entity_ids": ["ghost"]}]}
    )
    decision = module.parse_traversal_decision(
        raw,
        max_actions=4,
        known_ids={"a"},
        followup_searches_left=1,
    )
    assert decision.sufficient is True
    assert "no executable actions" in decision.parse_error


# --- converters -------------------------------------------------------------


def test_neighbors_to_candidates_maps_fields_and_metadata() -> None:
    module = _load_traversal_module()
    response = {
        "neighbors": [
            {
                "id": "entity:n1",
                "type": "session",
                "name": "neighbor one",
                "relationship": "DERIVED_FROM",
                "direction": "incoming",
                "distance": 2,
                "score": 0.7,
                "content": "neighbor content",
                "metadata": {"passage_index": 1, "parent_entity_id": "entity:p"},
            },
            {"id": "", "content": "dropped: no id"},
            "not a dict",
        ]
    }
    candidates = module.neighbors_to_candidates(response)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["id"] == "entity:n1"
    assert candidate["_selection_origin"] == "traversal:expand"
    assert candidate["metadata"]["traversal_verb"] == "expand_neighbors"
    assert candidate["metadata"]["traversal_relationship"] == "DERIVED_FROM"
    assert candidate["metadata"]["passage_index"] == 1


def test_slice_to_candidate_joins_window_and_disambiguates_id() -> None:
    module = _load_traversal_module()
    response = {
        "entity_id": "entity:span2",
        "parent_id": "entity:parent",
        "parent_name": "the memory",
        "parent_type": "session",
        "window_start": 1,
        "passage_total": 4,
        "covers_parent": False,
        "sliced": True,
        "passages": [
            {"id": "entity:span1", "content": "first span", "breadcrumb": "Section A"},
            {"id": "entity:span2", "content": "second span"},
            {"id": "entity:span3", "content": ""},
        ],
    }
    candidate = module.slice_to_candidate(response)
    assert candidate is not None
    assert candidate["id"] == "entity:parent#slice:1"
    assert candidate["name"] == "the memory"
    assert "[Section A]\nfirst span" in candidate["content"]
    assert "second span" in candidate["content"]
    assert candidate["metadata"]["parent_entity_id"] == "entity:parent"
    assert candidate["_selection_origin"] == "traversal:slice"


def test_slice_to_candidate_returns_none_when_nothing_readable() -> None:
    module = _load_traversal_module()
    assert module.slice_to_candidate({"parent_id": "p", "passages": []}) is None
    assert (
        module.slice_to_candidate({"parent_id": "p", "passages": [{"id": "s", "content": "   "}]})
        is None
    )


# --- the widening loop ------------------------------------------------------


def test_loop_sufficient_first_round_gathers_nothing() -> None:
    module = _load_traversal_module()
    calls: list[str] = []

    def llm(_system: str, _prompt: str) -> str:
        calls.append("llm")
        return '{"sufficient": true}'

    def unreachable(_action: object) -> dict[str, Any]:
        raise AssertionError("no verb should execute")

    gathered, trace = module.run_agentic_traversal(
        question="what changed?",
        pool=[_pool_candidate("a")],
        llm_complete=llm,
        execute_expand=unreachable,
        execute_slice=unreachable,
        execute_search=unreachable,
        widening_rounds=2,
    )
    assert gathered == []
    assert calls == ["llm"]
    assert trace["sufficient_early_stop"] is True
    assert trace["rounds_used"] == 1
    assert trace["gathered_candidates"] == 0


def test_loop_executes_actions_and_dedupes_against_pool_and_rounds() -> None:
    module = _load_traversal_module()
    decisions = iter(
        [
            json.dumps(
                {
                    "sufficient": False,
                    "actions": [
                        {"verb": "expand_neighbors", "entity_ids": ["a"]},
                        {"verb": "search", "query": "refined"},
                    ],
                }
            ),
            json.dumps(
                {
                    "sufficient": False,
                    # entity:n1 was gathered in round one, so it is known now.
                    "actions": [{"verb": "fetch_slice", "entity_id": "entity:n1"}],
                }
            ),
        ]
    )

    def llm(_system: str, _prompt: str) -> str:
        return next(decisions)

    def expand(action: object) -> dict[str, Any]:
        assert getattr(action, "entity_ids", None) == ["a"]
        return {
            "neighbors": [
                {"id": "entity:n1", "type": "session", "name": "n1", "content": "n1 content"},
                # Already in the pool: must be dropped by the id dedup.
                {"id": "a", "type": "session", "name": "a", "content": "duplicate"},
            ]
        }

    def slice_fn(action: object) -> dict[str, Any]:
        assert getattr(action, "entity_id", None) == "entity:n1"
        return {
            "parent_id": "entity:parent",
            "parent_name": "parent",
            "parent_type": "session",
            "window_start": 0,
            "passages": [{"id": "entity:n1", "content": "span content"}],
        }

    def search(action: object) -> list[dict[str, Any]]:
        assert getattr(action, "query", None) == "refined"
        return [
            {"id": "entity:s1", "type": "session", "name": "s1", "content": "found"},
            {"id": "entity:n1", "type": "session", "name": "n1", "content": "duplicate"},
        ]

    gathered, trace = module.run_agentic_traversal(
        question="multi-hop?",
        pool=[_pool_candidate("a")],
        llm_complete=llm,
        execute_expand=expand,
        execute_slice=slice_fn,
        execute_search=search,
        widening_rounds=2,
    )

    gathered_ids = [item["id"] for item in gathered]
    assert gathered_ids == ["entity:n1", "entity:s1", "entity:parent#slice:0"]
    origins = {item["_selection_origin"] for item in gathered}
    assert origins == {"traversal:expand", "traversal:search", "traversal:slice"}
    assert trace["rounds_used"] == trace["rounds_budget"]
    assert trace["followup_searches_used"] == 1
    assert trace["gathered_candidates"] == len(gathered_ids)
    assert all(action["ok"] for action in trace["actions"])


def test_loop_second_round_search_budget_is_spent() -> None:
    module = _load_traversal_module()
    prompts: list[str] = []
    decisions = iter(
        [
            json.dumps({"sufficient": False, "actions": [{"verb": "search", "query": "first"}]}),
            json.dumps({"sufficient": False, "actions": [{"verb": "search", "query": "second"}]}),
        ]
    )

    def llm(_system: str, prompt: str) -> str:
        prompts.append(prompt)
        return next(decisions)

    def unreachable(_action: object) -> dict[str, Any]:
        raise AssertionError("only search should execute")

    def search(_action: object) -> list[dict[str, Any]]:
        return [{"id": "entity:s1", "type": "session", "name": "s1", "content": "found"}]

    gathered, trace = module.run_agentic_traversal(
        question="q",
        pool=[_pool_candidate("a")],
        llm_complete=llm,
        execute_expand=unreachable,
        execute_slice=unreachable,
        execute_search=search,
        widening_rounds=2,
        followup_searches=1,
    )
    assert [item["id"] for item in gathered] == ["entity:s1"]
    assert trace["followup_searches_used"] == 1
    # The second-round prompt announces exhaustion and the parser enforces it.
    assert any("exhausted" in prompt for prompt in prompts[1:])
    assert any("search budget exhausted" in item for item in trace["dropped_actions"])


def test_loop_deadline_stops_before_any_model_call() -> None:
    module = _load_traversal_module()
    ticks = iter([0.0, 100.0, 100.0])

    def clock() -> float:
        return next(ticks)

    def llm(_system: str, _prompt: str) -> str:
        raise AssertionError("deadline must stop the loop before the model runs")

    def unreachable(_action: object) -> dict[str, Any]:
        raise AssertionError("no verb should execute")

    gathered, trace = module.run_agentic_traversal(
        question="q",
        pool=[_pool_candidate("a")],
        llm_complete=llm,
        execute_expand=unreachable,
        execute_slice=unreachable,
        execute_search=unreachable,
        widening_rounds=2,
        deadline_seconds=5.0,
        clock=clock,
    )
    assert gathered == []
    assert trace["deadline_hit"] is True
    assert trace["rounds_used"] == 0


def test_loop_isolates_one_failing_action() -> None:
    module = _load_traversal_module()

    def llm(_system: str, _prompt: str) -> str:
        return json.dumps(
            {
                "sufficient": False,
                "actions": [
                    {"verb": "expand_neighbors", "entity_ids": ["a"]},
                    {"verb": "fetch_slice", "entity_id": "b"},
                ],
            }
        )

    def expand(_action: object) -> dict[str, Any]:
        raise RuntimeError("surreal hiccup")

    def slice_fn(_action: object) -> dict[str, Any]:
        return {
            "parent_id": "entity:parent",
            "parent_name": "parent",
            "parent_type": "session",
            "window_start": 0,
            "passages": [{"id": "b", "content": "span content"}],
        }

    def search(_action: object) -> list[dict[str, Any]]:
        raise AssertionError("no search requested")

    gathered, trace = module.run_agentic_traversal(
        question="q",
        pool=[_pool_candidate("a"), _pool_candidate("b")],
        llm_complete=llm,
        execute_expand=expand,
        execute_slice=slice_fn,
        execute_search=search,
        widening_rounds=1,
    )
    assert [item["id"] for item in gathered] == ["entity:parent#slice:0"]
    action_traces = trace["actions"]
    assert action_traces[0]["ok"] is False
    assert "surreal hiccup" in action_traces[0]["error"]
    assert action_traces[1]["ok"] is True


def test_loop_llm_failure_degrades_to_baseline() -> None:
    module = _load_traversal_module()

    def llm(_system: str, _prompt: str) -> str:
        raise RuntimeError("model unreachable")

    def unreachable(_action: object) -> dict[str, Any]:
        raise AssertionError("no verb should execute")

    gathered, trace = module.run_agentic_traversal(
        question="q",
        pool=[_pool_candidate("a")],
        llm_complete=llm,
        execute_expand=unreachable,
        execute_slice=unreachable,
        execute_search=unreachable,
        widening_rounds=2,
    )
    assert gathered == []
    assert any("llm call failed" in item for item in trace["parse_errors"])


def test_prompt_is_question_text_and_previews_only() -> None:
    module = _load_traversal_module()
    prompt = module.build_traversal_prompt(
        question="What did the run change?",
        pool_previews=module.build_pool_previews([_pool_candidate("a")]),
        gathered_previews=[],
        round_number=2,
        total_rounds=2,
        max_actions=4,
        followup_searches_left=1,
    )
    assert "What did the run change?" in prompt
    assert "id=a" in prompt
    for forbidden in ("answer:", "gold", "score:"):
        assert forbidden not in prompt.lower()


# --- composer admission lane ------------------------------------------------


def test_composer_traversal_lane_admits_without_evicting_seeds() -> None:
    module = _load_memory_module()
    query = "inventory order dashboard"
    raw_results = [
        _search_result("t1", chunk_index=0, score=1.0),
        _search_result("t2", chunk_index=0, score=0.9),
        _search_result("t3", chunk_index=0, score=0.8),
        _search_result("t4", chunk_index=0, score=0.7),
    ]

    baseline_selected, baseline_composition = module.compile_operational_evidence_set(
        query=query,
        typed_results=[],
        raw_results=[dict(item) for item in raw_results],
        max_items=4,
    )
    traversal_selected, traversal_composition = module.compile_operational_evidence_set(
        query=query,
        typed_results=[],
        raw_results=[dict(item) for item in raw_results],
        max_items=4,
        traversal_results=[_traversal_candidate("entity:gathered:1")],
        traversal_overflow_items=2,
    )

    baseline_ids = [item["id"] for item in baseline_selected]
    traversal_ids = [item["id"] for item in traversal_selected]
    # Every seed the baseline selected is still there, in the same order.
    assert traversal_ids[: len(baseline_ids)] == baseline_ids
    assert traversal_ids[len(baseline_ids) :] == ["entity:gathered:1"]
    assert traversal_composition["traversal_admitted_items"] == 1
    assert traversal_composition["traversal_candidate_count"] == 1
    assert baseline_composition["traversal_admitted_items"] == 0
    ranks = [item["_evidence_selection_rank"] for item in traversal_selected]
    assert ranks == list(range(1, len(traversal_selected) + 1))


def test_composer_traversal_lane_dedupes_and_filters_empty_content() -> None:
    module = _load_memory_module()
    raw_results = [_search_result("t1", chunk_index=0, score=1.0)]
    selected, composition = module.compile_operational_evidence_set(
        query="q",
        typed_results=[],
        raw_results=[dict(item) for item in raw_results],
        max_items=4,
        traversal_results=[
            # Already selected through the raw lane: dropped.
            _traversal_candidate("entity:t1:0"),
            # Nothing to read: dropped.
            _traversal_candidate("entity:empty", content="   "),
            _traversal_candidate("entity:fresh"),
        ],
        traversal_overflow_items=4,
    )
    assert [item["id"] for item in selected] == ["entity:t1:0", "entity:fresh"]
    assert composition["traversal_candidate_count"] == 1
    assert composition["traversal_admitted_items"] == 1


def test_composer_traversal_lane_respects_char_budget() -> None:
    module = _load_memory_module()
    seed_content = "x" * 50
    fitting_content = "y" * 15
    oversized_content = "z" * 30
    raw_results = [_search_result("t1", chunk_index=0, score=1.0, content=seed_content)]
    selected, composition = module.compile_operational_evidence_set(
        query="q",
        typed_results=[],
        raw_results=[dict(item) for item in raw_results],
        max_items=4,
        char_budget=len(seed_content) + len(fitting_content) + len(oversized_content) // 2,
        traversal_results=[
            _traversal_candidate("entity:fits", content=fitting_content),
            _traversal_candidate("entity:too-big", content=oversized_content),
        ],
        traversal_overflow_items=4,
    )
    assert [item["id"] for item in selected] == ["entity:t1:0", "entity:fits"]
    assert composition["traversal_admitted_items"] == 1
    assert composition["selected_chars"] == len(seed_content) + len(fitting_content)


def test_composer_traversal_cap_bounds_admission() -> None:
    module = _load_memory_module()
    overflow_cap = 2
    selected, composition = module.compile_operational_evidence_set(
        query="q",
        typed_results=[],
        raw_results=[_search_result("t1", chunk_index=0, score=1.0)],
        max_items=2,
        traversal_results=[
            _traversal_candidate("entity:g1"),
            _traversal_candidate("entity:g2"),
            _traversal_candidate("entity:g3"),
        ],
        traversal_overflow_items=overflow_cap,
    )
    gathered_ids = [item["id"] for item in selected if item["id"].startswith("entity:g")]
    assert gathered_ids == ["entity:g1", "entity:g2"]
    assert composition["traversal_admitted_items"] == overflow_cap


def test_item_ceiling_rises_with_traversal_admission() -> None:
    module = _load_memory_module()
    max_items = 8
    support_overflow = 2
    traversal_admitted = 3
    ceiling = module.context_pack_item_ceiling(
        max_items=max_items,
        char_budget=None,
        candidate_count=12,
        overflow_items=support_overflow + traversal_admitted,
    )
    assert ceiling == max_items + support_overflow + traversal_admitted


# --- runner plumbing ---------------------------------------------------------


def test_runner_traversal_args_reach_memory_params_and_default_off(tmp_path: Path) -> None:
    module = _load_runner_module()
    base_argv = [
        "--data-root",
        str(tmp_path / "data"),
        "--domain",
        "enterprise",
        "--output-dir",
        str(tmp_path / "output"),
        "--plan-only",
    ]

    traversal_module = _load_traversal_module()
    arm_rounds = traversal_module.MAX_WIDENING_ROUNDS
    arm_overflow = 6
    default_params = module.build_memory_config(module.parse_args(base_argv))["memory_params"]
    arm_params = module.build_memory_config(
        module.parse_args(
            [
                *base_argv,
                "--agentic-traversal",
                "--traversal-widening-rounds",
                str(arm_rounds),
                "--traversal-model",
                "gpt-5.4-nano",
                "--traversal-overflow-items",
                str(arm_overflow),
            ]
        )
    )["memory_params"]

    assert default_params["agentic_traversal"] is False
    assert arm_params["agentic_traversal"] is True
    assert arm_params["traversal_widening_rounds"] == arm_rounds
    assert arm_params["traversal_model"] == "gpt-5.4-nano"
    assert arm_params["traversal_overflow_items"] == arm_overflow
    assert arm_params["traversal_followup_searches"] == traversal_module.DEFAULT_FOLLOWUP_SEARCHES
    assert (
        arm_params["traversal_deadline_seconds"]
        == traversal_module.DEFAULT_TRAVERSAL_DEADLINE_SECONDS
    )


def test_enabled_arm_without_api_key_fails_at_construction(monkeypatch: Any) -> None:
    """A missing traversal key dies at t=0, not per-question mid-run.

    Per-question degradation with the arm enabled would complete a full paid
    run as baseline geometry under the arm's name. The constructor check fires
    before any auth or network setup, so the run never starts.
    """
    module = _load_memory_module()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SIBYL_OPENAI_API_KEY", raising=False)

    try:
        module.SibylLiveApiMemory({"agentic_traversal": True})
    except RuntimeError as exc:
        message = str(exc)
        assert "OPENAI_API_KEY" in message
        assert "SIBYL_OPENAI_API_KEY" in message
    else:
        raise AssertionError("enabled arm without a key must refuse to construct")


def test_traversal_params_are_runtime_keys_for_attached_corpora() -> None:
    module = _load_memory_module()
    for key in (
        "agentic_traversal",
        "traversal_widening_rounds",
        "traversal_model",
        "traversal_max_actions",
        "traversal_followup_searches",
        "traversal_deadline_seconds",
        "traversal_overflow_items",
        "traversal_search_limit",
    ):
        assert key in module.LOADED_MEMORY_RUNTIME_KEYS
