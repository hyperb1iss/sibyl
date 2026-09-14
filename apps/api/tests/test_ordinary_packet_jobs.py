"""Packet coverage and execution usage remain separate from source independence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sibyl.jobs import ordinary_cohorts, reflection


async def test_packet_job_accounts_every_page_and_resumes_pending_results(monkeypatch):
    manifest = {"source_id": "source", "pages": [0, 1, 2]}
    packets = [
        SimpleNamespace(binding={"manifest": manifest, "index": index}, sha256=str(index))
        for index in range(3)
    ]
    monkeypatch.setattr(
        ordinary_cohorts, "prepare_stored_source_packets", AsyncMock(return_value=packets)
    )
    monkeypatch.setattr(ordinary_cohorts, "writable_source_authority", AsyncMock())
    resumed = False

    async def propose(*args, packet_binding, **kwargs):
        index = packet_binding["index"]
        if index > 0 and not resumed:
            error = ValueError("retained incomplete stage")
            error.__dict__.update(
                execution_id=f"execution-{index}",
                execution_state="failed" if index == 1 else "returned",
            )
            raise error
        return SimpleNamespace(id=f"candidate-{index}"), f"execution-{index}"

    monkeypatch.setattr(ordinary_cohorts, "propose_stored_cohort", propose)
    first = await ordinary_cohorts._reflect_packet_source("org", "owner", "source")
    assert first["source_pass_complete"] is False
    assert [page["outcome"] for page in first["pages"]] == ["returned", "failed", "pending"]
    assert first["independent_source_count"] == 1
    assert first["packet_count"] == 3
    monkeypatch.setattr(reflection, "_reflect_dream_sources", AsyncMock(return_value=[first]))
    monkeypatch.setattr(reflection, "_drain_dream_candidates", AsyncMock(return_value=[]))
    receipt = await reflection.run_reflection_dream_cycle({}, "org")
    assert receipt["sources_scanned"] == 1
    assert receipt["failed"] == 1
    assert receipt["model_usage"]["execution_ids"] == [f"execution-{index}" for index in range(3)]
    # The durable core owns replay. This adapter retains its page manifest and
    # reports completion only after every page returns a successful terminal result.
    resumed = True
    second = await ordinary_cohorts._reflect_packet_source("org", "owner", "source")
    assert second["source_pass_complete"] is True
    assert second["manifest"] == first["manifest"]
    assert second["independent_source_count"] == 1
    assert second["candidate_ids"] == [f"candidate-{index}" for index in range(3)]
