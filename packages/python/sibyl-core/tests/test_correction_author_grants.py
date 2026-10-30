"""Correcting someone else's project capture takes project maintainer access."""

import pytest

from sibyl_core.services.memory_correction import apply_memory_correction, preview_memory_correction
from sibyl_core.services.surreal_content import get_raw_memory, remember_raw_memory
from tests.test_correction_scope_grants import correction_store as correction_store

_PROJECT = "project-a"

# Every action the correction surface accepts: each one either takes the
# capture out of recall, rewrites it, or reverses a lifecycle decision.
_ACTIONS = (
    "delete",
    "hide",
    "mark_duplicate",
    "mark_sensitive",
    "mark_stale",
    "mark_wrong",
    "redact",
    "restore",
    "revise",
    "supersede",
)


async def _capture(store, source_id, principal_id, **kwargs):
    return await remember_raw_memory(
        organization_id=store.org,
        principal_id=principal_id,
        source_id=source_id,
        raw_content=f"Advice from {source_id}",
        embedding_provider=None,
        **kwargs,
    )


async def _project_capture(store, source_id, principal_id):
    return await _capture(
        store, source_id, principal_id, memory_scope="project", scope_key=_PROJECT
    )


async def _correction_kwargs(store, target, action, principal_id, maintained):
    kwargs = {
        "organization_id": store.org,
        "source_id": target.id,
        "principal_id": principal_id,
        "action": action,
        "reason": "probe",
        "accessible_projects": {_PROJECT},
        "writable_projects": {_PROJECT},
        "maintained_projects": maintained,
    }
    if action in {"supersede", "mark_duplicate"}:
        reference = await _project_capture(store, f"{target.source_id}-ref", principal_id)
        key = "replacement_source_id" if action == "supersede" else "duplicate_of_source_id"
        kwargs[key] = reference.id
    if action == "revise":
        kwargs["revised_content"] = "Rewritten advice"
    return kwargs


@pytest.mark.parametrize("action", _ACTIONS)
async def test_contributor_cannot_correct_another_principals_project_capture(
    correction_store, action
):
    store = correction_store
    target = await _project_capture(store, f"maintainer-{action}", "maintainer")
    kwargs = await _correction_kwargs(store, target, action, "contributor", set())

    preview = await preview_memory_correction(**kwargs)
    assert not preview.allowed
    assert preview.reason == "project_maintainer_required"

    result = await apply_memory_correction(**kwargs)
    assert not result.applied
    assert result.preview.reason == "project_maintainer_required"
    stored = await get_raw_memory(organization_id=store.org, memory_id=target.id)
    assert stored is not None
    assert stored.revision == target.revision
    assert stored.raw_content == target.raw_content
    assert "memory_lifecycle" not in stored.metadata


@pytest.mark.parametrize("action", _ACTIONS)
async def test_contributor_can_correct_their_own_project_capture(correction_store, action):
    store = correction_store
    target = await _project_capture(store, f"own-{action}", "contributor")
    kwargs = await _correction_kwargs(store, target, action, "contributor", set())

    result = await apply_memory_correction(**kwargs)
    assert result.applied, result.preview.reason
    stored = await get_raw_memory(organization_id=store.org, memory_id=target.id)
    assert stored is not None and stored.revision > target.revision


@pytest.mark.parametrize("action", _ACTIONS)
async def test_maintainer_can_correct_another_principals_project_capture(correction_store, action):
    store = correction_store
    target = await _project_capture(store, f"contributed-{action}", "contributor")
    kwargs = await _correction_kwargs(store, target, action, "maintainer", {_PROJECT})

    result = await apply_memory_correction(**kwargs)
    assert result.applied, result.preview.reason
    stored = await get_raw_memory(organization_id=store.org, memory_id=target.id)
    assert stored is not None and stored.revision > target.revision


async def test_maintainer_grant_on_another_project_does_not_carry_over(correction_store):
    store = correction_store
    target = await _project_capture(store, "elsewhere", "maintainer")
    kwargs = await _correction_kwargs(store, target, "delete", "contributor", {"project-b"})

    result = await apply_memory_correction(**kwargs)
    assert not result.applied
    assert result.preview.reason == "project_maintainer_required"


async def test_missing_maintainer_grants_fail_closed(correction_store):
    store = correction_store
    target = await _project_capture(store, "ungranted", "maintainer")
    kwargs = await _correction_kwargs(store, target, "delete", "contributor", None)
    del kwargs["maintained_projects"]

    result = await apply_memory_correction(**kwargs)
    assert not result.applied
    assert result.preview.reason == "project_maintainer_required"


@pytest.mark.parametrize("maintained", [set(), {_PROJECT}])
async def test_private_captures_still_answer_only_to_their_principal(correction_store, maintained):
    store = correction_store
    theirs = await _capture(store, "their-private", "maintainer")
    mine = await _capture(store, "my-private", "contributor")
    common = {
        "organization_id": store.org,
        "action": "delete",
        "reason": "probe",
        "accessible_projects": {_PROJECT},
        "writable_projects": {_PROJECT},
        "maintained_projects": maintained,
    }

    denied = await apply_memory_correction(
        source_id=theirs.id, principal_id="contributor", **common
    )
    assert not denied.applied
    assert denied.preview.reason == "principal_mismatch"

    allowed = await apply_memory_correction(source_id=mine.id, principal_id="contributor", **common)
    assert allowed.applied, allowed.preview.reason
