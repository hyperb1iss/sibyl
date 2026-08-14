"""Exit-code contract for CLI failure paths.

Hooks, CI jobs, and agents branch on `$?`. A refused write, an expired
session, and an unreachable server must all exit nonzero; an empty result
set is a successful answer and must not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli import entity, epic, project, task
from sibyl_cli.client import SibylClientError
from sibyl_cli.main import app as main_app

TASK_ID = "13364346-8475-4664-8b52-eb963af2fda7"
REFUSED: dict[str, Any] = {"success": False, "message": "Write refused"}


def _client(**methods: Any) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    for name, value in methods.items():
        setattr(client, name, AsyncMock(return_value=value))
    return client


@pytest.fixture
def resolved_ids():
    with (
        patch("sibyl_cli.task._resolve_task_id", AsyncMock(side_effect=lambda _c, i: i)),
        patch("sibyl_cli.epic._resolve_epic_id", AsyncMock(side_effect=lambda _c, i: i)),
    ):
        yield


def test_task_start_exits_nonzero_when_the_write_is_refused(resolved_ids: None) -> None:
    with patch("sibyl_cli.task.get_client", return_value=_client(start_task=REFUSED)):
        result = CliRunner().invoke(task.app, ["start", TASK_ID])

    assert result.exit_code == 1


def test_task_complete_exits_nonzero_when_the_write_is_refused(resolved_ids: None) -> None:
    with patch("sibyl_cli.task.get_client", return_value=_client(complete_task=REFUSED)):
        result = CliRunner().invoke(task.app, ["complete", TASK_ID])

    assert result.exit_code == 1


def test_task_update_exits_nonzero_when_the_write_is_refused(resolved_ids: None) -> None:
    with patch("sibyl_cli.task.get_client", return_value=_client(update_task=REFUSED)):
        result = CliRunner().invoke(task.app, ["update", TASK_ID, "--priority", "high"])

    assert result.exit_code == 1


def test_task_create_exits_nonzero_when_the_write_is_refused() -> None:
    with patch("sibyl_cli.task.get_client", return_value=_client(create_task=REFUSED)):
        result = CliRunner().invoke(
            task.app,
            ["create", "--title", "Refused", "--project", "project_123456789abc"],
        )

    assert result.exit_code == 1


def test_epic_start_exits_nonzero_when_the_write_is_refused(resolved_ids: None) -> None:
    with patch("sibyl_cli.epic.get_client", return_value=_client(update_entity=REFUSED)):
        result = CliRunner().invoke(epic.app, ["start", "epic_123"])

    assert result.exit_code == 1


def test_entity_create_exits_nonzero_when_the_write_is_refused() -> None:
    with patch("sibyl_cli.entity.get_client", return_value=_client(create_entity={})):
        result = CliRunner().invoke(entity.app, ["create", "--name", "Refused", "--type", "note"])

    assert result.exit_code == 1


def test_project_create_exits_nonzero_when_the_write_is_refused() -> None:
    with patch("sibyl_cli.project.get_client", return_value=_client(create_entity={})):
        result = CliRunner().invoke(project.app, ["create", "--name", "Refused"])

    assert result.exit_code == 1


def test_task_start_exits_nonzero_on_an_expired_session(resolved_ids: None) -> None:
    client = _client()
    client.start_task = AsyncMock(
        side_effect=SibylClientError("Not authenticated", status_code=401)
    )

    with patch("sibyl_cli.task.get_client", return_value=client):
        result = CliRunner().invoke(task.app, ["start", TASK_ID])

    assert result.exit_code == 1


def test_task_start_exits_nonzero_when_the_server_is_unreachable(resolved_ids: None) -> None:
    client = _client()
    client.start_task = AsyncMock(
        side_effect=SibylClientError("Cannot connect to Sibyl server at http://localhost:3334")
    )

    with patch("sibyl_cli.task.get_client", return_value=client):
        result = CliRunner().invoke(task.app, ["start", TASK_ID])

    assert result.exit_code == 1


@pytest.mark.parametrize("status_code", [403, 409, 500, 503])
def test_task_start_exits_nonzero_on_server_error_envelopes(
    resolved_ids: None,
    status_code: int,
) -> None:
    client = _client()
    client.start_task = AsyncMock(
        side_effect=SibylClientError("Request failed", status_code=status_code)
    )

    with patch("sibyl_cli.task.get_client", return_value=client):
        result = CliRunner().invoke(task.app, ["start", TASK_ID])

    assert result.exit_code == 1


def test_note_alias_exits_nonzero_when_the_write_is_refused() -> None:
    client = _client(create_note={})

    with (
        patch("sibyl_cli.main.get_client", return_value=client),
        patch("sibyl_cli.main.resolve_id_prefix", AsyncMock(side_effect=lambda _c, i, **_k: i)),
    ):
        result = CliRunner().invoke(main_app, ["note", TASK_ID, "body"])

    assert result.exit_code == 1


def test_local_stop_exits_nonzero_when_compose_fails(tmp_path: Path) -> None:
    from sibyl_cli import local

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n")
    failed = MagicMock()
    failed.returncode = 1

    with (
        patch.object(local, "SIBYL_LOCAL_COMPOSE", compose_file),
        patch("sibyl_cli.local.is_running", return_value=True),
        patch("sibyl_cli.local.run_compose", return_value=failed),
    ):
        result = CliRunner().invoke(local.app, ["stop"])

    assert result.exit_code == 1
    assert "Failed to stop Sibyl" in result.stdout


def test_an_empty_result_set_still_exits_zero() -> None:
    """No matches is an answer, not a failure. Scripts must not treat it as one."""
    client = _client(explore={"entities": [], "total": 0})

    with (
        patch("sibyl_cli.task.get_client", return_value=client),
        patch("sibyl_cli.task.resolve_project_from_cwd", return_value="project_123"),
    ):
        result = CliRunner().invoke(task.app, ["list"])

    assert result.exit_code == 0


def test_the_401_remediation_names_commands_that_exist() -> None:
    """The most-hit error path pointed at `sibyl auth signup`, which was never registered."""
    from sibyl_cli import auth

    client = _client()
    client.get = AsyncMock(side_effect=SibylClientError("Not authenticated", status_code=401))

    with patch("sibyl_cli.main.get_client", return_value=client):
        result = CliRunner().invoke(main_app, ["health"])

    assert result.exit_code == 1
    suggested = {
        line.split("sibyl auth ", 1)[1].split()[0]
        for line in result.stdout.splitlines()
        if "sibyl auth " in line
    }
    registered = {command.name for command in auth.app.registered_commands if command.name} | {
        group.name for group in auth.app.registered_groups if group.name
    }

    assert suggested
    assert suggested <= registered


@pytest.mark.parametrize(
    ("command", "method"),
    [
        (["start", TASK_ID], "start_task"),
        (["block", TASK_ID, "--reason", "waiting on review"], "block_task"),
        (["unblock", TASK_ID], "unblock_task"),
        (["review", TASK_ID], "submit_review"),
        (["complete", TASK_ID], "complete_task"),
        (["update", TASK_ID, "--priority", "high"], "update_task"),
    ],
)
def test_json_output_still_exits_nonzero_on_a_refused_write(
    resolved_ids: None,
    command: list[str],
    method: str,
) -> None:
    """--json is what agents invoke, so the refusal has to reach $? on that path too."""
    import json

    with patch("sibyl_cli.task.get_client", return_value=_client(**{method: REFUSED})):
        result = CliRunner().invoke(task.app, [*command, "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["success"] is False


def test_json_output_still_exits_zero_when_the_write_lands(resolved_ids: None) -> None:
    import json

    accepted = {"success": True, "data": {}}

    with patch("sibyl_cli.task.get_client", return_value=_client(start_task=accepted)):
        result = CliRunner().invoke(task.app, ["start", TASK_ID, "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["success"] is True


def test_crawl_ingest_exits_nonzero_when_the_crawl_is_refused() -> None:
    """The sibling of `source add` in the same module, missed by the audit's list."""
    from sibyl_cli import crawl

    refused = {"status": "failed", "message": "crawler exploded"}

    with patch("sibyl_cli.crawl_shared.get_client", return_value=_client(start_crawl=refused)):
        result = CliRunner().invoke(crawl.app, ["ingest", "source_123"])

    assert result.exit_code == 1
    assert "Crawl failed" in result.stdout


