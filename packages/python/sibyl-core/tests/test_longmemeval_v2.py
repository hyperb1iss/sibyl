from __future__ import annotations

import json
from pathlib import Path

import pytest

from sibyl_core.evals.longmemeval_v2 import (
    build_longmemeval_v2_trajectory_text,
    load_longmemeval_v2_haystack,
    load_longmemeval_v2_questions,
    select_longmemeval_v2_trajectories,
    summarize_longmemeval_v2_inputs,
)


def test_longmemeval_v2_loads_questions_haystack_and_selected_trajectories(
    tmp_path: Path,
) -> None:
    questions_path = tmp_path / "questions.jsonl"
    haystack_path = tmp_path / "lme_v2_small.json"
    trajectories_path = tmp_path / "trajectories.jsonl"
    questions_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "q1",
                        "domain": "enterprise",
                        "environment": "workarena",
                        "question_type": "dynamic-environment",
                        "question": "Which filter was selected?",
                        "image": None,
                        "answer": "The priority filter.",
                        "eval_function": "exact_match",
                    }
                ),
                json.dumps(
                    {
                        "id": "q2",
                        "domain": "web",
                        "environment": "visualwebarena",
                        "question_type": "procedure",
                        "question": "How did the checkout flow finish?",
                        "image": "question_screenshots/q2.png",
                        "answer": "It confirmed the order.",
                        "eval_function": "llm_judge",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    haystack_path.write_text(
        json.dumps({"q1": ["t1", "t2"], "q2": ["t3"]}),
        encoding="utf-8",
    )
    trajectories_path.write_text(
        "\n".join(
            [
                _trajectory_json("t2", action="click filter", thought="Need incidents"),
                _trajectory_json("t1", tree="button Priority\nlist Incidents"),
                _trajectory_json("t3", domain="web", environment="shopping"),
            ]
        ),
        encoding="utf-8",
    )

    questions = load_longmemeval_v2_questions(questions_path)
    haystack = load_longmemeval_v2_haystack(haystack_path)
    selected = select_longmemeval_v2_trajectories(trajectories_path, ["t1", "t3"])
    summary = summarize_longmemeval_v2_inputs(
        questions,
        haystack,
        trajectories=selected,
    )

    assert [question.id for question in questions] == ["q1", "q2"]
    assert questions[1].image == "question_screenshots/q2.png"
    assert haystack == {"q1": ["t1", "t2"], "q2": ["t3"]}
    assert list(selected) == ["t1", "t3"]
    assert summary["question_count"] == 2
    assert summary["domain_counts"] == {"enterprise": 1, "web": 1}
    assert summary["haystack_min"] == 1
    assert summary["haystack_max"] == 2
    assert summary["missing_trajectory_count"] == 1


def test_longmemeval_v2_trajectory_text_keeps_action_and_observation(
    tmp_path: Path,
) -> None:
    trajectory = next(
        iter(
            select_longmemeval_v2_trajectories(
                _write_trajectories_fixture(tmp_path),
                ["t1"],
            ).values()
        )
    )

    text = build_longmemeval_v2_trajectory_text(
        trajectory,
        include_screenshot_refs=True,
        max_accessibility_chars=12,
    )

    assert "Trajectory: t1" in text
    assert "Goal: Resolve the assigned incident." in text
    assert "Action: click filter" in text
    assert "Thought: Need incidents" in text
    assert "Screenshot: screenshots/t1/0.png" in text
    assert "Accessibility tree: button Prior" in text


def test_longmemeval_v2_rejects_missing_required_question_field(
    tmp_path: Path,
) -> None:
    questions_path = tmp_path / "questions.jsonl"
    questions_path.write_text(
        json.dumps(
            {
                "id": "q1",
                "domain": "enterprise",
                "environment": "workarena",
                "question_type": "procedure",
                "image": None,
                "answer": "Done",
                "eval_function": "exact_match",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="question"):
        load_longmemeval_v2_questions(questions_path)


def _write_trajectories_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "trajectories.jsonl"
    path.write_text(_trajectory_json("t1", action="click filter", thought="Need incidents"))
    return path


def _trajectory_json(
    trajectory_id: str,
    *,
    domain: str = "enterprise",
    environment: str = "workarena",
    tree: str = "button Priority\nlist Incidents",
    action: str | None = None,
    thought: str | None = None,
) -> str:
    return json.dumps(
        {
            "id": trajectory_id,
            "domain": domain,
            "environment": environment,
            "goal": "Resolve the assigned incident.",
            "outcome": "success",
            "start_url": "https://example.test/start",
            "states": [
                {
                    "state_index": 0,
                    "step": 0,
                    "url": "https://example.test/start",
                    "action": action,
                    "thought": thought,
                    "accessibility_tree": tree,
                    "screenshot": f"screenshots/{trajectory_id}/0.png",
                }
            ],
        }
    )
