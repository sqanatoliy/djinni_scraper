"""SQLite persistence for canonical vacancy observations and notification outbox."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from djinni_scraper.domain.vacancy import Company, Compensation, LanguageRequirement, Location, Vacancy


SCHEMA_VERSION = 5
PERSISTENCE_STATE_MUTATED_STAT = "persistence/state_mutated"
_V1_SCHEMA_VERSION = 1
_V2_SCHEMA_VERSION = 2
_V3_SCHEMA_VERSION = 3
_V4_SCHEMA_VERSION = 4
VACANCIES_TABLE = "vacancies"
DISCOVERIES_TABLE = "vacancy_discoveries"
PUBLICATIONS_TABLE = "vacancy_publications"
OUTBOX_TABLE = "notification_outbox"
VERSIONS_TABLE = "vacancy_versions"
VACANCY_INSERTED_EVENT = "VACANCY_INSERTED"
VACANCY_REPUBLISHED_EVENT = "VACANCY_REPUBLISHED"

_V1_CANONICAL_COLUMNS = frozenset(
    {
        "id",
        "identity_key",
        "source",
        "source_vacancy_id",
        "url",
        "title",
        "description",
        "company_name",
        "company_url",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_unit",
        "employment_type",
        "experience_months",
        "locations_json",
        "applicant_locations_json",
        "category",
        "industry",
        "tags_json",
        "language_requirements_json",
        "published_at",
        "valid_through",
        "scraped_at",
        "discovery_profiles_json",
        "raw_source_metadata_json",
    }
)
_V2_CANONICAL_COLUMNS = (
    _V1_CANONICAL_COLUMNS - {"discovery_profiles_json"}
) | {"content_hash", "first_seen_at", "last_seen_at"}
_DISCOVERY_COLUMNS = frozenset(
    {"vacancy_id", "profile_slug", "first_discovered_at", "last_discovered_at"}
)
_PUBLICATION_COLUMNS = frozenset(
    {"id", "vacancy_id", "published_at", "first_observed_at", "last_observed_at"}
)
_V3_OUTBOX_COLUMNS = frozenset(
    {
        "id",
        "vacancy_id",
        "event_type",
        "route",
        "profile_slug",
        "created_at",
        "attempt_count",
        "last_attempt_at",
        "next_attempt_at",
        "delivered_at",
        "last_error",
    }
)
_OUTBOX_COLUMNS = _V3_OUTBOX_COLUMNS | {"publication_id", "event_key"}
_VERSION_COLUMNS = frozenset(
    {"id", "vacancy_id", "content_hash", "observed_at", "snapshot_complete", "snapshot_json"}
)
_CONTENT_FIELDS = (
    "url",
    "title",
    "description",
    "company",
    "compensation",
    "employment_type",
    "experience_months",
    "locations",
    "applicant_locations",
    "category",
    "industry",
    "tags",
    "language_requirements",
    "valid_through",
)


class PersistenceStatus(str, Enum):
    """Canonical-content outcome for one observed vacancy."""

    INSERTED = "INSERTED"
    REPUBLISHED = "REPUBLISHED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True, slots=True)
class PersistenceResult:
    """Result for canonical persistence and transactional notification intent."""

    status: PersistenceStatus
    vacancy_id: int
    discovery_added: bool
    publication_added: bool = False
    publication_id: int | None = None
    notification_enqueued: bool = False
    notification_event_id: int | None = None

    @property
    def inserted(self) -> bool:
        """Compatibility predicate for callers that only distinguish new rows."""
        return self.status is PersistenceStatus.INSERTED


@dataclass(frozen=True, slots=True)
class VacancyDiscovery:
    """One normalized search-profile observation for a logical vacancy."""

    profile_slug: str
    first_discovered_at: datetime
    last_discovered_at: datetime


@dataclass(frozen=True, slots=True)
class VacancyPublication:
    """One normalized Djinni publication day for a logical vacancy."""

    id: int
    vacancy_id: int
    published_at: str
    first_observed_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True, slots=True)
class VacancyVersion:
    """One immutable canonical content state retained for future analytics."""

    id: int
    vacancy_id: int
    content_hash: str
    observed_at: datetime
    snapshot_complete: bool
    snapshot: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class NotificationIntent:
    """Observation-local routing metadata for a notification-eligible event."""

    route: str
    profile_slug: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.route, str) or not self.route.strip():
            raise ValueError("notification route must be a non-empty string")
        object.__setattr__(self, "route", self.route.strip())
        if self.profile_slug is not None:
            if not isinstance(self.profile_slug, str) or not self.profile_slug.strip():
                raise ValueError("notification profile_slug must be a non-empty string or None")
            object.__setattr__(self, "profile_slug", self.profile_slug.strip())


@dataclass(frozen=True, slots=True)
class NotificationOutboxEvent:
    """Persisted delivery state for one notification intent."""

    id: int
    vacancy_id: int
    publication_id: int | None
    event_type: str
    event_key: str
    route: str
    profile_slug: str | None
    created_at: datetime
    attempt_count: int
    last_attempt_at: datetime | None
    next_attempt_at: datetime | None
    delivered_at: datetime | None
    last_error: str | None


class IdentityConflictError(RuntimeError):
    """Raised when fallback URL and stable-ID rows point to different vacancies."""


class SQLiteVacancyRepository:
    """Transactional boundary for canonical snapshots and discovery provenance."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.connection: sqlite3.Connection | None = None
        self._state_mutated = False

    @property
    def state_mutated(self) -> bool:
        """Whether this opened repository lifecycle committed durable SQLite changes."""
        return self._state_mutated

    def open(self) -> None:
        """Opens the database, enables foreign keys, and initializes or migrates schema."""
        if self.connection is not None:
            return
        self._state_mutated = False
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        try:
            if initialize_schema(self.connection):
                self._state_mutated = True
        except Exception:
            self.connection.close()
            self.connection = None
            raise

    def close(self) -> None:
        """Closes the owned SQLite connection."""
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def persist_observation(
        self,
        vacancy: Vacancy,
        *,
        discovery_profiles: Iterable[str] | None = None,
        observation_complete: bool,
        observed_at: datetime,
        notification_intent: NotificationIntent | None = None,
    ) -> PersistenceResult:
        """Atomically resolves identity, persists the snapshot, and records discovery.

        Incomplete observations may create a new row but never overwrite the
        canonical snapshot of an existing row.  They still update ``last_seen``
        and discovery provenance.
        """
        connection = self._require_connection()
        observed_at_text = _utc_timestamp(observed_at)
        profiles = _normalized_profiles(
            vacancy.discovery_profiles if discovery_profiles is None else discovery_profiles
        )
        record = vacancy_to_record(vacancy)
        publication_day = normalize_publication_day(vacancy.published_at)
        changes_before = connection.total_changes

        try:
            connection.execute("BEGIN")
            existing_row, promoted = self._resolve_existing_observation(connection, vacancy)
            publication_id = None
            publication_added = False
            republished = False
            create_version = False
            if existing_row is None:
                vacancy_id = self._insert_vacancy_row(connection, record, observed_at_text)
                status = PersistenceStatus.INSERTED
                create_version = True
                if publication_day is not None:
                    publication_id = self._insert_publication(
                        connection,
                        vacancy_id=vacancy_id,
                        publication_day=publication_day,
                        observed_at=observed_at_text,
                    )
                    publication_added = True
            else:
                vacancy_id = existing_row["id"]
                status = PersistenceStatus.UNCHANGED
                if promoted:
                    self._promote_fallback_identity(connection, vacancy_id, record)
                had_publication_history = self._has_publication_history(connection, vacancy_id)
                if publication_day is not None:
                    publication_id, publication_added, republished = self._observe_publication(
                        connection,
                        vacancy_id=vacancy_id,
                        publication_day=publication_day,
                        observed_at=observed_at_text,
                        had_publication_history=had_publication_history,
                    )

                historical_publication = publication_added and had_publication_history and not republished
                content_changed = existing_row["content_hash"] != record["content_hash"]
                baseline_date_became_known = (
                    publication_added
                    and not had_publication_history
                    and existing_row["published_at"] is None
                )
                if republished:
                    status = PersistenceStatus.REPUBLISHED
                update_current_snapshot = observation_complete and not historical_publication and (
                    content_changed or republished or baseline_date_became_known
                )
                if update_current_snapshot:
                    self._update_vacancy_row(connection, vacancy_id, record, observed_at_text)
                    if not republished:
                        status = PersistenceStatus.UPDATED
                else:
                    self._touch_last_seen(connection, vacancy_id, observed_at_text)

                create_version = (
                    observation_complete
                    and not historical_publication
                    and not self._has_vacancy_version(
                        connection,
                        vacancy_id=vacancy_id,
                        content_hash=record["content_hash"],
                        snapshot_complete=True,
                    )
                )

            if create_version:
                self._record_vacancy_version(
                    connection,
                    vacancy_id=vacancy_id,
                    vacancy=vacancy,
                    content_hash=record["content_hash"],
                    observed_at=observed_at_text,
                    snapshot_complete=observation_complete,
                )

            discovery_added = self._merge_discoveries(
                connection,
                vacancy_id=vacancy_id,
                profile_slugs=profiles,
                observed_at=observed_at_text,
            )
            notification_event_id = None
            if (
                status in (PersistenceStatus.INSERTED, PersistenceStatus.REPUBLISHED)
                and notification_intent is not None
            ):
                notification_event_id = self._enqueue_notification(
                    connection,
                    vacancy_id=vacancy_id,
                    publication_id=publication_id if status is PersistenceStatus.REPUBLISHED else None,
                    event_type=(
                        VACANCY_INSERTED_EVENT
                        if status is PersistenceStatus.INSERTED
                        else VACANCY_REPUBLISHED_EVENT
                    ),
                    notification_intent=notification_intent,
                    created_at=observed_at_text,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        self._record_committed_row_changes(changes_before)

        return PersistenceResult(
            status=status,
            vacancy_id=vacancy_id,
            discovery_added=discovery_added,
            publication_added=publication_added,
            publication_id=publication_id,
            notification_enqueued=notification_event_id is not None,
            notification_event_id=notification_event_id,
        )

    def insert_vacancy(self, vacancy: Vacancy) -> PersistenceResult:
        """Compatibility wrapper for the former v1 repository entry point."""
        return self.persist_observation(
            vacancy,
            observation_complete=True,
            observed_at=vacancy.scraped_at,
        )

    def get_vacancy(self, vacancy: Vacancy) -> Vacancy | None:
        """Reads a vacancy by stable identity, with URL fallback when necessary."""
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM vacancies WHERE identity_key = ?", (vacancy_identity_key(vacancy),)
        ).fetchone()
        if row is None and vacancy.source_vacancy_id is not None:
            row = connection.execute(
                "SELECT * FROM vacancies WHERE identity_key = ?",
                (_identity_key(vacancy.source, None, vacancy.url),),
            ).fetchone()
        return self._vacancy_from_row(row) if row else None

    def get_vacancy_by_identity_key(self, identity_key: str) -> Vacancy | None:
        """Reads one canonical vacancy from its internal identity key."""
        row = self._require_connection().execute(
            "SELECT * FROM vacancies WHERE identity_key = ?", (identity_key,)
        ).fetchone()
        return self._vacancy_from_row(row) if row else None

    def get_vacancy_by_id(self, vacancy_id: int) -> Vacancy | None:
        """Reads one canonical snapshot for a notification event."""
        row = self._require_connection().execute(
            "SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)
        ).fetchone()
        return self._vacancy_from_row(row) if row else None

    def get_notification_event(self, event_id: int) -> NotificationOutboxEvent | None:
        """Reads one persisted outbox event for diagnostics and tests."""
        row = self._require_connection().execute(
            "SELECT * FROM notification_outbox WHERE id = ?", (event_id,)
        ).fetchone()
        return _row_to_outbox_event(row) if row else None

    def get_publication(self, publication_id: int) -> VacancyPublication | None:
        """Reads one publication occurrence for event-specific presentation."""
        row = self._require_connection().execute(
            """
            SELECT id, vacancy_id, published_at, first_observed_at, last_observed_at
            FROM vacancy_publications
            WHERE id = ?
            """,
            (publication_id,),
        ).fetchone()
        return _row_to_publication(row) if row else None

    def list_publications(self, vacancy_id: int) -> tuple[VacancyPublication, ...]:
        """Returns deterministic publication history for one logical vacancy."""
        rows = self._require_connection().execute(
            """
            SELECT id, vacancy_id, published_at, first_observed_at, last_observed_at
            FROM vacancy_publications
            WHERE vacancy_id = ?
            ORDER BY published_at, id
            """,
            (vacancy_id,),
        )
        return tuple(_row_to_publication(row) for row in rows)

    def list_vacancy_versions(self, vacancy_id: int) -> tuple[VacancyVersion, ...]:
        """Returns immutable canonical states in deterministic observation order."""
        rows = self._require_connection().execute(
            """
            SELECT id, vacancy_id, content_hash, observed_at, snapshot_complete, snapshot_json
            FROM vacancy_versions
            WHERE vacancy_id = ?
            ORDER BY observed_at, id
            """,
            (vacancy_id,),
        )
        return tuple(_row_to_vacancy_version(row) for row in rows)

    def get_latest_publication(self, vacancy_id: int) -> VacancyPublication | None:
        """Returns the latest known normalized publication day, if one exists."""
        publications = self.list_publications(vacancy_id)
        return max(publications, key=lambda publication: publication.published_at, default=None)

    def list_due_notification_events(
        self, *, now: datetime, limit: int
    ) -> tuple[NotificationOutboxEvent, ...]:
        """Returns pending notifications in deterministic FIFO-like order."""
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("notification batch limit must be a positive integer")
        now_text = _utc_timestamp(now)
        rows = self._require_connection().execute(
            """
            SELECT * FROM notification_outbox
            WHERE delivered_at IS NULL
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at, id
            LIMIT ?
            """,
            (now_text, limit),
        )
        return tuple(_row_to_outbox_event(row) for row in rows)

    def mark_notification_delivered(self, event_id: int, *, attempted_at: datetime) -> None:
        """Records one successful delivery in its own short transaction."""
        attempted_at_text = _utc_timestamp(attempted_at)
        self._update_notification_delivery(
            event_id,
            last_attempt_at=attempted_at_text,
            delivered_at=attempted_at_text,
            next_attempt_at=None,
            last_error=None,
        )

    def mark_notification_failed(
        self,
        event_id: int,
        *,
        attempted_at: datetime,
        next_attempt_at: datetime,
        error: str,
    ) -> None:
        """Keeps a failed event pending and persists bounded safe diagnostics."""
        self._update_notification_delivery(
            event_id,
            last_attempt_at=_utc_timestamp(attempted_at),
            delivered_at=None,
            next_attempt_at=_utc_timestamp(next_attempt_at),
            last_error=_safe_outbox_error(error),
        )

    def get_discoveries(self, vacancy_id: int) -> tuple[VacancyDiscovery, ...]:
        """Returns normalized provenance in deterministic profile order."""
        rows = self._require_connection().execute(
            """
            SELECT profile_slug, first_discovered_at, last_discovered_at
            FROM vacancy_discoveries
            WHERE vacancy_id = ?
            ORDER BY profile_slug
            """,
            (vacancy_id,),
        )
        return tuple(
            VacancyDiscovery(
                profile_slug=row["profile_slug"],
                first_discovered_at=_parse_datetime(row["first_discovered_at"]),
                last_discovered_at=_parse_datetime(row["last_discovered_at"]),
            )
            for row in rows
        )

    def get_discovery_profiles(self, vacancy_id: int) -> tuple[str, ...]:
        """Returns profile slugs for callers that do not need timestamps."""
        return tuple(discovery.profile_slug for discovery in self.get_discoveries(vacancy_id))

    @staticmethod
    def _has_publication_history(connection: sqlite3.Connection, vacancy_id: int) -> bool:
        return connection.execute(
            "SELECT 1 FROM vacancy_publications WHERE vacancy_id = ? LIMIT 1", (vacancy_id,)
        ).fetchone() is not None

    @staticmethod
    def _has_vacancy_version(
        connection: sqlite3.Connection,
        *,
        vacancy_id: int,
        content_hash: str,
        snapshot_complete: bool,
    ) -> bool:
        return connection.execute(
            """
            SELECT 1 FROM vacancy_versions
            WHERE vacancy_id = ? AND content_hash = ? AND snapshot_complete = ?
            LIMIT 1
            """,
            (vacancy_id, content_hash, int(snapshot_complete)),
        ).fetchone() is not None

    @staticmethod
    def _record_vacancy_version(
        connection: sqlite3.Connection,
        *,
        vacancy_id: int,
        vacancy: Vacancy,
        content_hash: str,
        observed_at: str,
        snapshot_complete: bool,
    ) -> None:
        """Records one distinct content/completeness state in the caller's transaction."""
        connection.execute(
            """
            INSERT INTO vacancy_versions (
                vacancy_id, content_hash, observed_at, snapshot_complete, snapshot_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                vacancy_id,
                content_hash,
                observed_at,
                int(snapshot_complete),
                vacancy_version_snapshot(vacancy),
            ),
        )

    def _observe_publication(
        self,
        connection: sqlite3.Connection,
        *,
        vacancy_id: int,
        publication_day: str,
        observed_at: str,
        had_publication_history: bool,
    ) -> tuple[int, bool, bool]:
        """Stores one normalized publication day and returns ``(id, added, republished)``."""
        existing = connection.execute(
            """
            SELECT id FROM vacancy_publications
            WHERE vacancy_id = ? AND published_at = ?
            """,
            (vacancy_id, publication_day),
        ).fetchone()
        if existing is not None:
            connection.execute(
                """
                UPDATE vacancy_publications
                SET last_observed_at = ?
                WHERE id = ? AND last_observed_at IS NOT ?
                """,
                (observed_at, existing["id"], observed_at),
            )
            return existing["id"], False, False

        latest = self.get_latest_publication(vacancy_id) if had_publication_history else None
        publication_id = self._insert_publication(
            connection,
            vacancy_id=vacancy_id,
            publication_day=publication_day,
            observed_at=observed_at,
        )
        republished = latest is not None and publication_day > latest.published_at
        return publication_id, True, republished

    def _resolve_existing_observation(
        self, connection: sqlite3.Connection, vacancy: Vacancy
    ) -> tuple[sqlite3.Row | None, bool]:
        if vacancy.source_vacancy_id is None:
            return self._row_by_identity_key(connection, vacancy_identity_key(vacancy)), False

        stable_row = self._row_by_identity_key(connection, vacancy_identity_key(vacancy))
        fallback_row = self._row_by_identity_key(
            connection,
            _identity_key(vacancy.source, None, vacancy.url),
        )
        if stable_row is not None and fallback_row is not None and stable_row["id"] != fallback_row["id"]:
            raise IdentityConflictError(
                "Stable source identity and URL fallback resolve to different canonical vacancies"
            )
        if stable_row is not None:
            return stable_row, False
        if fallback_row is not None:
            return fallback_row, True
        return None, False

    @staticmethod
    def _row_by_identity_key(connection: sqlite3.Connection, identity_key: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM vacancies WHERE identity_key = ?", (identity_key,)
        ).fetchone()

    @staticmethod
    def _insert_vacancy_row(
        connection: sqlite3.Connection, record: Mapping[str, Any], observed_at: str
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO vacancies (
                identity_key, source, source_vacancy_id, url, title, description,
                company_name, company_url,
                salary_min, salary_max, salary_currency, salary_unit,
                employment_type, experience_months,
                locations_json, applicant_locations_json,
                category, industry, tags_json, language_requirements_json,
                published_at, valid_through, scraped_at,
                raw_source_metadata_json, content_hash, first_seen_at, last_seen_at
            ) VALUES (
                :identity_key, :source, :source_vacancy_id, :url, :title, :description,
                :company_name, :company_url,
                :salary_min, :salary_max, :salary_currency, :salary_unit,
                :employment_type, :experience_months,
                :locations_json, :applicant_locations_json,
                :category, :industry, :tags_json, :language_requirements_json,
                :published_at, :valid_through, :scraped_at,
                :raw_source_metadata_json, :content_hash, :first_seen_at, :last_seen_at
            )
            """,
            {**record, "first_seen_at": observed_at, "last_seen_at": observed_at},
        )
        return cursor.lastrowid

    @staticmethod
    def _update_vacancy_row(
        connection: sqlite3.Connection,
        vacancy_id: int,
        record: Mapping[str, Any],
        observed_at: str,
    ) -> None:
        connection.execute(
            """
            UPDATE vacancies SET
                identity_key = :identity_key,
                source = :source,
                source_vacancy_id = :source_vacancy_id,
                url = :url,
                title = :title,
                description = :description,
                company_name = :company_name,
                company_url = :company_url,
                salary_min = :salary_min,
                salary_max = :salary_max,
                salary_currency = :salary_currency,
                salary_unit = :salary_unit,
                employment_type = :employment_type,
                experience_months = :experience_months,
                locations_json = :locations_json,
                applicant_locations_json = :applicant_locations_json,
                category = :category,
                industry = :industry,
                tags_json = :tags_json,
                language_requirements_json = :language_requirements_json,
                published_at = :published_at,
                valid_through = :valid_through,
                scraped_at = :scraped_at,
                raw_source_metadata_json = :raw_source_metadata_json,
                content_hash = :content_hash,
                last_seen_at = :last_seen_at
            WHERE id = :vacancy_id
            """,
            {**record, "vacancy_id": vacancy_id, "last_seen_at": observed_at},
        )

    @staticmethod
    def _promote_fallback_identity(
        connection: sqlite3.Connection, vacancy_id: int, record: Mapping[str, Any]
    ) -> None:
        connection.execute(
            """
            UPDATE vacancies
            SET identity_key = :identity_key, source_vacancy_id = :source_vacancy_id
            WHERE id = :vacancy_id
            """,
            {
                "identity_key": record["identity_key"],
                "source_vacancy_id": record["source_vacancy_id"],
                "vacancy_id": vacancy_id,
            },
        )

    @staticmethod
    def _touch_last_seen(connection: sqlite3.Connection, vacancy_id: int, observed_at: str) -> None:
        connection.execute(
            """
            UPDATE vacancies
            SET last_seen_at = ?
            WHERE id = ? AND last_seen_at IS NOT ?
            """,
            (observed_at, vacancy_id, observed_at),
        )

    @staticmethod
    def _insert_publication(
        connection: sqlite3.Connection,
        *,
        vacancy_id: int,
        publication_day: str,
        observed_at: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO vacancy_publications (
                vacancy_id, published_at, first_observed_at, last_observed_at
            ) VALUES (?, ?, ?, ?)
            """,
            (vacancy_id, publication_day, observed_at, observed_at),
        )
        return cursor.lastrowid

    @staticmethod
    def _merge_discoveries(
        connection: sqlite3.Connection,
        *,
        vacancy_id: int,
        profile_slugs: Iterable[str],
        observed_at: str,
    ) -> bool:
        discovery_added = False
        for profile_slug in profile_slugs:
            row = connection.execute(
                """
                SELECT 1 FROM vacancy_discoveries
                WHERE vacancy_id = ? AND profile_slug = ?
                """,
                (vacancy_id, profile_slug),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO vacancy_discoveries (
                        vacancy_id, profile_slug, first_discovered_at, last_discovered_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (vacancy_id, profile_slug, observed_at, observed_at),
                )
                discovery_added = True
            else:
                connection.execute(
                    """
                    UPDATE vacancy_discoveries
                    SET last_discovered_at = ?
                    WHERE vacancy_id = ? AND profile_slug = ? AND last_discovered_at IS NOT ?
                    """,
                    (observed_at, vacancy_id, profile_slug, observed_at),
                )
        return discovery_added

    @staticmethod
    def _enqueue_notification(
        connection: sqlite3.Connection,
        *,
        vacancy_id: int,
        publication_id: int | None,
        event_type: str,
        notification_intent: NotificationIntent,
        created_at: str,
    ) -> int:
        if event_type == VACANCY_INSERTED_EVENT:
            event_key = f"vacancy:{vacancy_id}:inserted"
        elif event_type == VACANCY_REPUBLISHED_EVENT and publication_id is not None:
            event_key = f"publication:{publication_id}:republished"
        else:
            raise ValueError("notification event requires a supported event type and identity")
        cursor = connection.execute(
            """
            INSERT INTO notification_outbox (
                vacancy_id, publication_id, event_type, event_key, route, profile_slug, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vacancy_id,
                publication_id,
                event_type,
                event_key,
                notification_intent.route,
                notification_intent.profile_slug,
                created_at,
            ),
        )
        return cursor.lastrowid

    def _update_notification_delivery(
        self,
        event_id: int,
        *,
        last_attempt_at: str,
        delivered_at: str | None,
        next_attempt_at: str | None,
        last_error: str | None,
    ) -> None:
        connection = self._require_connection()
        changes_before = connection.total_changes
        try:
            connection.execute("BEGIN")
            cursor = connection.execute(
                """
                UPDATE notification_outbox
                SET attempt_count = attempt_count + 1,
                    last_attempt_at = :last_attempt_at,
                    delivered_at = :delivered_at,
                    next_attempt_at = :next_attempt_at,
                    last_error = :last_error
                WHERE id = :event_id AND delivered_at IS NULL
                """,
                {
                    "event_id": event_id,
                    "last_attempt_at": last_attempt_at,
                    "delivered_at": delivered_at,
                    "next_attempt_at": next_attempt_at,
                    "last_error": last_error,
                },
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Notification event {event_id} is not pending")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        self._record_committed_row_changes(changes_before)

    def _record_committed_row_changes(self, changes_before: int) -> None:
        """Latches mutation only after a transaction that committed row changes.

        ``sqlite3.Connection.total_changes`` is scoped to this repository's
        single connection.  A baseline is taken for each transaction, and the
        value is considered only after ``commit`` succeeds; a rolled-back
        transaction therefore cannot flip the lifecycle signal.
        """
        if self._require_connection().total_changes > changes_before:
            self._state_mutated = True

    def _vacancy_from_row(self, row: Mapping[str, Any]) -> Vacancy:
        return row_to_vacancy(
            row,
            discovery_profiles=self.get_discovery_profiles(row["id"]),
        )

    def _require_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("SQLiteVacancyRepository is not open")
        return self.connection


def initialize_schema(connection: sqlite3.Connection) -> bool:
    """Creates or migrates schema and reports whether durable schema state changed."""
    connection.execute("PRAGMA foreign_keys = ON")
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    vacancy_columns = _table_columns(connection, VACANCIES_TABLE)
    schema_mutated = False

    if current_version == 0 and not vacancy_columns:
        _create_new_v5_schema(connection)
        return True
    if current_version in (0, _V1_SCHEMA_VERSION) and _V1_CANONICAL_COLUMNS.issubset(
        vacancy_columns
    ):
        _migrate_v1_to_v2(connection)
        current_version = _V2_SCHEMA_VERSION
        schema_mutated = True
    if current_version == _V2_SCHEMA_VERSION:
        _validate_v2_schema(connection)
        _migrate_v2_to_v3(connection)
        current_version = _V3_SCHEMA_VERSION
        schema_mutated = True
    if current_version == _V3_SCHEMA_VERSION:
        _validate_v3_schema(connection)
        _migrate_v3_to_v4(connection)
        current_version = _V4_SCHEMA_VERSION
        schema_mutated = True
    if current_version == _V4_SCHEMA_VERSION:
        _validate_v4_schema(connection)
        _migrate_v4_to_v5(connection)
        current_version = SCHEMA_VERSION
        schema_mutated = True
    if current_version == SCHEMA_VERSION:
        _validate_v5_schema(connection)
        return schema_mutated
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported SQLite schema version {current_version}; latest supported version is {SCHEMA_VERSION}"
        )
    raise RuntimeError("Existing vacancies table is not canonical schema v1, v2, v3, v4, or v5")