def test_crawl_ingest_json_exits_nonzero_when_the_crawl_is_refused() -> None:
    from sibyl_cli import crawl

    refused = {"status": "failed", "message": "crawler exploded"}

    with patch("sibyl_cli.crawl_shared.get_client", return_value=_client(start_crawl=refused)):
        result = CliRunner().invoke(crawl.app, ["ingest", "source_123", "--json"])

    assert result.exit_code == 1


def test_crawl_ingest_exits_zero_when_the_crawl_queues() -> None:
    from sibyl_cli import crawl

    queued = {"status": "queued", "message": "Crawl queued"}

    with patch("sibyl_cli.crawl_shared.get_client", return_value=_client(start_crawl=queued)):
        result = CliRunner().invoke(crawl.app, ["ingest", "source_123"])

    assert result.exit_code == 0


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_health_exits_nonzero_on_an_unhealthy_server(json_flag: list[str]) -> None:
    """Table and --json must agree about the same response."""
    client = _client()
    client.get = AsyncMock(return_value={"status": "unhealthy", "server_name": "stub"})

    with patch("sibyl_cli.main.get_client", return_value=client):
        result = CliRunner().invoke(main_app, ["health", *json_flag])

    assert result.exit_code == 1


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_health_exits_zero_on_a_healthy_server(json_flag: list[str]) -> None:
    client = _client()
    client.get = AsyncMock(return_value={"status": "healthy", "server_name": "stub"})

    with patch("sibyl_cli.main.get_client", return_value=client):
        result = CliRunner().invoke(main_app, ["health", *json_flag])

    assert result.exit_code == 0


