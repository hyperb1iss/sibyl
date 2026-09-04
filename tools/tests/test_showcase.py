from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tools.showcase import seed
from tools.showcase.fixtures import (
    KNOWLEDGE,
    PROJECTS,
    SOURCES,
    TASKS,
    ProjectFixture,
    SourceFixture,
)
from tools.showcase.seed import (
    ORG_SLUG_PATTERN,
    ShowcaseSafetyError,
    _complete_source_page,
    _expected_entity_projections,
    _expected_source_projections,
    _idempotency_key,
    build_corpus_snapshot,
    expected_entity_keys,
    expected_source_keys,
    forbidden_terms,
    load_forbidden_terms,
    require_loopback_url,
    validate_existing_data,
    validate_fixture,
)

from sibyl_cli import auth_store

TEST_FORBIDDEN_TERMS = ("blocked alpha", "blocked beta", "blocked gamma")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3334",
        "http://127.0.0.1:3334/",
        "http://[::1]:3334",
    ],
)
def test_loopback_showcase_urls_are_allowed(url: str) -> None:
    assert require_loopback_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "https://sibyl.example.com",
        "http://192.168.1.8:3334",
        "http://localhost:3334?organization=real",
        "http://user:secret@localhost:3334",
    ],
)
def test_nonlocal_or_ambiguous_showcase_urls_are_refused(url: str) -> None:
    with pytest.raises(ShowcaseSafetyError):
        require_loopback_url(url)


def test_showcase_slug_collision_is_not_possible_through_path_syntax() -> None:
    assert ORG_SLUG_PATTERN.fullmatch("sibyl-showcase")
    assert ORG_SLUG_PATTERN.fullmatch("../private") is None


def test_committed_showcase_fixture_is_safe_and_unique() -> None:
    validate_fixture(TEST_FORBIDDEN_TERMS)

    assert len(expected_entity_keys()) == len(PROJECTS) + len(TASKS) + len(KNOWLEDGE)
    assert len(expected_source_keys()) == len(SOURCES)


def test_forbidden_terms_traverse_dataclass_fields() -> None:
    poisoned = ProjectFixture(
        key="private",
        name="Internal blocked alpha plan",
        description="Do not publish",
        languages=(),
        technologies=(),
    )

    assert forbidden_terms(poisoned, TEST_FORBIDDEN_TERMS) == {"blocked alpha"}


def test_forbidden_terms_include_source_fixtures() -> None:
    poisoned = SourceFixture(
        name="Documentation",
        url="https://docs.example.com",
        source_type="documentation",
        description="Internal blocked beta notes",
    )

    assert forbidden_terms(poisoned, TEST_FORBIDDEN_TERMS) == {"blocked beta"}


def test_local_filter_config_is_required(tmp_path: Path) -> None:
    with pytest.raises(ShowcaseSafetyError, match="Create the ignored screenshot filter"):
        load_forbidden_terms(tmp_path / "missing.json")


def test_local_filter_config_loads_normalized_terms(tmp_path: Path) -> None:
    config = tmp_path / "filters.json"
    config.write_text('{"forbidden_terms": ["  Blocked Alpha  "]}', encoding="utf-8")

    assert load_forbidden_terms(config) == ("blocked alpha",)


def _validate_existing_data(
    entities: list[dict[str, object]],
    sources: list[dict[str, object]],
    *,
    require_complete: bool,
) -> None:
    validate_existing_data(
        entities,
        sources,
        forbidden_public_terms=TEST_FORBIDDEN_TERMS,
        require_complete=require_complete,
    )


def test_existing_foreign_entity_is_refused_before_seeding() -> None:
    with pytest.raises(ShowcaseSafetyError, match="unexpected entities"):
        _validate_existing_data(
            [
                {
                    "entity_type": "project",
                    "name": "Unrelated workspace",
                    "tags": [],
                    "metadata": {},
                }
            ],
            [],
            require_complete=False,
        )


def test_existing_forbidden_text_is_refused_even_on_expected_entity() -> None:
    project = PROJECTS[0]
    with pytest.raises(ShowcaseSafetyError, match="forbidden private content") as caught:
        _validate_existing_data(
            [
                {
                    "entity_type": "project",
                    "name": project.name,
                    "description": "Internal blocked alpha notes",
                    "tags": ["sibyl-showcase"],
                    "metadata": {"capture_mode": "showcase"},
                }
            ],
            [],
            require_complete=False,
        )
    assert TEST_FORBIDDEN_TERMS[0] not in str(caught.value).casefold()


def test_complete_validation_reports_missing_fixture_rows() -> None:
    with pytest.raises(ShowcaseSafetyError, match="missing entities"):
        _validate_existing_data([], [], require_complete=True)


def _complete_corpus() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    project_ids = {fixture.key: f"project-{fixture.key}" for fixture in PROJECTS}
    project_ids_by_name = {fixture.name: project_ids[fixture.key] for fixture in PROJECTS}
    entities = []
    for key, projection in _expected_entity_projections(project_ids).items():
        entity_id = project_ids_by_name.get(key[1], f"entity-{len(entities)}")
        entities.append({"id": entity_id, **projection})
    sources = list(_expected_source_projections().values())
    return entities, sources


