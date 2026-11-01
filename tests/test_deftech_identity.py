from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from djinni_scraper.domain.vacancy import Company, Vacancy
from djinni_scraper.persistence.sqlite import (
    NotificationIntent,
    PersistenceStatus,
    SQLiteVacancyRepository,
    vacancy_content_hash,
)


OBSERVED_AT = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CANONICAL_URL = "https://jobs.dou.ua/companies/nda-recruitment/vacancies/358452"


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteVacancyRepository(tmp_path / "vacancies.db")
    repo.open()
    yield repo
    repo.close()


def _vacancy(*, profile: str, description: str = "Canonical description", surface: str) -> Vacancy:
    return Vacancy(
        source="dou",
        source_vacancy_id="358452",
        url=CANONICAL_URL,
        title="Data Engineer",
        description=description,
        company=Company(name="NDA Recruitment", url="https://jobs.dou.ua/companies/nda-recruitment/"),
        category="Data Engineer",
        published_at=date(2026, 9, 11),
        scraped_at=OBSERVED_AT,
        discovery_profiles=(profile,),
        raw_source_metadata={
            "source": {
                "discovery_surface": surface,
                "surface_url": (
                    "https://deftech.dou.ua/jobs/companies/nda-recruitment/vacancies/358452"
                    if surface == "deftech"
                    else CANONICAL_URL
                ),
                "profile": profile,
            }
        },
    )


def _persist(repository, vacancy: Vacancy, *, profile: str):
    return repository.persist_observation(
        vacancy,
        observation_complete=True,
        observed_at=OBSERVED_AT,
        notification_intent=NotificationIntent(route="main", profile_slug=profile),
    )


def _count(repository, table: str) -> int:
    return repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_deftech_metadata_is_excluded_from_content_hash():
    regular = _vacancy(profile="dou-data-engineer-0-1", surface="dou")
    deftech = _vacancy(profile="deftech-data-engineer", surface="deftech")

    assert vacancy_content_hash(regular) == vacancy_content_hash(deftech)


def test_regular_dou_then_deftech_reuses_one_vacancy_version_outbox_and_adds_discovery(repository):
    regular = _persist(
        repository,
        _vacancy(profile="dou-data-engineer-0-1", surface="dou"),
        profile="dou-data-engineer-0-1",
    )
    deftech = _persist(
        repository,
        _vacancy(profile="deftech-data-engineer", surface="deftech"),
        profile="deftech-data-engineer",
    )

    assert regular.status is PersistenceStatus.INSERTED
    assert regular.notification_enqueued is True
    assert deftech.status is PersistenceStatus.UNCHANGED
    assert deftech.vacancy_id == regular.vacancy_id
    assert deftech.discovery_added is True
    assert deftech.notification_enqueued is False
    assert _count(repository, "vacancies") == 1
    assert _count(repository, "vacancy_versions") == 1
    assert _count(repository, "notification_outbox") == 1
    assert _count(repository, "vacancy_publications") == 1
    assert tuple(discovery.profile_slug for discovery in repository.get_discoveries(regular.vacancy_id)) == (
        "deftech-data-engineer",
        "dou-data-engineer-0-1",
    )


def test_deftech_then_regular_dou_is_order_independent_and_keeps_one_inserted_notification(repository):
    deftech = _persist(
        repository,
        _vacancy(profile="deftech-data-engineer", surface="deftech"),
        profile="deftech-data-engineer",
    )
    regular = _persist(
        repository,
        _vacancy(profile="dou-data-engineer-0-1", surface="dou"),
        profile="dou-data-engineer-0-1",
    )

    assert deftech.status is PersistenceStatus.INSERTED
    assert deftech.notification_enqueued is True
    assert regular.status is PersistenceStatus.UNCHANGED
    assert regular.vacancy_id == deftech.vacancy_id
    assert regular.discovery_added is True
    assert regular.notification_enqueued is False
    assert _count(repository, "vacancies") == 1
    assert _count(repository, "vacancy_versions") == 1
    assert _count(repository, "notification_outbox") == 1


def test_meaningful_same_id_content_change_still_creates_update_and_version_without_second_insert_notification(repository):
    first = _persist(
        repository,
        _vacancy(profile="deftech-data-engineer", surface="deftech"),
        profile="deftech-data-engineer",
    )
    changed = _persist(
        repository,
        _vacancy(
            profile="dou-data-engineer-0-1",
            surface="dou",
            description="Meaningfully changed canonical description",
        ),
        profile="dou-data-engineer-0-1",
    )

    assert first.status is PersistenceStatus.INSERTED
    assert changed.status is PersistenceStatus.UPDATED
    assert changed.vacancy_id == first.vacancy_id
    assert changed.notification_enqueued is False
    assert _count(repository, "vacancies") == 1
    assert _count(repository, "vacancy_versions") == 2
    assert _count(repository, "notification_outbox") == 1
