from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.perf.multi_user import (  # noqa: E402
    OPERATION_NAMES,
    check_thresholds,
    config_from_args,
    default_output_path,
    format_summary,
    run_multi_user_performance,
    write_report,
)

pytestmark = [
    pytest.mark.perf,
    pytest.mark.slow,
    pytest.mark.stress,
    pytest.mark.skipif(
        os.getenv("SIBYL_E2E_PERF") != "1",
        reason="set SIBYL_E2E_PERF=1 or run moon run e2e:test-perf",
    ),
]


@pytest.mark.asyncio
async def test_multi_user_api_performance(wait_for_services: None) -> None:
    config = config_from_args([])
    if os.getenv("SIBYL_PERF_OUTPUT_PATH") is None:
        config = replace(config, output_path=REPO_ROOT / default_output_path(config.run_id))

    report = await run_multi_user_performance(config)
    write_report(report, config.output_path)
    sys.stdout.write(format_summary(report, config.output_path))

    failures = check_thresholds(report, config)
    assert not failures, "\n".join(failures)
    assert report["summary"]["total_requests"] == config.users * config.iterations * len(
        OPERATION_NAMES
    )