def test_entity_list_exits_nonzero_on_an_invalid_type() -> None:
    result = CliRunner().invoke(entity.app, ["list", "--type", "not-a-real-type"])

    assert result.exit_code == 1
    assert "Invalid entity type" in result.stdout


def test_entity_create_exits_nonzero_on_an_invalid_type() -> None:
    result = CliRunner().invoke(
        entity.app, ["create", "--name", "x", "--type", "not-a-real-type"]
    )

    assert result.exit_code == 1


def test_task_update_exits_nonzero_when_no_fields_are_given(resolved_ids: None) -> None:
    with patch("sibyl_cli.task.get_client", return_value=_client()):
        result = CliRunner().invoke(task.app, ["update", TASK_ID])

    assert result.exit_code == 1
    assert "No fields to update" in result.stdout


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_crawl_link_graph_agrees_across_renderers_on_an_error(json_flag: list[str]) -> None:
    """Fixing one renderer and not the other is how the health blocker happened."""
    from sibyl_cli import crawl

    failed = {"status": "error", "error": "extraction blew up"}

    with patch("sibyl_cli.crawl.get_client", return_value=_client(link_graph=failed)):
        result = CliRunner().invoke(crawl.app, ["link-graph", "src_1", *json_flag])

    assert result.exit_code == 1


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_crawl_link_graph_exits_zero_when_it_completes(json_flag: list[str]) -> None:
    from sibyl_cli import crawl

    done = {"status": "complete", "entities_created": 0, "relationships_created": 0}

    with patch("sibyl_cli.crawl.get_client", return_value=_client(link_graph=done)):
        result = CliRunner().invoke(crawl.app, ["link-graph", "src_1", *json_flag])

    assert result.exit_code == 0


def test_epic_update_exits_nonzero_when_no_fields_are_given(resolved_ids: None) -> None:
    """The twin of the task update site, in a file this branch already edits."""
    with patch("sibyl_cli.epic.get_client", return_value=_client()):
        result = CliRunner().invoke(epic.app, ["update", "epic_123"])

    assert result.exit_code == 1
    assert "No fields to update" in result.stdout


def test_explore_traverse_exits_nonzero_on_an_out_of_range_depth() -> None:
    from sibyl_cli import explore

    result = CliRunner().invoke(explore.app, ["traverse", "entity_1", "--depth", "9"])

    assert result.exit_code == 1
    assert "Depth must be between 1 and 3" in result.stdout


