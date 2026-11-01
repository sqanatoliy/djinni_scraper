#!/usr/bin/env python3
"""Run one DefTech DOU-surface profile and fail closed on source-contract errors."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from djinni_scraper.deftech_profiles import list_deftech_search_profiles
from djinni_scraper.spiders.deftech_dou import DefTechDouSpider
from scripts.run_dou_profile import DouProfileOutcome, ExitCode, outcome_from_stats, write_github_outputs


def run_profile(profile: str) -> DouProfileOutcome:
    """Runs one DefTech crawler and returns its machine-readable terminal outcome."""
    process = CrawlerProcess(get_project_settings())
    crawler = process.create_crawler(DefTechDouSpider)
    try:
        process.crawl(crawler, profile=profile)
        process.start(stop_after_crawl=True)
    except BaseException:  # pragma: no cover - integration boundary
        logging.getLogger(__name__).exception("DefTech profile runner failed before crawl completion")
        return outcome_from_stats(
            profile,
            {"deftech/infrastructure_failure": "runner_exception"},
            stats_prefix="deftech",
            source="deftech",
        )
    return outcome_from_stats(
        profile,
        crawler.stats.get_stats(),
        stats_prefix="deftech",
        source="deftech",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        required=True,
        choices=tuple(profile.slug for profile in list_deftech_search_profiles()),
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
