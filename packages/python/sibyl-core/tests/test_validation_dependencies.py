"""Execution retirement closes dependent receipts without retiring their evidence."""

import asyncio
import copy
import json
import os

import pytest

from sibyl_core.services import validation_execution as owner
from sibyl_core.services.validation_dependencies import (
    dependency_closure,
    dependency_reference,
)
from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_review import review_digest
from tests.test_validation_progress_codec import (
    candidate as candidate,
)
from tests.test_validation_progress_codec import (
    citations as citations,
)
from tests.test_validation_progress_codec import (
    content_store as content_store,
)
from tests.test_validation_progress_codec import (
    historical as historical,
)
from tests.test_validation_progress_codec import (
    prepared as prepared,
)
from tests.test_validation_progress_codec import (
    private_journal as private_journal,
)
from tests.test_validation_progress_codec import (
    progress as progress,
)
from tests.test_validation_progress_codec import (
    sources as sources,
)


async def seed(historical):
    prior, _, _, _ = historical
    rows = await owner._query(
        "CREATE memory_validation_executions CONTENT $row RETURN AFTER;",
        row={
            **prior,
            "source_ids": ["parent"],
            "claim_id": "prior",
            "usage_json": canonical(json.loads(prior["result_json"])["usage"]),
        },
    )
    return rows[0]


def request_for(prior, parent):
    return {
        "org": "org",
        "principal": "owner",
        "parent": parent,
        "policy": "{}",
        "input": json.loads(prior["request_json"])["input"],
        "source_bindings": [{"source_id": parent, "incarnation": "initial", "generation": 1}],
        "execution_dependencies": [dependency_reference(prior).model_dump(mode="json")],
    }


async def child(prior, parent, additional=()):
    request = request_for(prior, parent)
    request["execution_dependencies"].extend(
        dependency_reference(row).model_dump(mode="json") for row in additional
    )
    execution = ValidationExecution(review_digest(request), "org", "owner")
    assert await execution.begin(
        parent_id=parent, source_ids=[parent], policy="{}", request=request
    )
    rows = await owner._query(
        "UPDATE memory_validation_executions SET state='returned', result_json=$result, "
        "usage_json=$usage WHERE uuid=$uuid RETURN AFTER;",
        uuid=execution.id,
        result=prior["result_json"],
        usage=prior["usage_json"],
    )
    return rows[0]


@pytest.mark.parametrize("retire", ["purge", "delete"])
async def test_dependency_retirement_clears_transitive_outputs(
    historical, content_store, private_journal, retire
):
    root = await seed(historical)
    first = await child(root, "first")
    second = await child(first, "second")
    assert first["dependency_ids"] == [root["uuid"]]
    assert second["dependency_ids"] == sorted([root["uuid"], first["uuid"]])
    assert second["source_ids"] == ["second"]
    exact = await owner._query(
        "SELECT * FROM memory_validation_executions WHERE organization_id=$org "
        "AND principal_id=$principal AND uuid=$uuid;",
        org="org",
        principal="owner",
        uuid=second["uuid"],
    )
    assert len(exact) == 1 and exact[0]["uuid"] == second["uuid"]
    indexed = await owner._query(
        "SELECT uuid FROM memory_validation_executions WHERE organization_id=$org "
        "AND principal_id=$principal AND $root IN dependency_ids AND purged=false;",
        org="org",
        principal="owner",
        root=root["uuid"],
    )
    scanned = await owner._query(
        "SELECT uuid FROM memory_validation_executions WITH NOINDEX WHERE organization_id=$org "
        "AND principal_id=$principal AND $root IN dependency_ids AND purged=false;",
        org="org",
        principal="owner",
        root=root["uuid"],
    )
    assert (
        sorted(row["uuid"] for row in indexed)
        == sorted(row["uuid"] for row in scanned)
        == sorted([first["uuid"], second["uuid"]])
    )
    if retire == "delete":
        await owner._query(
            "DELETE memory_validation_executions WHERE uuid=$uuid;", uuid=root["uuid"]
        )
    else:
        await owner._query(
            "UPDATE memory_validation_executions SET purged=true, result_json=NONE, "
            "recovery_key=NONE WHERE uuid=$uuid;",
            uuid=root["uuid"],
        )
    for prior in (first, second):
        current = await ValidationExecution(prior["uuid"], "org", "owner").load()
        assert current["purged"] is True
        assert current.get("result_json") is None
        assert current.get("recovery_key") is None
        assert current["usage_json"] == prior["usage_json"]
    from sibyl_core.migrate.validation_receipt_archive import capture

    rows = await owner._query("SELECT * FROM memory_validation_executions;")
    assert all(entry["status"] == "purged" for entry in capture(rows)["executions"])