def test_explore_dependencies_exits_nonzero_without_a_target() -> None:
    from sibyl_cli import explore

    result = CliRunner().invoke(explore.app, ["dependencies"])

    assert result.exit_code == 1
    assert "Must specify either" in result.stdout


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_synthesis_remember_exits_nonzero_when_nothing_was_remembered(
    json_flag: list[str],
) -> None:
    """The command's whole job is to remember; a run that did not is a failure."""
    drafted = {"artifact": {"title": "x", "remembered_memory_id": None}}

    with patch("sibyl_cli.main.get_client", return_value=_client(synthesis_draft=drafted)):
        result = CliRunner().invoke(main_app, ["synthesis", "remember", "a goal", *json_flag])

    assert result.exit_code == 1


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_synthesis_remember_exits_zero_when_it_lands(json_flag: list[str]) -> None:
    remembered = {
        "artifact": {
            "title": "x",
            "remembered_memory_id": "mem_1",
            "remembered_source_id": "src_1",
            "artifact_id": "art_1",
        }
    }

    with patch("sibyl_cli.main.get_client", return_value=_client(synthesis_draft=remembered)):
        result = CliRunner().invoke(main_app, ["synthesis", "remember", "a goal", *json_flag])

    assert result.exit_code == 0


def test_setup_exits_nonzero_when_integration_files_are_missing() -> None:
    from sibyl_cli import local

    with patch("sibyl_cli.setup.setup_agent_integration", return_value=False):
        result = CliRunner().invoke(local.app, ["setup"])

    assert result.exit_code == 1


def test_update_exits_nonzero_when_a_component_fails() -> None:
    from sibyl_cli import update as update_module

    with (
        patch.object(update_module, "update_cli", return_value=False),
        patch.object(
            update_module, "cli_update_available", return_value=("1.0.0", "2.0.0", True)
        ),
        patch.object(update_module, "get_server_version", return_value=None),
        patch.object(update_module, "is_dev_mode", return_value=False),
    ):
        result = CliRunner().invoke(update_module.app, ["--cli", "--yes"])

    assert result.exit_code == 1


def test_update_exits_nonzero_when_only_the_skills_refresh_fails() -> None:
    """A failure reported through warn() and a discarded bool is still a failure."""
    from sibyl_cli import update as update_module

    with (
        patch.object(update_module, "is_dev_mode", return_value=False),
        patch.object(update_module, "update_skills", return_value=False),
    ):
        result = CliRunner().invoke(update_module.app, ["--skills", "--yes"])

    assert result.exit_code == 1


def test_update_cli_reports_a_failed_skill_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """update_cli returned True even when the skills it installs never landed."""
    from sibyl_cli import update as update_module

    completed = MagicMock()
    completed.returncode = 0

    with (
        patch.object(update_module.subprocess, "run", return_value=completed),
        patch.object(update_module, "get_current_cli_version", return_value="2.0.0"),
        patch.object(update_module, "sync_skills_after_cli_update", return_value=False),
    ):
        assert update_module.update_cli() is False


def test_project_relink_exits_nonzero_when_nothing_matches() -> None:
    """relink exists to repair a link; repairing nothing is a refusal."""
    from sibyl_cli import project as project_module

    client = _client(list_projects={"projects": []})

    with (
        patch("sibyl_cli.project.get_client", return_value=client),
        patch("sibyl_cli.project.set_path_mapping"),
    ):
        result = CliRunner().invoke(project_module.app, ["relink"])

    assert result.exit_code == 1


def _contexts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sibyl_cli import config_store, state

    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(state, "_context_override", None)
    monkeypatch.setattr(state, "_ignore_selection", False)
    config_store.create_context("production", "https://prod.example.com", set_active=True)
    config_store.create_context("staging", "https://staging.example.com")


