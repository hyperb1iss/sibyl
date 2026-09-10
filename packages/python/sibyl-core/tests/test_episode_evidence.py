"""Controller evidence projection keeps distinct events and original citations."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from sibyl_core.tasks import consolidation as c
from sibyl_core.tasks.episode_evidence import project_episode


def _encoded(value):
    raw = json.dumps(value, ensure_ascii=False).encode()
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()


def _episode():
    messages = [{"role": "system", "content": "task"}, {"role": "user", "content": "budget"}]
    body = {"messages": messages, "model": "test"}
    raw = {
        "choices": [{"message": {"role": "assistant", "content": "λ result"}}],
        "usage": {"tokens": 1},
    }
    body64, body_hash = _encoded(body)
    response64, response_hash = _encoded(raw)
    payloads = [
        (
            "start",
            {"request": {"goal": "preserve evidence"}, "options": {}, "workspace_initial": {}},
        ),
        ("model_request", {"body": body, "body_base64": body64, "body_sha256": body_hash}),
        (
            "model_response",
            {
                "raw": raw,
                "body_base64": response64,
                "body_sha256": response_hash,
                "status_code": 200,
            },
        ),
        ("terminal", {"detail": "finished", "exit": 0, "reason": "complete", "usage": {}}),
    ]
    return {
        "schema_version": "sibyl-learning-episode-v1",
        "assignment": {"attempt": "one"},
        "assurance": {},
        "goal": "preserve evidence",
        "input_memory_pack_sha256": "a" * 64,
        "outcome": {"status": "passed"},
        "sealed_isolation": {},
        "trace": [
            {
                "schema_version": "sibyl-coding-trace-v1",
                "attempt_id": "one",
                "request_id": "one",
                "index": index,
                "kind": kind,
                "payload": payload,
            }
            for index, (kind, payload) in enumerate(payloads)
        ],
    }


def test_projection_cites_selected_original_values_and_classifies_audit() -> None:
    artifact = json.dumps(_episode(), ensure_ascii=False).encode()
    result = project_episode("source", artifact, prefix="s0")
    assert len(result.view["events"]) == 4
    citation = result.citations["s0.e2"]
    assert citation.episode_id == "source"
    cited = [json.loads(artifact[start:end]) for start, end in citation.ranges]
    assert [{"message": {"role": "assistant", "content": "λ result"}}] in cited
    assert any(row["disposition"] == "verified_encoding_alias" for row in result.coverage)
    assert any(
        row["path"][-1] == "usage" and row["disposition"] == "transport_audit"
        for row in result.coverage
    )


@pytest.mark.parametrize("mutation", ["encoding", "digest", "unknown", "history", "value_type"])
def test_distinct_or_unclassified_evidence_cannot_be_dropped(mutation: str) -> None:
    episode = _episode()
    request = episode["trace"][1]["payload"]
    if mutation == "encoding":
        request["body_base64"], _ = _encoded({"messages": [], "model": "other"})
    elif mutation == "digest":
        request["body_sha256"] = "0" * 64
    elif mutation == "value_type":
        request["body"]["stream"] = True
        request["body_base64"], request["body_sha256"] = _encoded({**request["body"], "stream": 1})
    elif mutation == "unknown":
        episode["trace"][2]["payload"]["new_error"] = "must remain visible"
    else:
        repeated = deepcopy(episode["trace"][1])
        repeated["index"] = 3
        episode["trace"].insert(3, repeated)
    with pytest.raises(ValueError):
        project_episode("source", json.dumps(episode).encode(), prefix="s0")


def test_repeated_history_is_verified_against_visible_response() -> None:
    episode = _episode()
    repeated = deepcopy(episode["trace"][1])
    body = repeated["payload"]["body"]
    body["messages"].append({"role": "assistant", "content": "λ result"})
    repeated["payload"]["body_base64"], repeated["payload"]["body_sha256"] = _encoded(body)
    episode["trace"].insert(3, repeated)
    result = project_episode("source", json.dumps(episode).encode(), prefix="s0")
    assert "initial_messages" not in result.view["events"][3]
    assert any(row["disposition"] == "verified_event_history_alias" for row in result.coverage)


def test_additional_response_error_remains_visible() -> None:
    episode = _episode()
    response = episode["trace"][2]["payload"]
    response["raw"]["error"] = {"detail": "distinct partial error"}
    response["body_base64"], response["body_sha256"] = _encoded(response["raw"])
    result = project_episode("source", json.dumps(episode).encode(), prefix="s0")
    assert result.view["events"][2]["additional_response_fields"]["error"] == {
        "detail": "distinct partial error"
    }


def _contrast_group():
    episodes = []
    for index, status in enumerate(("passed", "task_failed")):
        artifact = json.dumps(_episode()).encode()
        episodes.append(
            c.ConsolidationEpisode(
                episode_id=f"episode-{index}",
                session_id=f"session-{index}",
                family_id="family",
                split="learning",
                artifact=artifact,
                artifact_sha256=hashlib.sha256(artifact).hexdigest(),
                environment={"runtime": "python"},
                stored_sources=(
                    c.StoredSourceRef(source_id=f"source-{index}", observed_revision=1),
                ),
                outcome=c.DeclaredTaskOutcome(
                    receipt_schema_version="sibyl-agent-task-receipt-v1",
                    task_id=f"task-{index}",
                    attempt_id=f"attempt-{index}",
                    status=status,
                    success=status == "passed",
                    controller_final_snapshot_sha256="a" * 64,
                    checker_input_snapshot_sha256="a" * 64,
                    receipt_sha256=hashlib.sha256(str(index).encode()).hexdigest(),
                ),
            )
        )
    return c.ConsolidationGroup(
        group_id="group",
        mechanism="distinct evidence",
        organization_id="org",
        owner_principal_id="owner",
        memory_scope="private",
        environment_compatibility_keys=("runtime",),
        episodes=tuple(episodes),
    )


def _proposal():
    def assertion(identity):
        return {
            "statement": "observed result",
            "label": "observed",
            "support": [{"evidence_id": identity}],
        }

    success, failure = assertion("s0.e2"), assertion("s1.e2")
    return {
        "procedure": {
            "goal": success,
            "environment": [success],
            "preconditions": [success],
            "actions": [{"order": 1, "action": success, "success_criteria": success}],
            "expected_result": success,
            "failure_modes": [failure],
            "abstain_when": [failure],
        }
    }


@pytest.mark.parametrize("mutation", [None, "unknown_id", "coverage_hash", "outside_visible"])
async def test_projected_proposal_resolves_and_revalidates_original_ranges(monkeypatch, mutation):
    group = _contrast_group()
    proposal = {"outcome": {"kind": "procedure", **deepcopy(_proposal())}}
    if mutation == "unknown_id":
        proposal["outcome"]["procedure"]["goal"]["support"] = [{"evidence_id": "not-visible"}]

    class LocalExtractor:
        def __init__(self, output_type, **kwargs):
            self.output_type = output_type
            assert "evidence_id" in kwargs["system_prompt"]

        async def output_schema(self):
            return self.output_type.model_json_schema()

        async def extract_with_usage(self, prompt):
            assert "Evidence view:" in prompt
            return SimpleNamespace(
                output=self.output_type.model_validate(proposal),
                usage=SimpleNamespace(model_dump=lambda **kwargs: {}),
            )

    monkeypatch.setattr(c, "Extractor", LocalExtractor)
    loop_thread = threading.get_ident()
    projection_threads = []
    project = c.project_episode

    def tracked_projection(*args, **kwargs):
        projection_threads.append(threading.get_ident())
        return project(*args, **kwargs)

    monkeypatch.setattr(c, "project_episode", tracked_projection)
    if mutation == "unknown_id":
        with pytest.raises(ValueError, match="unknown or empty evidence ID"):
            await c.propose_conditional_procedure(group)
        return
    result = await c.propose_conditional_procedure(group)
    assert result.receipt["status"] == "proposed"
    assert projection_threads and loop_thread not in projection_threads
    assert result.candidate is not None
    assert not c.validate_candidate_content_agreement(result.candidate, group=group)
    if mutation == "coverage_hash":
        candidate = deepcopy(result.candidate)
        candidate.metadata[c.METADATA_KEY]["build_receipt"]["projection"]["coverage_sha256"] = (
            "0" * 64
        )
        assert c.validate_candidate_content_agreement(candidate, group=group)
    elif mutation == "outside_visible":
        draft = result.proposal.procedure.model_copy(deep=True)
        draft.goal.support[:] = [c.SupportRef(episode_id="episode-0", start_byte=0, end_byte=1)]
        receipt = deepcopy(result.receipt)
        receipt["output_sha256"] = c._digest(
            c._canonical(c.ProcedureProposal(procedure=draft).model_dump(mode="json"))
        )
        candidate = c._candidate(group, draft, receipt)
        assert c.validate_candidate_content_agreement(candidate, group=group)
    else:
        spans = result.candidate.metadata[c.METADATA_KEY]["spans"]
        assert {span["episode_id"] for span in spans} == {"episode-0", "episode-1"}
        for span in spans:
            artifact = group.episodes[int(span["episode_id"][-1])].artifact
            assert (
                hashlib.sha256(artifact[span["start_byte"] : span["end_byte"]]).hexdigest()
                == span["slice_sha256"]
            )


def test_projection_receipt_is_stable_across_hash_seeds() -> None:
    script = (
        "import dataclasses,hashlib,json,sys;"
        "from sibyl_core.tasks.episode_evidence import project_episode;"
        "result=project_episode('source',sys.stdin.buffer.read(),prefix='s0');"
        "print(hashlib.sha256(json.dumps(dataclasses.asdict(result),sort_keys=True).encode()).hexdigest())"
    )
    results = []
    for seed in ("1", "123"):
        process = subprocess.run(
            [sys.executable, "-c", script],
            input=json.dumps(_episode()).encode(),
            capture_output=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        results.append(process.stdout)
    assert results[0] == results[1]
