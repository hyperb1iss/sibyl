from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from shutil import which
from typing import Any, NotRequired, TypedDict, cast

import pytest
from tools.trust import team_scope_gate

MISSING_SURFACE_EXIT_CODE = 2
GRAPH_TEAM_DENIAL_SURFACE_COUNT = 3
REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_RECEIPT = team_scope_gate.DEFAULT_RECEIPT_PATH
MANIFEST_PATH = REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "manifest.json"


class MoonTask(TypedDict):
    command: str
    args: NotRequired[list[str]]
    target: str


class MoonTaskQuery(TypedDict):
    tasks: dict[str, dict[str, MoonTask]]


def _root_moon_tasks() -> dict[str, MoonTask]:
    moon = which("moon")
    assert moon is not None

    result = subprocess.run(  # noqa: S603
        [moon, "query", "tasks", "--project", "root"],
        cwd=REPO_ROOT,
        env={**os.environ, "MOON_COLOR": "false"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = cast(MoonTaskQuery, json.loads(result.stdout))
    return payload["tasks"]["root"]


@pytest.fixture(scope="module")
def observed_receipt() -> dict[str, Any]:
    """One real fixture-store run, shared by every test that reads observations."""
    return team_scope_gate.build_observed_team_scope_receipt()


@pytest.fixture(scope="module")
def committed_receipt() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(COMMITTED_RECEIPT.read_text(encoding="utf-8")))


def _passing_results() -> list[team_scope_gate.GateResult]:
    return [
        team_scope_gate.GateResult(check=check, exit_code=0, elapsed_seconds=0.0)
        for check in team_scope_gate.GATE_CHECKS
    ]


def _full_receipt(observed: dict[str, Any]) -> dict[str, Any]:
    results = _passing_results()
    return team_scope_gate.with_check_results(
        team_scope_gate.with_promotion_coverage(deepcopy(observed), results),
        results,
    )


class TestObservedReceipt:
    def test_reports_no_leak_and_no_allow_failure(self, observed_receipt: dict[str, Any]) -> None:
        metrics = observed_receipt["metrics"]

        assert metrics["leak_count"] == 0
        assert metrics["allow_failure_count"] == 0
        assert observed_receipt["schema_version"] == team_scope_gate.RECEIPT_SCHEMA_VERSION

    def test_probes_both_directions_on_every_surface(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        directions: dict[str, set[str]] = {}
        for probe in observed_receipt["probes"]:
            directions.setdefault(probe["surface"], set()).add(probe["expected"])

        assert directions, "receipt recorded no probes"
        deny_only = sorted(
            surface for surface, seen in directions.items() if seen == {team_scope_gate.DENY}
        )
        # Only the surfaces that cannot serve a team row at all may be deny-only,
        # and they have to say so through a declared boundary.
        assert deny_only == [], f"surfaces probed in one direction only: {deny_only}"

    def test_resolves_membership_from_the_auth_store(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        by_label = {entry["label"]: entry for entry in observed_receipt["principals"]}

        assert by_label["member"]["resolved_teams"] == [team_scope_gate.TEAM_ID]
        assert by_label["member"]["resolved_projects"] == [team_scope_gate.PROJECT_ID]
        assert by_label["outsider"]["resolved_teams"] == [team_scope_gate.OTHER_TEAM_ID]
        # The outsider holds a different project so the row-selection clause gets
        # probed on the project surface too, not just the membership check.
        assert by_label["outsider"]["resolved_projects"] == [team_scope_gate.OTHER_PROJECT_ID]
        # The delegation resolver reads memory_spaces and memory_space_members, so
        # the delegated scope has a real serving direction rather than a boundary.
        assert by_label["member"]["resolved_delegations"] == [team_scope_gate.DELEGATION_ID]
        assert by_label["outsider"]["resolved_delegations"] == []

    def test_write_path_drops_every_forged_owner_field(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        offered = {
            entry["label"]: entry["offered_owner_forgeries"]
            for entry in observed_receipt["memories"]
        }
        surviving = {
            entry["label"]: entry["surviving_owner_forgeries"]
            for entry in observed_receipt["memories"]
        }

        # The two backfill-provenance keys are the pair that matters: the
        # authorized path never rewrites them, so they survive if the write
        # path's drop filter is removed, while the scope and owner keys would be
        # overwritten anyway and cannot prove the filter runs.
        assert offered["private-member"] == [
            "memory_scope",
            "principal_id",
            "scope_backfill_prior",
            "scope_backfill_source",
        ]
        assert offered["team-alpha"] == ["scope_key"]
        assert all(not fields for fields in surviving.values())

    def test_team_row_is_stamped_with_the_authorized_key(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        team_row = next(
            entry for entry in observed_receipt["memories"] if entry["label"] == "team-alpha"
        )

        assert team_row["stamped_memory_scope"] == "team"
        assert team_row["stamped_scope_key"] == team_scope_gate.TEAM_ID
        assert team_row["stamped_principal_id"] == team_scope_gate.PRINCIPAL_IDS["member"]

    def test_is_byte_deterministic(self, observed_receipt: dict[str, Any]) -> None:
        rebuilt = team_scope_gate.build_observed_team_scope_receipt()

        assert json.dumps(rebuilt, sort_keys=True) == json.dumps(
            observed_receipt,
            sort_keys=True,
        )

    def test_validates_once_evidence_checks_pass(self, observed_receipt: dict[str, Any]) -> None:
        assert team_scope_gate.validate_team_scope_receipt(_full_receipt(observed_receipt)) == []


class TestCommittedReceipt:
    def test_matches_a_fresh_observation(
        self,
        committed_receipt: dict[str, Any],
        observed_receipt: dict[str, Any],
    ) -> None:
        """The committed receipt has to be regenerable, or it is hand-typed again."""
        assert team_scope_gate.observed_receipt_fields(
            committed_receipt
        ) == team_scope_gate.observed_receipt_fields(observed_receipt)

    def test_carries_no_wall_clock_field(self, committed_receipt: dict[str, Any]) -> None:
        assert "generated_at" not in committed_receipt

    def test_satisfies_its_own_validator(self, committed_receipt: dict[str, Any]) -> None:
        assert team_scope_gate.validate_team_scope_receipt(committed_receipt) == []

    def test_covers_every_manifest_required_surface(
        self,
        committed_receipt: dict[str, Any],
    ) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        contract = next(
            entry
            for entry in manifest["gate_contracts"]
            if entry["name"] == "team-scope-trust-gate"
        )
        covered = {
            surface for check in committed_receipt["checks"] for surface in check["surfaces"]
        }

        for metric_contract in contract["metric_contracts"]:
            assert metric_contract["receipt_schema"] == team_scope_gate.RECEIPT_SCHEMA_VERSION
            assert metric_contract["metric"] in committed_receipt["metrics"]
            for surface in metric_contract.get("required_surfaces", []):
                assert surface in covered, f"{surface} is required but uncovered"

    def test_manifest_gates_the_allow_direction(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        contract = next(
            entry
            for entry in manifest["gate_contracts"]
            if entry["name"] == "team-scope-trust-gate"
        )
        allow_contract = next(
            entry
            for entry in contract["metric_contracts"]
            if entry["metric"] == "allow_failure_count"
        )

        assert allow_contract["direction"] == "lower"
        assert allow_contract["threshold"] == 0
        assert allow_contract["require_receipt_checks"] is True


class TestValidationCatchesRegressions:
    def test_rejects_a_leak(self, observed_receipt: dict[str, Any]) -> None:
        receipt = _full_receipt(observed_receipt)
        receipt["metrics"]["leak_count"] = 1

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("leak_count" in failure for failure in failures)

    def test_rejects_an_allow_failure(self, observed_receipt: dict[str, Any]) -> None:
        receipt = _full_receipt(observed_receipt)
        receipt["metrics"]["allow_failure_count"] = 2

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("allow_failure_count" in failure for failure in failures)

    def test_rejects_a_failing_probe_even_when_metrics_read_clean(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        receipt = _full_receipt(observed_receipt)
        receipt["probes"][0] = {**receipt["probes"][0], "status": "FAIL", "observed": "allow"}

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("expected" in failure for failure in failures)

    def test_rejects_a_dropped_probe_family(self, observed_receipt: dict[str, Any]) -> None:
        """A gate that stops probing must not read as a gate that found nothing."""
        receipt = _full_receipt(observed_receipt)
        receipt["probes"] = [
            probe for probe in receipt["probes"] if probe["surface"] != "graph_metadata_read"
        ]
        receipt["surfaces"] = [
            surface for surface in receipt["surfaces"] if surface != "graph_metadata_read"
        ]
        receipt["metrics"]["deny_probe_count"] = 0
        receipt["metrics"]["allow_probe_count"] = 0
        receipt["metrics"]["surface_count"] = 5

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("deny_probe_count" in failure for failure in failures)
        assert any("allow_probe_count" in failure for failure in failures)
        assert any("surface_count" in failure for failure in failures)
        assert any("'graph_metadata_read' never ran" in failure for failure in failures)

    def test_rejects_a_silent_read_surface(self, observed_receipt: dict[str, Any]) -> None:
        """A recall surface that stops answering must not pass on the listing's word."""
        receipt = _full_receipt(observed_receipt)
        allowed = next(
            index
            for index, probe in enumerate(receipt["probes"])
            if probe["expected"] == team_scope_gate.ALLOW
        )
        receipt["probes"][allowed] = {
            **receipt["probes"][allowed],
            "surface_disagreement": True,
        }
        receipt["metrics"]["surface_disagreement_count"] = 1

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("surface_disagreement_count" in failure for failure in failures)
        assert any("stopped filtering" in failure for failure in failures)

    def test_counts_a_disagreement_as_a_probe_failure(self) -> None:
        probe = team_scope_gate.ScopeProbe(
            surface="raw_targeted_read",
            reader_label="member",
            memory_label="team-alpha",
            expectation=team_scope_gate.ALLOW,
        )
        observation = team_scope_gate.ProbeObservation(
            probe=probe,
            observed=team_scope_gate.ALLOW,
            detail="listed=True recalled=False",
            disagreement=True,
        )

        # The expectation matches, so only the disagreement can fail it.
        assert observation.leaked is False
        assert observation.allow_failed is False
        assert observation.passed is False
        assert observation.as_receipt_entry()["status"] == "FAIL"

    def test_rejects_an_unknown_probe_surface(self, observed_receipt: dict[str, Any]) -> None:
        receipt = _full_receipt(observed_receipt)
        receipt["surfaces"] = [*receipt["surfaces"], "invented_surface"]

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("invented_surface" in failure for failure in failures)

    def test_rejects_an_empty_probe_list(self, observed_receipt: dict[str, Any]) -> None:
        receipt = _full_receipt(observed_receipt)
        receipt["probes"] = []

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert "receipt probes must be a non-empty list" in failures

    def test_rejects_a_read_neutral_mis_stamp(self, observed_receipt: dict[str, Any]) -> None:
        """A row stamped for the wrong audience moves no read outcome at all."""
        receipt = _full_receipt(observed_receipt)
        receipt["metrics"]["stamped_scope_mismatch_count"] = 1

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("stamped_scope_mismatch_count" in failure for failure in failures)
        assert any("not captured for" in failure for failure in failures)

    def test_stamp_mismatch_is_detected_from_the_stamped_metadata(self) -> None:
        memory = team_scope_gate.SeededMemory(
            label="team-alpha",
            memory_scope="team",
            scope_key="team-1",
            owner_label="member",
            title="t",
            content="c",
            raw_memory_id="r",
            graph_metadata={"memory_scope": "project", "scope_key": "team-1"},
            requested_metadata={},
        )

        assert memory.stamp_mismatches == ("team-alpha captured as team but stamped project",)

    def test_rejects_a_surviving_owner_forgery(self, observed_receipt: dict[str, Any]) -> None:
        receipt = _full_receipt(observed_receipt)
        receipt["metrics"]["owner_forgery_surviving_count"] = 1

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("owner_forgery_surviving_count" in failure for failure in failures)

    def test_rejects_a_receipt_that_offered_no_forgery(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        receipt = _full_receipt(observed_receipt)
        receipt["metrics"]["owner_forgery_offered_count"] = 0

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("owner_forgery_offered_count" in failure for failure in failures)

    def test_rejects_a_stale_graph_membership_boundary(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        """Fixing the graph read helper must force the boundary to be retired."""
        receipt = _full_receipt(observed_receipt)
        receipt["metrics"]["graph_team_membership_forwarded"] = 1

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("retire the" in failure for failure in failures)

    def test_rejects_a_missing_promotion_coverage(self, observed_receipt: dict[str, Any]) -> None:
        receipt = team_scope_gate.with_check_results(deepcopy(observed_receipt), [])

        failures = team_scope_gate.validate_team_scope_receipt(receipt)

        assert any("promotion_attribution_coverage" in failure for failure in failures)


class TestBoundaryProbe:
    def test_reports_the_current_read_helper_signature(self) -> None:
        assert team_scope_gate.graph_team_membership_forwarded() is False

    def test_declares_the_boundary_on_entitled_team_denials(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        entitled_team_denials = [
            probe
            for probe in observed_receipt["probes"]
            if probe["memory"] == "team-alpha"
            and probe["reader"] == "member"
            and probe["surface"]
            in {
                "graph_metadata_read",
                "graph_metadata_read_narrowed",
                "retrieval_candidate_filter",
            }
        ]

        assert len(entitled_team_denials) == GRAPH_TEAM_DENIAL_SURFACE_COUNT
        for probe in entitled_team_denials:
            assert probe["expected"] == team_scope_gate.DENY
            assert probe["boundary"] == team_scope_gate.GRAPH_MEMBERSHIP_BOUNDARY

    def test_records_only_the_graph_membership_boundary(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        assert observed_receipt["boundaries"] == [team_scope_gate.GRAPH_MEMBERSHIP_BOUNDARY]

    def test_delegated_scope_is_probed_in_both_directions(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        """A boundary asserting nobody can hold a delegation would be unfalsifiable."""
        directions = {
            probe["expected"]
            for probe in observed_receipt["probes"]
            if probe["memory"] == "delegated-oncall"
        }

        assert directions == {team_scope_gate.ALLOW, team_scope_gate.DENY}


class TestPromotionCoverage:
    def test_is_one_when_every_promotion_check_passes(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        receipt = team_scope_gate.with_promotion_coverage(
            deepcopy(observed_receipt),
            _passing_results(),
        )

        assert receipt["metrics"]["promotion_attribution_coverage"] == 1
        assert receipt["metrics"]["promotion_preview_coverage"] == 1

    def test_drops_when_a_promotion_check_fails(self, observed_receipt: dict[str, Any]) -> None:
        results = [
            team_scope_gate.GateResult(
                check=check,
                exit_code=1 if check.name == "team-scope-rest-policy" else 0,
                elapsed_seconds=0.0,
            )
            for check in team_scope_gate.GATE_CHECKS
        ]

        receipt = team_scope_gate.with_promotion_coverage(deepcopy(observed_receipt), results)

        assert receipt["metrics"]["promotion_attribution_coverage"] < 1
        assert receipt["metrics"]["promotion_preview_coverage"] < 1

    def test_is_zero_when_the_checks_never_ran(self, observed_receipt: dict[str, Any]) -> None:
        receipt = team_scope_gate.with_promotion_coverage(deepcopy(observed_receipt), [])

        assert receipt["metrics"]["promotion_attribution_coverage"] == 0
        assert receipt["metrics"]["promotion_preview_coverage"] == 0


class TestRunGate:
    def test_executes_every_check_and_writes_a_receipt(
        self,
        observed_receipt: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        commands: list[tuple[str, ...]] = []
        receipt_path = tmp_path / "receipt.json"

        exit_code = team_scope_gate.run_gate(
            runner=lambda command: commands.append(command) or 0,
            echo=lambda _: None,
            receipt_path=receipt_path,
            receipt_builder=lambda: deepcopy(observed_receipt),
        )

        assert exit_code == 0
        assert commands == [check.command for check in team_scope_gate.GATE_CHECKS]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["metrics"]["leak_count"] == 0
        assert receipt["metrics"]["promotion_attribution_coverage"] == 1

    def test_fails_and_still_writes_a_receipt_when_a_check_fails(
        self,
        observed_receipt: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        failing = team_scope_gate.GATE_CHECKS[1]
        receipt_path = tmp_path / "receipt.json"
        messages: list[str] = []

        exit_code = team_scope_gate.run_gate(
            runner=lambda command: 1 if command == failing.command else 0,
            echo=messages.append,
            receipt_path=receipt_path,
            receipt_builder=lambda: deepcopy(observed_receipt),
        )

        assert exit_code == 1
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        statuses = {check["name"]: check["status"] for check in receipt["checks"]}
        assert statuses[failing.name] == "FAIL"

    def test_reports_a_leak_without_running_evidence_checks(
        self,
        observed_receipt: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        leaked = deepcopy(observed_receipt)
        leaked["metrics"]["leak_count"] = 1
        leaked["probes"][0] = {
            **leaked["probes"][0],
            "status": "FAIL",
            "expected": team_scope_gate.DENY,
            "observed": team_scope_gate.ALLOW,
        }
        messages: list[str] = []

        exit_code = team_scope_gate.run_gate(
            runner=lambda _: 0,
            echo=messages.append,
            receipt_path=tmp_path / "receipt.json",
            receipt_builder=lambda: leaked,
        )

        assert exit_code == 1
        assert any("leak_count: 1" in message for message in messages)

    def test_rejects_missing_required_surface(self) -> None:
        check = team_scope_gate.GateCheck(
            name="partial",
            description="partial coverage",
            surfaces=("manifest",),
            command=("moon", "run", "bench-gate"),
        )
        messages: list[str] = []

        exit_code = team_scope_gate.run_gate(
            [check],
            runner=lambda _: 0,
            echo=messages.append,
            receipt_path=None,
        )

        assert exit_code == MISSING_SURFACE_EXIT_CODE
        assert "Team scope gate is missing required surfaces:" in messages
        assert "- team target redaction" in messages

    def test_observe_only_enforces_the_anti_vacuity_floors(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        """`--observe-only` is a gate, so a shrunken probe set has to fail it there too."""
        starved = deepcopy(observed_receipt)
        starved["probes"] = starved["probes"][:1]
        starved["surfaces"] = [starved["probes"][0]["surface"]]
        starved["metrics"]["deny_probe_count"] = 1
        starved["metrics"]["allow_probe_count"] = 0
        starved["metrics"]["surface_count"] = 1
        messages: list[str] = []

        exit_code = team_scope_gate.run_observations(
            echo=messages.append,
            receipt_path=None,
            receipt_builder=lambda: starved,
        )

        assert exit_code == 1
        assert any("allow_probe_count" in message for message in messages)

    def test_observe_only_never_writes_without_a_path(
        self,
        observed_receipt: dict[str, Any],
    ) -> None:
        messages: list[str] = []

        exit_code = team_scope_gate.run_observations(
            echo=messages.append,
            receipt_path=None,
            receipt_builder=lambda: deepcopy(observed_receipt),
        )

        assert exit_code == 0
        assert not any("receipt:" in message for message in messages)


class TestCommandLine:
    def test_lists_gate_checks(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = team_scope_gate.main(["--list"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "team-scope-rest-policy: moon run api:memory-trust-rest-test" in captured.out
        assert "ai-memory-contracts: moon run bench-gate" in captured.out

    def test_root_moon_tasks_expose_the_gate(self) -> None:
        tasks = _root_moon_tasks()

        gate = tasks["team-scope-gate"]
        assert gate["target"] == "root:team-scope-gate"
        assert gate["command"] == "uv"
        assert gate["args"] == ["run", "python", "-m", "tools.trust.team_scope_gate"]

        observe = tasks["team-scope-gate-observe"]
        assert observe["args"] == [
            "run",
            "python",
            "-m",
            "tools.trust.team_scope_gate",
            "--observe-only",
        ]

        test_task = tasks["team-scope-gate-test"]
        assert test_task["target"] == "root:team-scope-gate-test"
        assert test_task["args"] == [
            "run",
            "pytest",
            "tools/tests/test_team_scope_gate.py",
            "-v",
        ]