def test_complete_showcase_corpus_is_accepted() -> None:
    entities, sources = _complete_corpus()

    _validate_existing_data(entities, sources, require_complete=True)


def test_truncated_source_page_is_refused() -> None:
    assert _complete_source_page({"sources": [{"id": "one"}], "total": 1}) == [{"id": "one"}]
    with pytest.raises(ShowcaseSafetyError, match="over 200 sources"):
        _complete_source_page({"sources": [{"id": "one"}], "total": 2})


def test_corpus_snapshot_is_deterministic_and_binds_generated_ids() -> None:
    entities, sources = _complete_corpus()
    expected = build_corpus_snapshot(entities, sources)

    assert build_corpus_snapshot(reversed(entities), reversed(sources)) == expected
    entities[0]["id"] = "replacement-project"
    assert build_corpus_snapshot(entities, sources) != expected


def test_expected_entity_with_drifted_content_is_refused() -> None:
    entities, _ = _complete_corpus()
    entities[0]["description"] = "Confidential customer roadmap"

    with pytest.raises(ShowcaseSafetyError, match="drifted entities"):
        _validate_existing_data([entities[0]], [], require_complete=False)


@pytest.mark.parametrize(
    ("entity_type", "field", "value"),
    [
        ("decision", "source_file", "/Users/example/Customer-Roadmap.md"),
        ("project", "repository_url", "https://github.com/customer/private"),
        ("task", "assignees", ["private-customer@example.com"]),
    ],
)
def test_screenshot_visible_entity_fields_cannot_hide_outside_the_fixture(
    entity_type: str,
    field: str,
    value: object,
) -> None:
    entities, sources = _complete_corpus()
    entity = next(row for row in entities if row["entity_type"] == entity_type)
    if field == "source_file":
        entity[field] = value
    else:
        metadata = entity["metadata"]
        assert isinstance(metadata, dict)
        metadata[field] = value

    with pytest.raises(ShowcaseSafetyError, match="drifted entities"):
        _validate_existing_data(entities, sources, require_complete=True)


def test_expected_source_with_drifted_description_is_refused() -> None:
    _, sources = _complete_corpus()
    sources[0]["description"] = "Confidential customer roadmap"

    with pytest.raises(ShowcaseSafetyError, match="drifted sources"):
        _validate_existing_data([], [sources[0]], require_complete=False)


def test_source_error_text_cannot_hide_outside_the_fixture() -> None:
    entities, sources = _complete_corpus()
    sources[0]["last_error"] = "Failed while crawling a blocked alpha repository"

    with pytest.raises(ShowcaseSafetyError, match="forbidden private content"):
        _validate_existing_data(entities, sources, require_complete=True)


def test_duplicate_expected_rows_cannot_hide_behind_set_equality() -> None:
    entities, sources = _complete_corpus()
    entities.append(dict(entities[0]))

    with pytest.raises(ShowcaseSafetyError, match="duplicate entities"):
        _validate_existing_data(entities, sources, require_complete=True)


def test_idempotency_keys_are_deterministic_and_payload_bound() -> None:
    first = _idempotency_key("task", {"name": "One"})

    assert first == _idempotency_key("task", {"name": "One"})
    assert first != _idempotency_key("task", {"name": "Two"})


@pytest.mark.asyncio
@pytest.mark.parametrize("paired_url", [None, "http://localhost:3434/api/"])
async def test_showcase_accepts_matching_or_unpaired_environment_credentials(
    monkeypatch: pytest.MonkeyPatch, paired_url: str | None
) -> None:
    monkeypatch.setenv("SIBYL_AUTH_TOKEN", "synthetic-showcase-token")
    if paired_url:
        monkeypatch.setenv("SIBYL_API_URL", paired_url)
    else:
        monkeypatch.delenv("SIBYL_API_URL", raising=False)

    assert await seed.active_cli_token("http://localhost:3434") == "synthetic-showcase-token"


@pytest.mark.asyncio
async def test_showcase_never_borrows_a_foreign_environment_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_AUTH_TOKEN", "synthetic-foreign-token")
    monkeypatch.setenv("SIBYL_API_URL", "http://localhost:3334/api")
    monkeypatch.setattr(seed.config_store, "resolve_effective_context", lambda: None)

    with pytest.raises(ShowcaseSafetyError, match="No active Sibyl CLI context"):
        await seed.active_cli_token("http://localhost:3434")


@pytest.mark.asyncio
async def test_showcase_uses_its_stored_login_when_environment_targets_another_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("SIBYL_AUTH_TOKEN", "synthetic-foreign-token")
    monkeypatch.setenv("SIBYL_API_URL", "http://localhost:3334/api")
    context = seed.config_store.create_context("showcase", "http://localhost:3434")
    monkeypatch.setattr(seed.config_store, "resolve_effective_context", lambda: context)
    monkeypatch.setattr(seed.SibylClient, "list_orgs", AsyncMock(return_value=[]))
    auth_store.set_tokens(
        "http://localhost:3434/api",
        "synthetic-stored-token",
        credential_scope="context:showcase:org:default",
    )

    assert await seed.active_cli_token("http://localhost:3434") == "synthetic-stored-token"
