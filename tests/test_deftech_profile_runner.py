from pathlib import Path
import subprocess
import sys

import pytest

from scripts.run_deftech_profile import ExitCode, outcome_from_stats, write_github_outputs


ROOT = Path(__file__).parents[1]


def _complete_stats(**overrides):
    values = {
        "deftech/search_requested": True,
        "deftech/listing_completed": True,
        "deftech/xhr_completed": True,
        "deftech/listing_pages": 1,
        "deftech/listing_job_cards": 7,
        "deftech/xhr_batches": 1,
        "deftech/detail_requests": 7,
        "deftech/detail_completed": 7,
        "item_scraped_count": 7,
    }
    values.update(overrides)
    return values


def _outcome(stats):
    return outcome_from_stats(
        "deftech-data-engineer",
        stats,
        stats_prefix="deftech",
        source="deftech",
    )


def test_deftech_runner_accepts_a_valid_zero_result_after_completed_xhr_contract():
    outcome = _outcome(
        _complete_stats(
            **{
                "deftech/listing_job_cards": 0,
                "deftech/detail_requests": 0,
                "deftech/detail_completed": 0,
                "item_scraped_count": 0,
            }
        )
    )

    assert outcome.exit_code == ExitCode.SUCCESS
    assert outcome.success is True
    assert outcome.source == "deftech"
    assert outcome.state_mutated is False


def test_deftech_runner_uses_shared_persistence_mutation_stat_not_item_count():
    no_durable_change = _outcome(_complete_stats())
    outcome = _outcome(
        _complete_stats(
            **{
                "deftech/listing_job_cards": 0,
                "deftech/detail_requests": 0,
                "deftech/detail_completed": 0,
                "item_scraped_count": 0,
                "persistence/state_mutated": True,
            }
        )
    )

    assert no_durable_change.items_persisted == 7
    assert no_durable_change.state_mutated is False
    assert outcome.success is True
    assert outcome.items_persisted == 0
    assert outcome.state_mutated is True


@pytest.mark.parametrize(
    ("stats", "reason"),
    (
        ({"deftech/infrastructure_failure": "detail_canonical_invalid"}, "detail_canonical_invalid"),
        (_complete_stats(**{"deftech/xhr_completed": False}), "listing_or_xhr_not_completed"),
        (_complete_stats(**{"deftech/detail_completed": 6}), "detail_processing_incomplete"),
        ({"deftech/search_requested": True, "deftech/listing_completed": True}, "listing_or_xhr_not_completed"),
    ),
)
def test_deftech_runner_fails_closed_for_contract_and_detail_errors(stats, reason):
    outcome = _outcome(stats)

    assert outcome.exit_code == ExitCode.INFRASTRUCTURE_FAILURE
    assert outcome.success is False
    assert outcome.reason == reason


def test_deftech_runner_outputs_include_the_surface_identifier(tmp_path):
    outcome = _outcome(_complete_stats())
    path = tmp_path / "github-output"

    write_github_outputs(str(path), outcome)

    values = dict(line.split("=", 1) for line in path.read_text().splitlines())
    assert values["profile"] == "deftech-data-engineer"
    assert values["success"] == "true"
    assert outcome.source == "deftech"


def test_deftech_runner_is_directly_executable_from_the_repository_root():
    result = subprocess.run(
        [sys.executable, "scripts/run_deftech_profile.py", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "deftech-data-engineer" in result.stdout
