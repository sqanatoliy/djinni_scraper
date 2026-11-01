#!/usr/bin/env python3
"""Run one Djinni search profile and expose a machine-readable crawl outcome.

Scrapy treats ``CloseSpider`` as a normal shutdown.  This wrapper deliberately
turns authentication and UI failures into a non-zero process result so CI
cannot upload a fresh, empty SQLite file after a failed crawl.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict, dataclass
from enum import IntEnum
from pathlib import Path
from typing import Mapping

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from djinni_scraper.search_profiles import get_search_profile
from djinni_scraper.spiders.djinni import DjinniSpider


class ExitCode(IntEnum):
    SUCCESS = 0
    APPLICATION_FAILURE = 2
    INFRASTRUCTURE_FAILURE = 1


@dataclass(frozen=True, slots=True)
class ProfileOutcome:
    profile: str
    exit_code: int
    success: bool
    category: str
    reason: str
    authenticated: bool
    search_requested: bool
    listing_completed: bool
    listing_pages: int
    listing_job_cards: int
    items_persisted: int
    state_mutated: bool

    def github_outputs(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "success": str(self.success).lower(),
            "category": self.category,
            "reason": self.reason,
            "authenticated": str(self.authenticated).lower(),
            "search_requested": str(self.search_requested).lower(),
            "listing_completed": str(self.listing_completed).lower(),
            "listing_pages": str(self.listing_pages),
            "listing_job_cards": str(self.listing_job_cards),
            "items_persisted": str(self.items_persisted),
            "state_mutated": str(self.state_mutated).lower(),
        }


def outcome_from_stats(profile: str, stats: Mapping[str, object]) -> ProfileOutcome:
    """Classify a crawl from explicit spider stats, never from log text."""
    authenticated = stats.get("djinni/authenticated") is True
    search_requested = stats.get("djinni/search_requested") is True
    listing_completed = stats.get("djinni/listing_completed") is True
    listing_pages = int(stats.get("djinni/listing_pages", 0))
    listing_job_cards = int(stats.get("djinni/listing_job_cards", 0))
    items_persisted = int(stats.get("item_scraped_count", 0))
    state_mutated = items_persisted > 0

    infrastructure_failure = stats.get("djinni/infrastructure_failure")
    application_failure = stats.get("djinni/application_failure")
    spider_exceptions = any(str(key).startswith("spider_exceptions/") for key in stats)

    if infrastructure_failure:
        category = "infrastructure_failure"
        reason = str(infrastructure_failure)
        exit_code = ExitCode.INFRASTRUCTURE_FAILURE
    elif application_failure:
        category = "application_failure"
        reason = str(application_failure)
        exit_code = ExitCode.APPLICATION_FAILURE
    elif spider_exceptions:
        category = "infrastructure_failure"
        reason = "spider_exception"
        exit_code = ExitCode.INFRASTRUCTURE_FAILURE
    elif not authenticated:
        category = "infrastructure_failure"
        reason = "authentication_not_confirmed"
        exit_code = ExitCode.INFRASTRUCTURE_FAILURE
    elif not search_requested:
        category = "infrastructure_failure"
        reason = "search_url_not_requested"
        exit_code = ExitCode.INFRASTRUCTURE_FAILURE
    elif not listing_completed:
        category = "infrastructure_failure"
        reason = "listing_not_completed"
        exit_code = ExitCode.INFRASTRUCTURE_FAILURE
    else:
        category = "success"
        reason = "listing_completed"
        exit_code = ExitCode.SUCCESS

    return ProfileOutcome(
        profile=profile,
        exit_code=int(exit_code),
        success=exit_code is ExitCode.SUCCESS,
        category=category,
        reason=reason,
        authenticated=authenticated,
        search_requested=search_requested,
        listing_completed=listing_completed,
        listing_pages=listing_pages,
        listing_job_cards=listing_job_cards,
        items_persisted=items_persisted,
        state_mutated=state_mutated,
    )


def run_profile(profile: str) -> ProfileOutcome:
    """Start one crawler and return its explicit outcome after shutdown."""
    process = CrawlerProcess(get_project_settings())
    crawler = process.create_crawler(DjinniSpider)
    try:
        process.crawl(crawler, profile=profile)
        process.start(stop_after_crawl=True)
    except BaseException:  # pragma: no cover - integration boundary
        logging.getLogger(__name__).exception("Djinni profile runner failed before crawl completion")
        return outcome_from_stats(
            profile,
            {"djinni/infrastructure_failure": "runner_exception"},
        )
    return outcome_from_stats(profile, crawler.stats.get_stats())


def write_github_outputs(path: str | None, outcome: ProfileOutcome) -> None:
    """Write scalar outputs before returning a non-zero exit code, if requested."""
    if not path:
        return
    output_path = Path(path)
    with output_path.open("a", encoding="utf-8") as output_file:
        for key, value in outcome.github_outputs().items():
            output_file.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=tuple(get_search_profile(slug).slug for slug in ("python", "data_engineer", "other", "support")))
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT"),
        help="Path to the GitHub Actions output file (defaults to GITHUB_OUTPUT).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    outcome = run_profile(args.profile)
    write_github_outputs(args.github_output, outcome)
    print(json.dumps(asdict(outcome), sort_keys=True))
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
