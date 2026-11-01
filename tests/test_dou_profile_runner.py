from pathlib import Path
import subprocess
import sys

import pytest

from scripts.run_dou_profile import ExitCode, outcome_from_stats, write_github_outputs


ROOT = Path(__file__).parents[1]


def _complete_stats(**overrides):
    values = {
        "dou/search_requested": True,
        "dou/listing_completed": True,
        "dou/xhr_completed": True,
        "dou/listing_pages": 1,
        "dou/listing_job_cards": 2,
        "dou/xhr_batches": 1,
        "dou/detail_requests": 2,
        "dou/detail_completed": 2,
        "item_scraped_count": 2,
    }
    values.update(overrides)
    return values


def test_completed_dou_zero_result_profile_is_successful_after_final_xhr_contract():
    outcome = outcome_from_stats(
        "dou-data-engineer-0-1",
        _complete_stats(
            **{
                "dou/listing_job_cards": 0,
                "dou/detail_requests": 0,
                "dou/detail_completed": 0,
                "item_scraped_count": 0,
            }
        ),
    )

    assert outcome.exit_code == ExitCode.SUCCESS
    assert outcome.success is True
    assert outcome.source == "dou"
    assert outcome.items_persisted == 0
    assert outcome.state_mutated is False


@pytest.mark.parametrize(
    ("stats", "reason"),
    (
        ({"dou/infrastructure_failure": "xhr_non_progress"}, "xhr_non_progress"),
        (_complete_stats(**{"dou/xhr_completed": False}), "listing_or_xhr_not_completed"),
        (_complete_stats(**{"dou/detail_completed": 1}), "detail_processing_incomplete"),
        ({"dou/search_requested": True, "dou/listing_completed": True}, "listing_or_xhr_not_completed"),
    ),
)
def test_runner_fails_closed_for_source_contract_or_detail_failures(stats, reason):
    outcome = outcome_from_stats("dou-python-0-1", stats)

    assert outcome.exit_code == ExitCode.INFRASTRUCTURE_FAILURE
    assert outcome.success is False
    assert outcome.reason == reason


def test_runner_outputs_are_machine_readable_and_include_dou_xhr_terminal_state(tmp_path):
    outcome = outcome_from_stats("dou-support-remote-0-1", _complete_stats())
    path = tmp_path / "github-output"

    write_github_outputs(str(path), outcome)

    values = dict(line.split("=", 1) for line in path.read_text().splitlines())
    assert values == {
        "profile": "dou-support-remote-0-1",
        "success": "true",
        "category": "success",
        "reason": "listing_xhr_and_details_completed",
        "search_requested": "true",
        "listing_completed": "true",
        "xhr_completed": "true",
        "listing_pages": "1",
        "listing_job_cards": "2",
        "xhr_batches": "1",
        "items_persisted": "2",
        "detail_requests": "2",
        "detail_completed": "2",
        "state_mutated": "true",
    }


def test_dou_runner_is_directly_executable_from_the_repository_root():
    result = subprocess.run(
        [sys.executable, "scripts/run_dou_profile.py", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "dou-python-0-1" in result.stdout
