#!/usr/bin/env python3
"""Run one DOU profile and fail closed on listing, XHR, or detail-contract errors."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from enum import IntEnum
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from djinni_scraper.dou_profiles import list_dou_search_profiles
from djinni_scraper.spiders.dou import DouSpider


class ExitCode(IntEnum):
    SUCCESS = 0
    INFRASTRUCTURE_FAILURE = 1


@dataclass(frozen=True, slots=True)
class DouProfileOutcome:
    source: str
    profile: str
    exit_code: int
    success: bool
    category: str
    reason: str
    search_requested: bool
    listing_completed: bool
    xhr_completed: bool
    listing_pages: int
    listing_job_cards: int
    xhr_batches: int
    items_persisted: int
    detail_requests: int
    detail_completed: int
    state_mutated: bool

    def github_outputs(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "success": str(self.success).lower(),
            "category": self.category,
            "reason": self.reason,
            "search_requested": str(self.search_requested).lower(),
            "listing_completed": str(self.listing_completed).lower(),
            "xhr_completed": str(self.xhr_completed).lower(),
            "listing_pages": str(self.listing_pages),
            "listing_job_cards": str(self.listing_job_cards),
            "xhr_batches": str(self.xhr_batches),
            "items_persisted": str(self.items_persisted),
            "detail_requests": str(self.detail_requests),
            "detail_completed": str(self.detail_completed),
            "state_mutated": str(self.state_mutated).lower(),
        }


def outcome_from_stats(
    profile: str,
    stats: Mapping[str, object],
    *,
    stats_prefix: str = "dou",
    source: str = "dou",
) -> DouProfileOutcome:
    """Classifies a public DOU-family surface from explicit source-contract stats."""
    prefix = stats_prefix.strip().rstrip("/")
    stat = lambda name: f"{prefix}/{name}"
    search_requested = stats.get(stat("search_requested")) is True
    listing_completed = stats.get(stat("listing_completed")) is True
    xhr_completed = stats.get(stat("xhr_completed")) is True
    listing_pages = int(stats.get(stat("listing_pages"), 0))
    listing_job_cards = int(stats.get(stat("listing_job_cards"), 0))
    xhr_batches = int(stats.get(stat("xhr_batches"), 0))
    items_persisted = int(stats.get("item_scraped_count", 0))
    detail_requests = int(stats.get(stat("detail_requests"), 0))
    detail_completed = int(stats.get(stat("detail_completed"), 0))
    failure = stats.get(stat("infrastructure_failure"))
    spider_exceptions = any(str(key).startswith("spider_exceptions/") for key in stats)

    if failure:
        category, reason = "infrastructure_failure", str(failure)
    elif spider_exceptions:
        category, reason = "infrastructure_failure", "spider_exception"
    elif not search_requested:
        category, reason = "infrastructure_failure", "search_url_not_requested"
    elif not listing_completed or not xhr_completed:
        category, reason = "infrastructure_failure", "listing_or_xhr_not_completed"
    elif detail_requests != detail_completed:
        category, reason = "infrastructure_failure", "detail_processing_incomplete"
    else:
        category, reason = "success", "listing_xhr_and_details_completed"

    exit_code = ExitCode.SUCCESS if category == "success" else ExitCode.INFRASTRUCTURE_FAILURE
    return DouProfileOutcome(
        source=source,
        profile=profile,
        exit_code=int(exit_code),
        success=exit_code is ExitCode.SUCCESS,
        category=category,
        reason=reason,
        search_requested=search_requested,
        listing_completed=listing_completed,
        xhr_completed=xhr_completed,
        listing_pages=listing_pages,
        listing_job_cards=listing_job_cards,
        xhr_batches=xhr_batches,
        items_persisted=items_persisted,
        detail_requests=detail_requests,
        detail_completed=detail_completed,
        state_mutated=items_persisted > 0,
    )


def run_profile(profile: str) -> DouProfileOutcome:
    """Runs one typed DOU crawler and returns a machine-readable terminal outcome."""
    process = CrawlerProcess(get_project_settings())
    crawler = process.create_crawler(DouSpider)
    try:
        process.crawl(crawler, profile=profile)
        process.start(stop_after_crawl=True)
    except BaseException:  # pragma: no cover - integration boundary
        logging.getLogger(__name__).exception("DOU profile runner failed before crawl completion")
        return outcome_from_stats(profile, {"dou/infrastructure_failure": "runner_exception"})
    return outcome_from_stats(profile, crawler.stats.get_stats())


def write_github_outputs(path: str | None, outcome: DouProfileOutcome) -> None:
    """Writes scalar outputs even when the runner will return a non-zero code."""
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output_file:
        for key, value in outcome.github_outputs().items():
            output_file.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        required=True,
        choices=tuple(profile.slug for profile in list_dou_search_profiles()),
    )
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