def test_a_misspelled_context_flag_never_falls_back_to_the_active_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`-C stagingg` with production active must not mutate production."""
    from sibyl_cli import task as task_module

    _contexts(tmp_path, monkeypatch)
    client = _client(create_task={"success": True, "task_id": "t1"})

    with patch("sibyl_cli.task.get_client", return_value=client) as get:
        result = CliRunner().invoke(
            main_app,
            ["-C", "stagingg", "task", "create", "--title", "x", "--project", "p"],
        )

    assert result.exit_code == 1
    assert "Unknown context 'stagingg'" in result.stdout
    assert "production" in result.stdout
    get.assert_not_called()
    _ = task_module


def test_a_misspelled_context_env_var_is_also_a_hard_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _contexts(tmp_path, monkeypatch)
    monkeypatch.setenv("SIBYL_CONTEXT", "stagingg")

    result = CliRunner().invoke(main_app, ["task", "list"])

    assert result.exit_code == 1
    assert "SIBYL_CONTEXT" in result.stdout


def test_a_directory_pin_at_a_deleted_context_is_a_hard_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_cli import config_store

    _contexts(tmp_path, monkeypatch)
    monkeypatch.setattr(config_store, "resolve_context_from_cwd", lambda: "deleted-ctx")

    result = CliRunner().invoke(main_app, ["task", "list"])

    assert result.exit_code == 1
    assert "directory pin" in result.stdout


def test_a_broken_selection_still_lets_you_repair_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard-erroring every command would strand the user with no way back."""
    from sibyl_cli import state

    _contexts(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "_ignore_selection", False)

    with patch("sibyl_cli.context.get_client") as get:
        result = CliRunner().invoke(main_app, ["-C", "stagingg", "config", "context", "list"])

    # The repair command runs; the broken selection is dropped, not enforced.
    assert result.exit_code == 0
    assert "does not exist; ignoring it" in result.stdout
    assert "Unknown context" not in result.stdout
    assert state.context_selection_ignored()
    # Recovery reads the config file. It must never reach a server, because the
    # only server left to reach is the one the user did not select.
    get.assert_not_called()


