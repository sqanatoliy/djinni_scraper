from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "djinni_scraper.yml"
SETTINGS_PATH = ROOT / "djinni_scraper" / "settings.py"
DB_PATH = "runtime/vacancies.db"
CACHE_PREFIX = "djinni-vacancies-db-v1-"
PROFILE_STEPS = (
    ("Run Djinni scraper for PYTHON jobs", "python"),
    ("Run Djinni scraper for DATA Engineering jobs", "data_engineer"),
    ("Run Djinni scraper for OTHER category jobs", "other"),
    ("Run Djinni scraper for SUPPORT jobs", "support"),
)


def _workflow() -> str:
    return WORKFLOW_PATH.read_text()


def _settings() -> str:
    return SETTINGS_PATH.read_text()


def _step_block(workflow: str, name: str) -> str:
    start = workflow.index(f"- name: {name}")
    end = workflow.find("\n    - name:", start + 1)
    return workflow[start:] if end == -1 else workflow[start:end]


def test_workflow_uses_one_run_unique_restore_save_cache_lifecycle():
    workflow = _workflow()
    restore = _step_block(workflow, "Restore canonical SQLite state")
    save = _step_block(workflow, "Save canonical SQLite state")

    assert f'default="{DB_PATH}"' in _settings()
    assert workflow.count("actions/cache/restore@v4") == 1
    assert workflow.count("actions/cache/save@v4") == 1
    assert "id: vacancy-db-restore" in restore
    assert f"path: {DB_PATH}" in restore
    assert f"key: {CACHE_PREFIX}${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}" in restore
    assert f"{CACHE_PREFIX}\n" in restore
    assert f"path: {DB_PATH}" in save
    assert "key: ${{ steps.vacancy-db-restore.outputs.cache-primary-key }}" in save


def test_workflow_serializes_all_profiles_between_one_restore_and_one_validated_save():
    workflow = _workflow()
    restore_index = workflow.index("Restore canonical SQLite state")
    validation_index = workflow.index("Validate canonical SQLite state")
    save_index = workflow.index("Save canonical SQLite state")

    assert "matrix:" not in workflow
    assert restore_index < validation_index < save_index
    profile_indices = []
    for name, slug in PROFILE_STEPS:
        step = _step_block(workflow, name)
        profile_indices.append(workflow.index(name))
        assert f"DB_PATH: ./{DB_PATH}" in step
        assert f"scrapy crawl djinni -a profile={slug}" in step
    assert profile_indices == sorted(profile_indices)
    assert restore_index < profile_indices[0] < profile_indices[-1] < validation_index


def test_workflow_saves_valid_partial_progress_but_not_an_invalid_database():
    workflow = _workflow()
    validation = _step_block(workflow, "Validate canonical SQLite state")
    save = _step_block(workflow, "Save canonical SQLite state")

    assert "id: vacancy-db-state" in validation
    assert "if: always()" in validation
    assert "sqlite3.connect" in validation
    assert "mode=ro" in validation
    assert 'PRAGMA quick_check;' in validation
    assert 'PRAGMA user_version;' in validation
    assert "name = 'vacancies'" in validation
    assert 'output.write(f"valid={' in validation
    assert "if: always() && steps.vacancy-db-state.outputs.valid == 'true'" in save


def test_workflow_prevents_lost_updates_from_overlapping_runs_and_non_main_dispatches():
    workflow = _workflow()

    assert "concurrency:" in workflow
    assert "group: djinni-vacancy-crawler-${{ github.ref }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "queue: max" in workflow
    assert "if: github.ref == format('refs/heads/{0}', github.event.repository.default_branch)" in workflow


def test_workflow_never_commits_or_pushes_sqlite_state_to_git():
    workflow = _workflow()

    assert "jobs.db" not in workflow
    assert "git add" not in workflow
    assert "git commit" not in workflow
    assert "git push" not in workflow
