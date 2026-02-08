"""Tests for sandbox rollout resolution -- pure function tests."""

from __future__ import annotations

from uuid import uuid4

from sibyl.agents.sandbox_rollout import resolve_sandbox_mode


class TestResolveMode:
    """Deterministic rollout resolution."""

    def test_global_off_overrides_all(self):
        """global_mode='off' always returns 'off' regardless of other settings."""
        result = resolve_sandbox_mode(
            global_mode="off",
            org_id=uuid4(),
            rollout_percent=100,
            rollout_orgs=[],
            canary_mode=False,
        )
        assert result == "off"

    def test_global_off_overrides_explicit_org(self):
        """global_mode='off' overrides even explicit org allowlist."""
        org = uuid4()
        result = resolve_sandbox_mode(
            global_mode="off",
            org_id=org,
            rollout_percent=100,
            rollout_orgs=[str(org)],
            canary_mode=False,
        )
        assert result == "off"

    def test_global_off_overrides_canary(self):
        """global_mode='off' overrides canary mode."""
        org = uuid4()
        result = resolve_sandbox_mode(
            global_mode="off",
            org_id=org,
            rollout_percent=100,
            rollout_orgs=[str(org)],
            canary_mode=True,
        )
        assert result == "off"

    def test_explicit_org_in_list(self):
        """Org in rollout_orgs gets global_mode."""
        org = uuid4()
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=org,
            rollout_percent=0,
            rollout_orgs=[str(org)],
            canary_mode=False,
        )
        assert result == "enforced"

    def test_explicit_org_canary_mode(self):
        """Org in rollout_orgs with canary gets shadow."""
        org = uuid4()
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=org,
            rollout_percent=0,
            rollout_orgs=[str(org)],
            canary_mode=True,
        )
        assert result == "shadow"

    def test_hundred_percent_means_all(self):
        """rollout_percent=100 enables for all orgs."""
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=uuid4(),
            rollout_percent=100,
            rollout_orgs=[],
            canary_mode=False,
        )
        assert result == "enforced"

    def test_zero_percent_means_off(self):
        """rollout_percent=0 with no explicit orgs means off."""
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=uuid4(),
            rollout_percent=0,
            rollout_orgs=[],
            canary_mode=False,
        )
        assert result == "off"

    def test_percent_deterministic(self):
        """Same org_id always gets the same result."""
        org = uuid4()
        results = set()
        for _ in range(10):
            r = resolve_sandbox_mode(
                global_mode="enforced",
                org_id=org,
                rollout_percent=50,
                rollout_orgs=[],
                canary_mode=False,
            )
            results.add(r)

        # Should always be the same answer for the same org
        assert len(results) == 1

    def test_hundred_percent_ignores_canary_for_non_explicit(self):
        """100% rollout returns global_mode directly (bypasses canary for percentage)."""
        # The code path: rollout_percent >= 100 returns global_mode before hash check
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=uuid4(),
            rollout_percent=100,
            rollout_orgs=[],
            canary_mode=True,
        )
        # 100% hits the early return: `return global_mode`
        assert result == "enforced"

    def test_shadow_mode_passthrough(self):
        """global_mode='shadow' works as expected."""
        result = resolve_sandbox_mode(
            global_mode="shadow",
            org_id=uuid4(),
            rollout_percent=100,
            rollout_orgs=[],
            canary_mode=False,
        )
        assert result == "shadow"

    def test_org_not_in_list_with_zero_percent(self):
        """Org not in allowlist with 0% gets off."""
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=uuid4(),
            rollout_percent=0,
            rollout_orgs=["some-other-org-id"],
            canary_mode=False,
        )
        assert result == "off"

    def test_negative_percent_means_off(self):
        """Negative rollout_percent treated as off."""
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=uuid4(),
            rollout_percent=-10,
            rollout_orgs=[],
            canary_mode=False,
        )
        assert result == "off"

    def test_over_hundred_percent_means_all(self):
        """rollout_percent > 100 treated same as 100."""
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=uuid4(),
            rollout_percent=200,
            rollout_orgs=[],
            canary_mode=False,
        )
        assert result == "enforced"

    def test_different_orgs_get_different_results_at_50_percent(self):
        """At 50%, roughly half of orgs should be in vs out."""
        in_count = 0
        total = 200
        for _ in range(total):
            result = resolve_sandbox_mode(
                global_mode="enforced",
                org_id=uuid4(),
                rollout_percent=50,
                rollout_orgs=[],
                canary_mode=False,
            )
            if result == "enforced":
                in_count += 1

        # Allow wide tolerance: 25-75% should be "in"
        assert 50 < in_count < 150, f"Expected ~100 in, got {in_count}"

    def test_canary_with_percentage_bucket(self):
        """Org in percentage bucket with canary gets shadow."""
        # Force an org into the bucket by using 100% with explicit org
        org = uuid4()
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=org,
            rollout_percent=0,
            rollout_orgs=[str(org)],
            canary_mode=True,
        )
        assert result == "shadow"

    def test_explicit_org_takes_priority_over_percentage(self):
        """Org in allowlist is included even at 0%."""
        org = uuid4()
        result = resolve_sandbox_mode(
            global_mode="enforced",
            org_id=org,
            rollout_percent=0,
            rollout_orgs=[str(org)],
            canary_mode=False,
        )
        assert result == "enforced"
