"""Public DefTech DOU search surface using the proven DOU HTTP transport."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import scrapy

from djinni_scraper.adapters.dou_vacancy import dou_vacancy_id_from_url, normalize_dou_url
from djinni_scraper.deftech_profiles import DefTechSearchProfile, get_deftech_search_profile
from djinni_scraper.spiders.dou import DouSpider


class DefTechDouSpider(DouSpider):
    """Scrapes one DefTech profile while canonicalizing to the shared DOU vacancy."""

    name = "deftech"
    source_label = "DefTech"
    # ``jobs.dou.ua`` is a narrowly scoped recovery target for a DefTech
    # listing card with a stale company slug. It remains subject to canonical
    # link validation and is never a second discovery surface.
    allowed_domains = ["deftech.dou.ua", "jobs.dou.ua"]

    def __init__(self, profile: str, **kwargs: Any):
        scrapy.Spider.__init__(self, **kwargs)
        if kwargs:
            conflicting_args = ", ".join(sorted(kwargs))
            raise ValueError(f"DefTech profile mode cannot be combined with ad-hoc arguments: {conflicting_args}")
        self.search_profile: DefTechSearchProfile = get_deftech_search_profile(profile)
        self.discovery_profile = self.search_profile.slug
        self.notification_route = self.search_profile.notification_route
        self.start_urls = [self.search_profile.search_url]

    def _set_run_stat(self, key: str, value: object) -> None:
        super()._set_run_stat(self._deftech_stat_key(key), value)

    def _run_stat_value(self, key: str) -> int:
        return super()._run_stat_value(self._deftech_stat_key(key))

    def _canonical_url_for_detail(
        self,
        response: scrapy.http.Response,
        *,
        surface_url: str,
        surface_vacancy_id: str,
    ) -> str:
        canonical_links = response.css('link[rel="canonical"]::attr(href)').getall()
        if len(canonical_links) != 1:
            self._fail(
                "detail_canonical_invalid",
                "DefTech detail must expose exactly one canonical link",
            )
        try:
            canonical_url = normalize_dou_url(response.urljoin(canonical_links[0]))
        except ValueError:
            self._fail("detail_canonical_invalid", "DefTech canonical link must be an absolute HTTP URL")
            raise AssertionError("unreachable")
        parsed = urlsplit(canonical_url)
        if parsed.scheme != "https" or parsed.netloc != "jobs.dou.ua":
            self._fail(
                "detail_canonical_invalid",
                "DefTech canonical link must use https://jobs.dou.ua",
            )
        if dou_vacancy_id_from_url(canonical_url) != surface_vacancy_id:
            self._fail(
                "detail_canonical_invalid",
                "DefTech canonical link vacancy ID must match the surface detail URL",
            )
        return canonical_url

    def _surface_url_for_detail(self, response: scrapy.http.Response, *, listing_item) -> str:
        """Keeps DefTech provenance when a stale DefTech slug needs DOU recovery."""
        del response
        return normalize_dou_url(listing_item["url"])

    def errback_detail(self, failure: Any):
        """Recovers only a DefTech 404 caused by a stale listing company slug."""
        response = getattr(getattr(failure, "value", None), "response", None)
        request = getattr(failure, "request", None)
        if (
            response is not None
            and response.status == 404
            and request is not None
            and urlsplit(request.url).netloc == "deftech.dou.ua"
        ):
            listing_item = request.cb_kwargs.get("listing_item")
            if listing_item is not None:
                fallback_url = self._regular_dou_fallback_url(listing_item["url"])
                self.logger.info("deftech-stale-detail-fallback url=%s", fallback_url)
                return scrapy.Request(
                    fallback_url,
                    callback=self.parse_detail,
                    cb_kwargs={"listing_item": listing_item},
                    errback=self.errback_detail,
                    dont_filter=True,
                )
        return super().errback_detail(failure)

    def _detail_source_metadata(self, *, canonical_url: str, surface_url: str) -> dict[str, str]:
        return {
            "detail_url": canonical_url,
            "discovery_surface": "deftech",
            "surface_url": surface_url,
            "profile": self.discovery_profile,
        }

    @staticmethod
    def _deftech_stat_key(key: str) -> str:
        return f"deftech/{key.removeprefix('dou/')}"

    @staticmethod
    def _regular_dou_fallback_url(surface_url: str) -> str:
        parsed = urlsplit(surface_url)
        path = parsed.path.removeprefix("/jobs") or "/"
        return urlunsplit(("https", "jobs.dou.ua", path, "", ""))