async def test_dependency_inventory_archive_and_tamper(historical, content_store, private_journal):
    from sibyl_core.migrate.validation_receipt_archive import capture, prepare

    root = await seed(historical)
    first = await child(root, "first")
    second = await child(first, "second")
    rows = [second, root, first]
    section = capture(rows)
    assert prepare(section, rows) == []
    for change in ("missing", "closure", "result", "owner"):
        broken = copy.deepcopy(rows)
        if change == "missing":
            broken.pop(1)
        elif change == "closure":
            broken[0]["dependency_ids"].remove(root["uuid"])
        elif change == "result":
            broken[1]["result_json"] += " "
        else:
            broken[1]["principal_id"] = "other"
        with pytest.raises(ValueError):
            prepare(section, broken)
    with pytest.raises(Exception, match="immutable"):
        await owner._query(
            "UPDATE memory_validation_executions SET dependency_ids=[] WHERE uuid=$uuid;",
            uuid=second["uuid"],
        )


async def test_dependency_refuses_self_cycle_before_create(historical):
    prior, _, _, _ = historical
    request = request_for(prior, "first")
    with pytest.raises(ValueError, match="cycle"):
        dependency_closure(
            request,
            {prior["uuid"]: prior},
            execution_id=prior["uuid"],
            org="org",
            principal="owner",
        )


async def test_dependency_begin_native_purge_race(
    historical, content_store, private_journal, monkeypatch
):
    if not os.environ.get("SIBYL_OPERATIONAL_TEST_URL"):
        pytest.skip("Requires independent native transactions")
    root = await seed(historical)
    request = request_for(root, "first")
    execution = ValidationExecution(review_digest(request), "org", "owner")
    original = owner._query
    entered = asyncio.Event()

    async def delayed(query, **params):
        if "CREATE $key CONTENT $row" in query:
            query = query.replace(
                "UPDATE memory_validation_executions",
                "SLEEP 1s; UPDATE memory_validation_executions",
                1,
            )
            entered.set()
        return await original(query, **params)

    monkeypatch.setattr(owner, "_query", delayed)
    pending = asyncio.create_task(
        execution.begin(parent_id="first", source_ids=["first"], policy="{}", request=request)
    )
    await entered.wait()
    await asyncio.sleep(0.3)
    await original(
        "UPDATE memory_validation_executions SET purged=true, result_json=NONE, recovery_key=NONE "
        "WHERE uuid=$uuid;",
        uuid=root["uuid"],
    )
    assert not pending.done()
    with pytest.raises(Exception, match=r"(?i)conflict|dependency changed"):
        await pending
    assert await execution.load() is None
    assert (await ValidationExecution(root["uuid"], "org", "owner").load())["purged"] is True


def chain_rows(historical, count):
    prior = copy.deepcopy(historical[0])
    prior.update(
        source_ids=["parent"],
        dependency_ids=[],
        claim_id="prior",
        usage_json=canonical(json.loads(prior["result_json"])["usage"]),
    )
    chain = [prior]
    for index in range(count):
        request = request_for(prior, f"child-{index}")
        prior = {
            **prior,
            "uuid": review_digest(request),
            "request_sha256": review_digest(request),
            "request_json": canonical(request),
            "parent_id": request["parent"],
            "source_ids": [request["parent"]],
            "dependency_ids": sorted([prior["uuid"], *prior["dependency_ids"]]),
        }
        chain.append(prior)
    return chain


async def test_dependency_long_archive_is_iterative_and_order_independent(historical):
    from sibyl_core.migrate.validation_receipt_archive import capture, prepare

    chain = chain_rows(historical, 1050)
    section = capture(list(reversed(chain)))
    assert prepare(section, chain) == []
    assert len(section["executions"]) == 1051