def validate_supported_schema(connection: sqlite3.Connection) -> int:
    """Validates an existing database that repository initialization can open or migrate.

    This deliberately does not create an empty schema or perform migrations, so
    callers can inspect a downloaded state file before making it authoritative
    local input.
    """
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    vacancy_columns = _table_columns(connection, VACANCIES_TABLE)

    if current_version in (0, _V1_SCHEMA_VERSION) and _V1_CANONICAL_COLUMNS.issubset(vacancy_columns):
        return current_version
    if current_version == _V2_SCHEMA_VERSION:
        _validate_v2_schema(connection)
        return current_version
    if current_version == _V3_SCHEMA_VERSION:
        _validate_v3_schema(connection)
        return current_version
    if current_version == _V4_SCHEMA_VERSION:
        _validate_v4_schema(connection)
        return current_version
    if current_version == SCHEMA_VERSION:
        _validate_v5_schema(connection)
        return current_version
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported SQLite schema version {current_version}; latest supported version is {SCHEMA_VERSION}"
        )
    raise RuntimeError("Existing vacancies table is not canonical schema v1, v2, v3, v4, or v5")


def _create_new_v5_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN")
        _create_vacancies_table(connection)
        _create_discoveries_table(connection)
        _create_publications_table(connection)
        _create_outbox_table(connection)
        _create_vacancy_versions_table(connection)
        _create_v2_indexes(connection)
        _create_outbox_indexes(connection)
        _create_vacancy_version_indexes(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Migrates v1 rows without discarding canonical state or provenance evidence."""
    try:
        connection.execute("BEGIN")
        connection.execute("ALTER TABLE vacancies RENAME TO vacancies_v1")
        v1_rows = list(connection.execute("SELECT * FROM vacancies_v1 ORDER BY id"))
        _create_vacancies_table(connection)

        discoveries_to_backfill: list[tuple[int, tuple[str, ...], str]] = []
        for row in v1_rows:
            vacancy = _v1_row_to_vacancy(row)
            record = vacancy_to_record(vacancy)
            seen_at = _utc_timestamp(_parse_datetime(row["scraped_at"]))
            connection.execute(
                """
                INSERT INTO vacancies (
                    id, identity_key, source, source_vacancy_id, url, title, description,
                    company_name, company_url,
                    salary_min, salary_max, salary_currency, salary_unit,
                    employment_type, experience_months,
                    locations_json, applicant_locations_json,
                    category, industry, tags_json, language_requirements_json,
                    published_at, valid_through, scraped_at,
                    raw_source_metadata_json, content_hash, first_seen_at, last_seen_at
                ) VALUES (
                    :id, :identity_key, :source, :source_vacancy_id, :url, :title, :description,
                    :company_name, :company_url,
                    :salary_min, :salary_max, :salary_currency, :salary_unit,
                    :employment_type, :experience_months,
                    :locations_json, :applicant_locations_json,
                    :category, :industry, :tags_json, :language_requirements_json,
                    :published_at, :valid_through, :scraped_at,
                    :raw_source_metadata_json, :content_hash, :first_seen_at, :last_seen_at
                )
                """,
                {
                    **record,
                    "id": row["id"],
                    "first_seen_at": seen_at,
                    "last_seen_at": seen_at,
                },
            )
            discoveries_to_backfill.append(
                (row["id"], _profiles_from_v1_row(row), seen_at)
            )

        connection.execute("DROP TABLE vacancies_v1")
        _create_discoveries_table(connection)
        for vacancy_id, profiles, seen_at in discoveries_to_backfill:
            for profile_slug in profiles:
                connection.execute(
                    """
                    INSERT INTO vacancy_discoveries (
                        vacancy_id, profile_slug, first_discovered_at, last_discovered_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (vacancy_id, profile_slug, seen_at, seen_at),
                )
        _create_v2_indexes(connection)
        connection.execute(f"PRAGMA user_version = {_V2_SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _create_vacancies_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE vacancies (
            id INTEGER PRIMARY KEY,
            identity_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            source_vacancy_id TEXT,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            company_name TEXT,
            company_url TEXT,
            salary_min NUMERIC,
            salary_max NUMERIC,
            salary_currency TEXT,
            salary_unit TEXT,
            employment_type TEXT,
            experience_months NUMERIC,
            locations_json TEXT NOT NULL,
            applicant_locations_json TEXT NOT NULL,
            category TEXT,
            industry TEXT,
            tags_json TEXT NOT NULL,
            language_requirements_json TEXT NOT NULL,
            published_at TEXT,
            valid_through TEXT,
            scraped_at TEXT NOT NULL,
            raw_source_metadata_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )


def _create_discoveries_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE vacancy_discoveries (
            vacancy_id INTEGER NOT NULL,
            profile_slug TEXT NOT NULL,
            first_discovered_at TEXT NOT NULL,
            last_discovered_at TEXT NOT NULL,
            PRIMARY KEY (vacancy_id, profile_slug),
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id) ON DELETE CASCADE
        ) WITHOUT ROWID
        """
    )


def _create_publications_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE vacancy_publications (
            id INTEGER PRIMARY KEY,
            vacancy_id INTEGER NOT NULL,
            published_at TEXT NOT NULL,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id) ON DELETE CASCADE,
            UNIQUE (vacancy_id, published_at)
        )
        """
    )