def test_the_recall_command_is_not_a_repair_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sibyl context <goal>` fetches a pack; it shares a word with the config group."""
    from sibyl_cli import state

    _contexts(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "_ignore_selection", False)

    with patch("sibyl_cli.main.get_client") as get:
        result = CliRunner().invoke(main_app, ["-C", "stagingg", "context", "list"])

    assert result.exit_code == 1
    assert "Unknown context 'stagingg'" in result.stdout
    get.assert_not_called()
    assert not state.context_selection_ignored()


def test_pack_is_not_a_repair_command_either(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The context group holds one networked leaf, and it does not inherit recovery."""
    from sibyl_cli import state

    _contexts(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "_ignore_selection", False)

    with patch("sibyl_cli.context.get_client") as get:
        result = CliRunner().invoke(main_app, ["-C", "stagingg", "config", "context", "pack"])

    assert result.exit_code == 1
    assert "Unknown context 'stagingg'" in result.stdout
    get.assert_not_called()


def test_doctor_drops_to_local_checks_when_the_selection_is_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probing the active context would diagnose a server the user never named."""
    from sibyl_cli import state

    _contexts(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "_ignore_selection", False)
    probes: list[str] = []

    class _NoNetwork:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _NoNetwork:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def get(self, url: str, *args: Any, **kwargs: Any) -> None:
            probes.append(url)
            raise AssertionError(f"doctor probed {url} after dropping the selection")

    with (
        patch("sibyl_cli.doctor.httpx.AsyncClient", _NoNetwork),
        patch("sibyl_cli.doctor.get_client") as get,
    ):
        result = CliRunner().invoke(main_app, ["-C", "stagingg", "doctor", "--json"])

    assert result.exit_code == 1
    assert probes == []
    get.assert_not_called()

    payload = json.loads(result.stdout[result.stdout.index("{") :])
    names = {check["name"]: check["status"] for check in payload["checks"]}
    assert names["context-selection"] == "fail"
    # None of the probes that would have been aimed at the active context ran.
    assert not {"health", "port", "write"} & set(names)


def test_every_recovery_leaf_is_a_real_command_that_never_opens_a_connection() -> None:
    """The carve-out is only safe while its members stay on the filesystem."""
    import inspect

    import typer
    from typer.main import get_command

    from sibyl_cli import main as main_module

    root = get_command(main_app)
    for path in sorted(main_module._CONTEXT_REPAIR_COMMANDS):
        command = root
        for name in path:
            lookup = getattr(command, "get_command", None)
            assert lookup is not None, f"{path} walks through a leaf at '{name}'"
            command = lookup(typer.Context(command), name)
            assert command is not None, f"{path} names a command that does not exist"

        if path == ("doctor",):
            # doctor probes servers by trade; it drops to filesystem checks when
            # the selection is broken, which the doctor test above pins.
            continue

        source = inspect.getsource(inspect.unwrap(command.callback))
        assert "get_client(" not in source and "SibylClient(" not in source, (
            f"{path} is on the recovery allowlist but opens a connection, so a "
            "dropped selection would retarget it at the active context."
        )


def test_an_option_before_a_repair_leaf_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable command path gets no recovery privilege, only refusal."""
    from sibyl_cli import state

    _contexts(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "_ignore_selection", False)

    result = CliRunner().invoke(main_app, ["-C", "stagingg", "contexts", "--json", "list"])

    assert result.exit_code == 1
    assert "Unknown context 'stagingg'" in result.stdout


def test_the_resolver_itself_refuses_to_fall_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard lives in the resolver, not only in the callback."""
    from sibyl_cli import config_store
    from sibyl_cli.client import resolve_api_base_url

    _contexts(tmp_path, monkeypatch)

    with pytest.raises(config_store.UnknownContextError):
        resolve_api_base_url("stagingg")


def test_an_unknown_context_never_inherits_the_active_tls_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_cli import config_store
    from sibyl_cli.client import SibylClient

    _contexts(tmp_path, monkeypatch)

    with pytest.raises(config_store.UnknownContextError):
        SibylClient(context_name="stagingg")


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_synthesis_verify_exits_nonzero_when_verification_fails(json_flag: list[str]) -> None:
    """Five sweep rounds passed over this one; it reports a verdict AND a failure."""
    failed = {"verification": {"status": "gaps", "gap_count": 3, "gaps": []}}

    with patch("sibyl_cli.main.get_client", return_value=_client(synthesis_draft=failed)):
        result = CliRunner().invoke(main_app, ["synthesis", "verify", "a goal", *json_flag])

    assert result.exit_code == 1


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_synthesis_verify_exits_zero_when_verification_passes(json_flag: list[str]) -> None:
    passed = {"verification": {"status": "pass", "source_count": 4, "gaps": []}}

    with patch("sibyl_cli.main.get_client", return_value=_client(synthesis_draft=passed)):
        result = CliRunner().invoke(main_app, ["synthesis", "verify", "a goal", *json_flag])

    assert result.exit_code == 0


def test_setup_reports_failure_when_hooks_cannot_be_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Against the real function, not a mock of it: the previous test mocked the
    whole of setup_agent_integration, so its broken branch never ran."""
    from sibyl_cli import setup as setup_module

    monkeypatch.setattr(setup_module, "find_sibyl_repo", lambda: tmp_path)
    monkeypatch.setattr(setup_module, "install_skills_symlink", lambda _d: (1, 0))
    monkeypatch.setattr(setup_module, "install_hooks_symlink", lambda _d: True)
    monkeypatch.setattr(setup_module, "configure_claude_hooks", lambda: False)

    assert setup_module.setup_agent_integration(verbose=False) is False


def test_setup_reports_failure_when_hooks_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_cli import setup as setup_module

    monkeypatch.setattr(setup_module, "find_sibyl_repo", lambda: tmp_path)
    monkeypatch.setattr(setup_module, "install_skills_symlink", lambda _d: (1, 0))
    monkeypatch.setattr(setup_module, "install_hooks_symlink", lambda _d: False)

    assert setup_module.setup_agent_integration(verbose=False) is False


def test_setup_reports_success_when_everything_lands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_cli import setup as setup_module

    monkeypatch.setattr(setup_module, "find_sibyl_repo", lambda: tmp_path)
    monkeypatch.setattr(setup_module, "install_skills_symlink", lambda _d: (1, 0))
    monkeypatch.setattr(setup_module, "install_hooks_symlink", lambda _d: True)
    monkeypatch.setattr(setup_module, "configure_claude_hooks", lambda: True)

    assert setup_module.setup_agent_integration(verbose=False) is True


def test_update_skills_propagates_a_failed_hook_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sibyl update --skills --yes` reported complete success with hooks absent."""
    from sibyl_cli import setup as setup_module
    from sibyl_cli import update as update_module

    monkeypatch.setattr(setup_module, "find_sibyl_repo", lambda: tmp_path)
    monkeypatch.setattr(setup_module, "install_skills_symlink", lambda _d: (1, 0))
    monkeypatch.setattr(setup_module, "install_hooks_symlink", lambda _d: False)
    monkeypatch.setattr(update_module, "is_dev_mode", lambda: False)

    result = CliRunner().invoke(update_module.app, ["--skills", "--yes"])

    assert result.exit_code == 1