async def test_dependency_long_native_purge(historical, content_store):
    if not os.environ.get("SIBYL_OPERATIONAL_TEST_URL"):
        pytest.skip("Requires independent native transactions")
    from sibyl_core.migrate.validation_receipt_archive import capture

    chain = chain_rows(historical, 128)
    await owner._query("INSERT INTO memory_validation_executions $rows RETURN NONE;", rows=chain)
    await owner._query(
        "DELETE memory_validation_executions WHERE uuid=$uuid;", uuid=chain[0]["uuid"]
    )
    rows = await owner._query("SELECT * FROM memory_validation_executions;")
    assert len(rows) == 128
    assert all(
        row["purged"] is True and row.get("result_json") is None and row.get("recovery_key") is None
        for row in rows
    )
    assert all(row["usage_json"] == chain[0]["usage_json"] for row in rows)
    assert all(row["status"] == "purged" for row in capture(rows)["executions"])


@pytest.mark.parametrize("retire", ["purge", "delete"])
async def test_dependency_branching_equal_depth(historical, content_store, private_journal, retire):
    root = await seed(historical)
    left = await child(root, "left")
    right = await child(root, "right")
    join = await child(left, "join", [right])
    tail = await child(join, "tail")
    assert len(join["dependency_ids"]) == 3
    if retire == "delete":
        await owner._query(
            "DELETE memory_validation_executions WHERE uuid=$uuid;", uuid=root["uuid"]
        )
    else:
        await owner._query(
            "UPDATE memory_validation_executions SET purged=true, result_json=NONE, recovery_key=NONE WHERE uuid=$uuid;",
            uuid=root["uuid"],
        )
    for previous in (left, right, join, tail):
        row = await ValidationExecution(previous["uuid"], "org", "owner").load()
        assert row["purged"] is True and row.get("result_json") is None
        assert row["usage_json"] == previous["usage_json"]


async def test_dependency_parallel_native_roots(historical, content_store, private_journal):
    if not os.environ.get("SIBYL_OPERATIONAL_TEST_URL"):
        pytest.skip("Requires independent native transactions")
    first = await seed(historical)
    other = copy.deepcopy(first)
    other.pop("id", None)
    request = json.loads(other["request_json"])
    request["parent"] = "other-root"
    other.update(
        uuid=review_digest(request),
        request_sha256=review_digest(request),
        request_json=canonical(request),
        parent_id="other-root",
    )
    second = (
        await owner._query(
            "CREATE memory_validation_executions CONTENT $row RETURN AFTER;", row=other
        )
    )[0]
    children = [await child(first, "first-child"), await child(second, "second-child")]
    await asyncio.gather(
        *[
            owner._query("DELETE memory_validation_executions WHERE uuid=$uuid;", uuid=row["uuid"])
            for row in (first, second)
        ]
    )
    for child_row in children:
        row = await ValidationExecution(child_row["uuid"], "org", "owner").load()
        assert row["purged"] is True and row.get("result_json") is None
        assert row["usage_json"] == child_row["usage_json"]


async def test_dependency_populated_schema_upgrade_scoped_indexes(historical, content_store):
    from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
    from sibyl_core.services.content_client import surreal_content_client

    root = await seed(historical)
    expected = []
    for org, principal in (("org", "other"), ("other", "owner")):
        row = copy.deepcopy(root)
        row.pop("id", None)
        request = json.loads(row["request_json"])
        request.update(org=org, principal=principal)
        row.update(
            uuid=review_digest(request),
            request_sha256=review_digest(request),
            request_json=canonical(request),
            organization_id=org,
            principal_id=principal,
        )
        expected.extend(
            await owner._query(
                "CREATE memory_validation_executions CONTENT $row RETURN AFTER;", row=row
            )
        )
    before = await owner._query("SELECT * FROM memory_validation_executions ORDER BY uuid;")
    await owner._query(
        "DEFINE INDEX OVERWRITE memory_validation_execution_owner ON memory_validation_executions FIELDS organization_id, principal_id, parent_id;"
    )
    await owner._query("UPDATE schema_version SET version=40 WHERE name='content';")
    async with surreal_content_client() as client:
        await bootstrap_content_schema(client)
    assert await owner._query("SELECT * FROM memory_validation_executions ORDER BY uuid;") == before
    for row in (root, *expected):
        params = dict(
            org=row["organization_id"],
            principal=row["principal_id"],
            parent_identity=root["parent_id"],
        )
        rows = await owner._query(
            "SELECT * FROM memory_validation_executions WHERE organization_id=$org AND principal_id=$principal AND parent_id=$parent_identity;",
            **params,
        )
        assert len(rows) == 1 and rows[0]["uuid"] == row["uuid"]
        plan = await owner._query(
            "SELECT * FROM memory_validation_executions WHERE organization_id=$org AND principal_id=$principal AND parent_id=$parent_identity EXPLAIN;",
            **params,
        )
        assert_index(plan, "memory_validation_execution_owner")
        exact = await owner._query(
            "SELECT * FROM memory_validation_executions WHERE organization_id=$org AND principal_id=$principal AND uuid=$uuid EXPLAIN;",
            org=params["org"],
            principal=params["principal"],
            uuid=row["uuid"],
        )
        assert_index(exact, "memory_validation_execution_uuid")