def _create_outbox_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE notification_outbox (
            id INTEGER PRIMARY KEY,
            vacancy_id INTEGER NOT NULL,
            publication_id INTEGER,
            event_type TEXT NOT NULL,
            event_key TEXT NOT NULL UNIQUE,
            route TEXT NOT NULL,
            profile_slug TEXT,
            created_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            next_attempt_at TEXT,
            delivered_at TEXT,
            last_error TEXT,
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id) ON DELETE CASCADE,
            FOREIGN KEY (publication_id) REFERENCES vacancy_publications(id) ON DELETE CASCADE
        )
        """
    )


def _create_vacancy_versions_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE vacancy_versions (
            id INTEGER PRIMARY KEY,
            vacancy_id INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            snapshot_complete INTEGER NOT NULL CHECK (snapshot_complete IN (0, 1)),
            snapshot_json TEXT NOT NULL,
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id) ON DELETE CASCADE,
            UNIQUE (vacancy_id, content_hash, snapshot_complete)
        )
        """
    )


def _create_v3_outbox_table(connection: sqlite3.Connection) -> None:
    """Creates the legacy v3 outbox only while migrating an older database."""
    connection.execute(
        """
        CREATE TABLE notification_outbox (
            id INTEGER PRIMARY KEY,
            vacancy_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            route TEXT NOT NULL,
            profile_slug TEXT,
            created_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            next_attempt_at TEXT,
            delivered_at TEXT,
            last_error TEXT,
            UNIQUE (vacancy_id, event_type),
            FOREIGN KEY (vacancy_id) REFERENCES vacancies(id) ON DELETE CASCADE
        )
        """
    )


