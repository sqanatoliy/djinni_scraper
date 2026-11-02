from pathlib import Path
import subprocess
import sys

import pytest

from scripts.run_djinni_profile import ExitCode, outcome_from_stats, write_github_outputs


ROOT = Path(__file__).parents[1]


def test_confirmed_zero_result_listing_is_a_successful_profile_outcome():
    outcome = outcome_from_stats(
        "python",
        {
            "djinni/authenticated": True,
            "djinni/search_requested": True,
            "djinni/listing_completed": True,
            "djinni/listing_pages": 1,
            "djinni/listing_job_cards": 0,
        },
    )

    assert outcome.exit_code == ExitCode.SUCCESS
    assert outcome.success is True
    assert outcome.category == "success"
    assert outcome.state_mutated is False


def test_djinni_runner_uses_shared_persistence_mutation_stat_not_item_count():
    base_stats = {
        "djinni/authenticated": True,
        "djinni/search_requested": True,
        "djinni/listing_completed": True,
        "djinni/listing_pages": 1,
        "djinni/listing_job_cards": 5,
        "item_scraped_count": 5,
    }

    without_durable_change = outcome_from_stats("python", base_stats)
    notification_only_change = outcome_from_stats(
        "python",
        {**base_stats, "item_scraped_count": 0, "persistence/state_mutated": True},
    )

    assert without_durable_change.state_mutated is False
    assert notification_only_change.items_persisted == 0
    assert notification_only_change.state_mutated is True


@pytest.mark.parametrize(
    "reason",
    ("login_ui_unavailable", "auth_rejected", "auth_challenge"),
)
def test_auth_or_application_failure_returns_exit_code_two(reason):
    outcome = outcome_from_stats(
        "python",
        {"djinni/application_failure": reason},
    )

    assert outcome.exit_code == ExitCode.APPLICATION_FAILURE
    assert outcome.success is False
    assert outcome.reason == reason


def test_infrastructure_failure_and_missing_markers_return_exit_code_one():
    infrastructure = outcome_from_stats(
        "python",
        {"djinni/infrastructure_failure": "playwright_auth_error"},
    )
    incomplete = outcome_from_stats("python", {"djinni/authenticated": True})

    assert infrastructure.exit_code == ExitCode.INFRASTRUCTURE_FAILURE
    assert infrastructure.reason == "playwright_auth_error"
    assert incomplete.exit_code == ExitCode.INFRASTRUCTURE_FAILURE
    assert incomplete.reason == "search_url_not_requested"


def test_runner_outputs_include_safe_outcome_markers_and_actual_counts(tmp_path):
    outcome = outcome_from_stats(
        "support",
        {
            "djinni/authenticated": True,
            "djinni/search_requested": True,
            "djinni/listing_completed": True,
            "djinni/listing_pages": 2,
            "djinni/listing_job_cards": 5,
            "item_scraped_count": 5,
            "persistence/state_mutated": True,
        },
    )
    output_path = tmp_path / "github-output"

    write_github_outputs(str(output_path), outcome)

    outputs = dict(line.split("=", 1) for line in Path(output_path).read_text().splitlines())
    assert outputs == {
        "profile": "support",
        "success": "true",
        "category": "success",
        "reason": "listing_completed",
        "authenticated": "true",
        "search_requested": "true",
        "listing_completed": "true",
        "listing_pages": "2",
        "listing_job_cards": "5",
        "items_persisted": "5",
        "state_mutated": "true",
    }


def test_runner_is_directly_executable_from_the_repository_root():
    result = subprocess.run(
        [sys.executable, "scripts/run_djinni_profile.py", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--profile" in result.stdout