async def test_dependency_native_source_purge_deepest_first(historical, content_store):
    if not os.environ.get("SIBYL_OPERATIONAL_TEST_URL"):
        pytest.skip("Requires independent native transactions")
    chain = chain_rows(historical, 128)
    for row in chain:
        row["source_ids"] = ["parent", row["parent_id"]]
    await owner._query("CREATE raw_captures SET uuid='parent', organization_id='org';")
    await owner._query("INSERT INTO memory_validation_executions $rows RETURN NONE;", rows=chain)
    await owner._query("DELETE raw_captures WHERE uuid='parent' AND organization_id='org';")
    rows = await owner._query("SELECT * FROM memory_validation_executions;")
    assert len(rows) == 129
    assert all(
        row["purged"] is True and row.get("result_json") is None and row.get("recovery_key") is None
        for row in rows
    )
    assert all(row["usage_json"] == chain[0]["usage_json"] for row in rows)


def assert_index(plan, expected):
    pending = list(plan)
    seen = []
    while pending:
        node = pending.pop()
        pending.extend(node.get("children", []))
        assert node.get("operator") != "TableScan" and node.get("operation") != "Iterate Table", (
            plan
        )
        if node.get("operation") == "Iterate Index":
            seen.append(node["detail"]["plan"]["index"])
        elif node.get("operator") == "IndexScan":
            seen.append(node["attributes"]["index"])
    assert seen == [expected], plan


async def test_dependency_legacy_progress_upgrade_and_archive(
    historical, progress, content_store, private_journal
):
    from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
    from sibyl_core.migrate.validation_receipt_archive import capture, prepare
    from sibyl_core.services.content_client import surreal_content_client
    from sibyl_core.services.validation_dependencies import normalize_legacy_dependencies
    from sibyl_core.services.validation_result_codec import encode_validation_result

    prior, binding, result, _ = historical
    root = {
        **prior,
        "source_ids": ["parent"],
        "claim_id": "old",
        "usage_json": canonical(json.loads(prior["result_json"])["usage"]),
    }
    request = {
        "org": "org",
        "principal": "owner",
        "parent": "child",
        "policy": "{}",
        "input": progress.input_sha256,
        "source_bindings": [{"source_id": "child", "incarnation": "initial", "generation": 1}],
        "progress_history": binding.model_dump(mode="json"),
    }
    descendant = {
        **root,
        "uuid": review_digest(request),
        "request_sha256": review_digest(request),
        "request_json": canonical(request),
        "parent_id": "child",
        "source_ids": ["child"],
        "result_json": canonical(encode_validation_result(result)),
        "usage_json": canonical(encode_validation_result(result)["usage"]),
    }
    original = [descendant, root]
    section = capture(original)
    assert prepare(section, original) == []
    normalized = normalize_legacy_dependencies(original)
    assert normalized[0]["dependency_ids"] == [root["uuid"]]
    assert "dependency_ids" not in original[0]
    for event in ("retain_validation_dependencies", "purge_validation_dependents"):
        await owner._query(f"REMOVE EVENT {event} ON memory_validation_executions;")
    await owner._query("REMOVE FIELD dependency_ids ON memory_validation_executions;")
    await owner._query("INSERT INTO memory_validation_executions $rows RETURN NONE;", rows=original)
    await owner._query("UPDATE schema_version SET version=40 WHERE name='content';")
    async with surreal_content_client() as client:
        await bootstrap_content_schema(client)
    current = await ValidationExecution(descendant["uuid"], "org", "owner").load()
    assert current["dependency_ids"] == [root["uuid"]]
    assert current["request_json"] == descendant["request_json"]
    assert current["result_json"] == descendant["result_json"]
    assert current["usage_json"] == descendant["usage_json"]
    await owner._query("DELETE memory_validation_executions WHERE uuid=$uuid;", uuid=root["uuid"])
    retired = await ValidationExecution(descendant["uuid"], "org", "owner").load()
    assert retired["purged"] is True and retired.get("result_json") is None
    assert capture([retired])["executions"][0]["status"] == "purged"