def _create_v2_indexes(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE INDEX idx_vacancies_source ON vacancies (source)")
    connection.execute(
        "CREATE INDEX idx_vacancies_source_vacancy_id ON vacancies (source, source_vacancy_id)"
    )
    connection.execute("CREATE INDEX idx_vacancies_published_at ON vacancies (published_at)")
    connection.execute("CREATE INDEX idx_vacancies_last_seen_at ON vacancies (last_seen_at)")
    connection.execute(
        "CREATE INDEX idx_vacancy_discoveries_profile_slug ON vacancy_discoveries (profile_slug)"
    )


def _create_outbox_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE INDEX idx_notification_outbox_pending
        ON notification_outbox (delivered_at, next_attempt_at, created_at, id)
        """
    )
    connection.execute(
        "CREATE INDEX idx_notification_outbox_vacancy_id ON notification_outbox (vacancy_id)"
    )


def _create_vacancy_version_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX idx_vacancy_versions_vacancy_observed "
        "ON vacancy_versions (vacancy_id, observed_at, id)"
    )


def _validate_v2_schema(connection: sqlite3.Connection) -> None:
    vacancy_columns = _table_columns(connection, VACANCIES_TABLE)
    discovery_columns = _table_columns(connection, DISCOVERIES_TABLE)
    if not _V2_CANONICAL_COLUMNS.issubset(vacancy_columns) or not _DISCOVERY_COLUMNS.issubset(
        discovery_columns
    ):
        raise RuntimeError("Existing database is marked schema v2 but does not match the canonical v2 schema")


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Adds an empty transactional outbox without inventing historical notifications."""
    try:
        connection.execute("BEGIN")
        _create_v3_outbox_table(connection)
        _create_outbox_indexes(connection)
        connection.execute(f"PRAGMA user_version = {_V3_SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _validate_v3_schema(connection: sqlite3.Connection) -> None:
    _validate_v2_schema(connection)
    outbox_columns = _table_columns(connection, OUTBOX_TABLE)
    if not _V3_OUTBOX_COLUMNS.issubset(outbox_columns):
        raise RuntimeError("Existing database is marked schema v3 but does not match the notification outbox schema")


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """Adds publication history and generic outbox identity without replaying notifications."""
    legacy_outbox_table = "notification_outbox_v3"
    try:
        connection.execute("BEGIN")
        connection.execute(f"ALTER TABLE {OUTBOX_TABLE} RENAME TO {legacy_outbox_table}")
        for index_name in (
            "idx_notification_outbox_pending",
            "idx_notification_outbox_vacancy_id",
        ):
            connection.execute(f"DROP INDEX IF EXISTS {index_name}")
        _create_publications_table(connection)

        vacancy_rows = list(connection.execute("SELECT * FROM vacancies ORDER BY id"))
        for row in vacancy_rows:
            vacancy = row_to_vacancy(row)
            connection.execute(
                "UPDATE vacancies SET content_hash = ? WHERE id = ?",
                (vacancy_content_hash(vacancy), row["id"]),
            )
            publication_day = normalize_publication_day(vacancy.published_at)
            if publication_day is not None:
                connection.execute(
                    """
                    INSERT INTO vacancy_publications (
                        vacancy_id, published_at, first_observed_at, last_observed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (row["id"], publication_day, row["first_seen_at"], row["last_seen_at"]),
                )

        _create_outbox_table(connection)
        legacy_events = list(connection.execute(f"SELECT * FROM {legacy_outbox_table} ORDER BY id"))
        for event in legacy_events:
            connection.execute(
                """
                INSERT INTO notification_outbox (
                    id, vacancy_id, publication_id, event_type, event_key, route, profile_slug,
                    created_at, attempt_count, last_attempt_at, next_attempt_at, delivered_at, last_error
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["vacancy_id"],
                    event["event_type"],
                    _legacy_outbox_event_key(event),
                    event["route"],
                    event["profile_slug"],
                    event["created_at"],
                    event["attempt_count"],
                    event["last_attempt_at"],
                    event["next_attempt_at"],
                    event["delivered_at"],
                    event["last_error"],
                ),
            )
        connection.execute(f"DROP TABLE {legacy_outbox_table}")
        _create_outbox_indexes(connection)
        connection.execute(f"PRAGMA user_version = {_V4_SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _validate_v4_schema(connection: sqlite3.Connection) -> None:
    _validate_v2_schema(connection)
    publication_columns = _table_columns(connection, PUBLICATIONS_TABLE)
    outbox_columns = _table_columns(connection, OUTBOX_TABLE)
    if not _PUBLICATION_COLUMNS.issubset(publication_columns) or not _OUTBOX_COLUMNS.issubset(
        outbox_columns
    ):
        raise RuntimeError("Existing database is marked schema v4 but does not match publication/outbox schema")


def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    """Backfills one baseline content state per v4 vacancy without emitting events."""
    try:
        connection.execute("BEGIN")
        _create_vacancy_versions_table(connection)
        profile_rows = connection.execute(
            """
            SELECT vacancy_id, profile_slug
            FROM vacancy_discoveries
            ORDER BY vacancy_id, profile_slug
            """
        )
        profiles_by_vacancy: dict[int, list[str]] = {}
        for profile_row in profile_rows:
            profiles_by_vacancy.setdefault(profile_row["vacancy_id"], []).append(
                profile_row["profile_slug"]
            )

        for row in connection.execute("SELECT * FROM vacancies ORDER BY id"):
            vacancy = row_to_vacancy(
                row,
                discovery_profiles=tuple(profiles_by_vacancy.get(row["id"], ())),
            )
            connection.execute(
                """
                INSERT INTO vacancy_versions (
                    vacancy_id, content_hash, observed_at, snapshot_complete, snapshot_json
                ) VALUES (?, ?, ?, 1, ?)
                """,
                (
                    row["id"],
                    row["content_hash"],
                    row["scraped_at"],
                    vacancy_version_snapshot(vacancy),
                ),
            )
        _create_vacancy_version_indexes(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _validate_v5_schema(connection: sqlite3.Connection) -> None:
    _validate_v4_schema(connection)
    version_columns = _table_columns(connection, VERSIONS_TABLE)
    if not _VERSION_COLUMNS.issubset(version_columns):
        raise RuntimeError("Existing database is marked schema v5 but does not match vacancy version schema")


def vacancy_identity_key(vacancy: Vacancy) -> str:
    """Returns stable ``source + ID`` identity or normalized URL fallback identity."""
    return _identity_key(vacancy.source, vacancy.source_vacancy_id, vacancy.url)


def canonical_content_projection(vacancy: Vacancy) -> dict[str, Any]:
    """Returns deterministic source snapshot content used for change detection.

    ``scraped_at``, identity, discovery provenance, persistence timestamps, and
    raw metadata are intentionally excluded.  Djinni raw site metadata includes
    volatile listing counters, so hashing it would create false content updates.
    """
    values = vacancy.to_dict()
    projection = {field: values[field] for field in _CONTENT_FIELDS}
    projection["url"] = _normalized_identity_url(projection["url"])
    return projection


def vacancy_content_hash(vacancy: Vacancy) -> str:
    """Hashes the deterministic canonical content projection with SHA-256."""
    serialized = _json_dump(canonical_content_projection(vacancy)).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def vacancy_version_snapshot(vacancy: Vacancy) -> str:
    """Serializes a source-neutral canonical snapshot for immutable history."""
    snapshot = vacancy.to_dict()
    snapshot["url"] = _normalized_identity_url(snapshot["url"])
    return _json_dump(snapshot)


def vacancy_to_record(vacancy: Vacancy) -> dict[str, Any]:
    """Flattens canonical values and deterministic JSON serialization for SQLite."""
    data = vacancy.to_dict()
    company = data["company"] or {}
    compensation = data["compensation"] or {}
    return {
        "identity_key": vacancy_identity_key(vacancy),
        "source": data["source"],
        "source_vacancy_id": data["source_vacancy_id"],
        "url": _normalized_identity_url(data["url"]),
        "title": data["title"],
        "description": data["description"],
        "company_name": company.get("name"),
        "company_url": company.get("url"),
        "salary_min": compensation.get("min_amount"),
        "salary_max": compensation.get("max_amount"),
        "salary_currency": compensation.get("currency"),
        "salary_unit": compensation.get("unit"),
        "employment_type": data["employment_type"],
        "experience_months": data["experience_months"],
        "locations_json": _json_dump(data["locations"]),
        "applicant_locations_json": _json_dump(data["applicant_locations"]),
        "category": data["category"],
        "industry": data["industry"],
        "tags_json": _json_dump(data["tags"]),
        "language_requirements_json": _json_dump(data["language_requirements"]),
        "published_at": normalize_publication_timestamp(vacancy.published_at),
        "valid_through": data["valid_through"],
        "scraped_at": data["scraped_at"],
        "raw_source_metadata_json": _json_dump(data["raw_source_metadata"]),
        "content_hash": vacancy_content_hash(vacancy),
    }


def row_to_vacancy(
    row: Mapping[str, Any], *, discovery_profiles: Iterable[str] = ()
) -> Vacancy:
    """Reconstructs a canonical Vacancy from a v2 row and normalized provenance."""
    company = _company_from_row(row)
    compensation = _compensation_from_row(row)
    return Vacancy(
        source=row["source"],
        source_vacancy_id=row["source_vacancy_id"],
        url=row["url"],
        title=row["title"],
        description=row["description"],
        company=company,
        compensation=compensation,
        employment_type=row["employment_type"],
        experience_months=row["experience_months"],
        locations=tuple(Location(**value) for value in _json_load(row["locations_json"])),
        applicant_locations=tuple(Location(**value) for value in _json_load(row["applicant_locations_json"])),
        category=row["category"],
        industry=row["industry"],
        tags=tuple(_json_load(row["tags_json"])),
        language_requirements=tuple(
            LanguageRequirement(**value) for value in _json_load(row["language_requirements_json"])
        ),
        published_at=_parse_date_or_datetime(row["published_at"]),
        valid_through=_parse_date_or_datetime(row["valid_through"]),
        scraped_at=_parse_datetime(row["scraped_at"]),
        discovery_profiles=tuple(discovery_profiles),
        raw_source_metadata=_json_load(row["raw_source_metadata_json"]),
    )


def _row_to_outbox_event(row: Mapping[str, Any]) -> NotificationOutboxEvent:
    return NotificationOutboxEvent(
        id=row["id"],
        vacancy_id=row["vacancy_id"],
        publication_id=row["publication_id"],
        event_type=row["event_type"],
        event_key=row["event_key"],
        route=row["route"],
        profile_slug=row["profile_slug"],
        created_at=_parse_datetime(row["created_at"]),
        attempt_count=row["attempt_count"],
        last_attempt_at=_parse_optional_datetime(row["last_attempt_at"]),
        next_attempt_at=_parse_optional_datetime(row["next_attempt_at"]),
        delivered_at=_parse_optional_datetime(row["delivered_at"]),
        last_error=row["last_error"],
    )


def _row_to_publication(row: Mapping[str, Any]) -> VacancyPublication:
    return VacancyPublication(
        id=row["id"],
        vacancy_id=row["vacancy_id"],
        published_at=row["published_at"],
        first_observed_at=_parse_datetime(row["first_observed_at"]),
        last_observed_at=_parse_datetime(row["last_observed_at"]),
    )


def _row_to_vacancy_version(row: Mapping[str, Any]) -> VacancyVersion:
    return VacancyVersion(
        id=row["id"],
        vacancy_id=row["vacancy_id"],
        content_hash=row["content_hash"],
        observed_at=_parse_datetime(row["observed_at"]),
        snapshot_complete=bool(row["snapshot_complete"]),
        snapshot=_json_load(row["snapshot_json"]),
    )


def _legacy_outbox_event_key(event: Mapping[str, Any]) -> str:
    if event["event_type"] == VACANCY_INSERTED_EVENT:
        return f"vacancy:{event['vacancy_id']}:inserted"
    return f"vacancy:{event['vacancy_id']}:{str(event['event_type']).casefold()}"


def _v1_row_to_vacancy(row: Mapping[str, Any]) -> Vacancy:
    return row_to_vacancy(row, discovery_profiles=_profiles_from_v1_row(row))


def _profiles_from_v1_row(row: Mapping[str, Any]) -> tuple[str, ...]:
    try:
        raw_profiles = _json_load(row["discovery_profiles_json"])
    except (TypeError, json.JSONDecodeError):
        raw_profiles = ()
    return _normalized_profiles(raw_profiles if isinstance(raw_profiles, list) else ())


def _identity_key(source: str, source_vacancy_id: str | None, url: str) -> str:
    if source_vacancy_id is not None:
        return _json_dump([source, "id", source_vacancy_id])
    return _json_dump([source, "url", _normalized_identity_url(url)])


def _normalized_identity_url(url: str) -> str:
    """Applies the minimal Djinni URL rules needed for fallback identity.

    The adapter already emits canonical Djinni URLs.  This second normalization
    protects repository callers from trailing slashes, fragments, query strings,
    and obvious tracking parameters without introducing fuzzy matching.
    """
    parsed = urlsplit(url.strip())
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def normalize_publication_timestamp(value: date | datetime | None) -> str | None:
    """Preserves source date/datetime precision in one non-null ISO representation."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise ValueError("published_at must be a date, datetime, or None")


def normalize_publication_day(value: date | datetime | None) -> str | None:
    """Returns Djinni's publication-occurrence key without changing source display precision.

    Djinni's detail JSON-LD and listing fallback expose different timestamp
    precisions, so a calendar day is the reliable repost signal.  Naive values
    retain their source calendar date; offset-aware values use the UTC date so
    equivalent instants get the same deterministic key without consulting the
    host timezone.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).date().isoformat()
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise ValueError("published_at must be a date, datetime, or None")


def _normalized_profiles(profiles: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                profile.strip()
                for profile in profiles
                if isinstance(profile, str) and profile.strip()
            }
        )
    )


def _utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _company_from_row(row: Mapping[str, Any]) -> Company | None:
    if row["company_name"] is None and row["company_url"] is None:
        return None
    return Company(name=row["company_name"], url=row["company_url"])


def _compensation_from_row(row: Mapping[str, Any]) -> Compensation | None:
    values = (row["salary_min"], row["salary_max"], row["salary_currency"], row["salary_unit"])
    if all(value is None for value in values):
        return None
    return Compensation(
        min_amount=row["salary_min"],
        max_amount=row["salary_max"],
        currency=row["salary_currency"],
        unit=row["salary_unit"],
    )


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str) -> Any:
    return json.loads(value)


def _parse_date_or_datetime(value: str | None) -> date | datetime | None:
    if value is None:
        return None
    if "T" in value or " " in value:
        return _parse_datetime(value)
    return date.fromisoformat(value)


def _parse_datetime(value: str) -> datetime:
    normalized_value = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized_value)


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


def _safe_outbox_error(error: str) -> str:
    """Stores concise diagnostics without allowing unbounded or multiline data."""
    normalized = " ".join(str(error).split())
    return normalized[:500] or "notification delivery failed"
