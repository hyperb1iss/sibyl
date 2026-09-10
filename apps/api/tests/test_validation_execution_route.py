"""The authenticated validation adapter never accepts model authority from a body."""

from types import SimpleNamespace

import pytest

from sibyl.api.routes import memory_evals
from sibyl_core.auth import OrganizationRole
from tests.test_memory_eval_admission import eval_api as eval_api  # noqa: PLC0414


@pytest.mark.parametrize("retired", [False, True])
async def test_validation_route_re_resolves_owner(eval_api, monkeypatch, retired):
    seen = []

    async def fresh(request):
        assert request.state.auth_context is None
        seen.append(1)
        return SimpleNamespace(
            user_id="other" if retired and len(seen) > 1 else "owner",
            organization_id=eval_api.assignment.organization_id,
            org_role=OrganizationRole.OWNER,
        )

    monkeypatch.setattr(memory_evals, "get_auth_context", fresh)

    async def execute(**kwargs):
        assert kwargs["principal_id"] == "owner"
        await kwargs["authorize"]()
        await kwargs["authorize"]()
        return {"status": "no_findings", "submission": None}

    monkeypatch.setattr(
        "sibyl_core.services.procedure_validation.validate_stored_procedure", execute
    )
    response = await eval_api.client.post("/memory/eval/candidates/parent/validate")
    assert response.status_code == (403 if retired else 200)
    if not retired:
        assert response.json() == {"status": "no_findings", "submission": None}
    assert len(seen) == 2
