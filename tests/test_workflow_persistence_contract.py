from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "djinni_scraper.yml"
SETTINGS_PATH = ROOT / "djinni_scraper" / "settings.py"
DB_PATH = "runtime/vacancies.db"
R2_OBJECT_KEY = "production/vacancies.db"
PROFILE_STEPS = (
    ("Run Djinni scraper for PYTHON jobs", "python"),
    ("Run Djinni scraper for DATA Engineering jobs", "data_engineer"),
    ("Run Djinni scraper for OTHER category jobs", "other"),
    ("Run Djinni scraper for SUPPORT jobs", "support"),
)
DOU_PROFILE_STEPS = (
    ("Run DOU scraper for Python 0-1", "dou-python-0-1"),
    ("Run DOU scraper for Python 1-3", "dou-python-1-3"),
    ("Run DOU scraper for Data Engineer 0-1", "dou-data-engineer-0-1"),
    ("Run DOU scraper for Data Engineer 1-3", "dou-data-engineer-1-3"),
    ("Run DOU scraper for First Job Remote", "dou-first-job-remote"),
    ("Run DOU scraper for Support Remote 0-1", "dou-support-remote-0-1"),
)
DEFTECH_PROFILE_STEP = ("Run DefTech scraper for Data Engineer", "deftech-data-engineer")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text()


def _settings() -> str:
    return SETTINGS_PATH.read_text()


def _step_block(workflow: str, name: str) -> str:
    start = workflow.index(f"- name: {name}")
    end = workflow.find("\n    - name:", start + 1)
    return workflow[start:] if end == -1 else workflow[start:end]


def test_workflow_uses_cloudflare_r2_as_the_only_remote_sqlite_state_store():
    workflow = _workflow()
    restore = _step_block(workflow, "Restore canonical SQLite state from Cloudflare R2")
    save = _step_block(workflow, "Persist canonical SQLite state to Cloudflare R2")

    assert f'default="{DB_PATH}"' in _settings()
    assert "actions/cache" not in workflow
    assert f"R2_DB_OBJECT_KEY: {R2_OBJECT_KEY}" in workflow
    for secret_name in (
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_ENDPOINT_URL",
        "R2_BUCKET_NAME",
    ):
        assert f"{secret_name}: ${{{{ secrets.{secret_name} }}}}" in workflow
    assert "AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}" in workflow
    assert "AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}" in workflow
    assert "AWS_DEFAULT_REGION: auto" in workflow
    assert 'AWS_EC2_METADATA_DISABLED: "true"' in workflow
    assert "aws s3api head-bucket" in restore
    assert "aws s3api head-object" in restore
    assert "$R2_DB_OBJECT_KEY" in restore
    assert "$R2_DB_OBJECT_KEY" in save


def test_workflow_restores_to_a_temporary_file_and_allows_only_verified_missing_object_bootstrap():
    restore = _step_block(_workflow(), "Restore canonical SQLite state from Cloudflare R2")

    assert "temporary_path=runtime/vacancies.db.download" in restore
    assert "--allow-supported-schema" in restore
    assert 'mv "$temporary_path" "$database_path"' in restore
    assert "NoSuchKey|404|Not Found" in restore
    assert "R2 state object not found; starting with a fresh database" in restore
    assert "refusing unsafe bootstrap" in restore
    assert "refusing to bootstrap a fresh database" in restore


def test_workflow_serializes_all_profiles_between_one_restore_and_one_validated_r2_upload():
    workflow = _workflow()
    restore_index = workflow.index("Restore canonical SQLite state from Cloudflare R2")
    prerequisites_index = workflow.index("Confirm crawler prerequisites")
    decision_index = workflow.index("Decide whether canonical SQLite state may be persisted")
    validation_index = workflow.index("Validate canonical SQLite state")
    upload_index = workflow.index("Persist canonical SQLite state to Cloudflare R2")

    assert "matrix:" not in workflow
    profile_indices = []
    for name, slug in PROFILE_STEPS:
        step = _step_block(workflow, name)
        profile_indices.append(workflow.index(name))
        assert f"DB_PATH: ./{DB_PATH}" in step
        assert f"python scripts/run_djinni_profile.py --profile {slug}" in step
        assert "id: crawl-" in step
        assert "if: always()" in step
        assert "steps.crawler-prerequisites.outputs.ready == 'true'" in step
        assert "TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}" in step
        assert "CHAT_ID: ${{ secrets.CHAT_ID }}" in step
        assert "NO_EXP_TELEGRAM_TOKEN: ${{ secrets.NO_EXP_TELEGRAM_TOKEN }}" in step
        assert "NO_EXP_CHAT_ID: ${{ secrets.NO_EXP_CHAT_ID }}" in step
    for name, slug in DOU_PROFILE_STEPS:
        step = _step_block(workflow, name)
        profile_indices.append(workflow.index(name))
        assert f"DB_PATH: ./{DB_PATH}" in step
        assert f"python scripts/run_dou_profile.py --profile {slug}" in step
        assert "id: crawl-dou-" in step
        assert "if: always()" in step
        assert "steps.crawler-prerequisites.outputs.ready == 'true'" in step
        assert "TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}" in step
        assert "CHAT_ID: ${{ secrets.CHAT_ID }}" in step
        assert "DJINNI_EMAIL" not in step
        assert "DJINNI_PASSWORD" not in step
    name, slug = DEFTECH_PROFILE_STEP
    step = _step_block(workflow, name)
    profile_indices.append(workflow.index(name))
    assert f"DB_PATH: ./{DB_PATH}" in step
    assert f"python scripts/run_deftech_profile.py --profile {slug}" in step
    assert "id: crawl-deftech-" in step
    assert "if: always()" in step
    assert "steps.crawler-prerequisites.outputs.ready == 'true'" in step
    assert "TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}" in step
    assert "CHAT_ID: ${{ secrets.CHAT_ID }}" in step
    assert "DJINNI_EMAIL" not in step
    assert "DJINNI_PASSWORD" not in step
    assert profile_indices == sorted(profile_indices)
    assert restore_index < prerequisites_index < profile_indices[0] < profile_indices[-1] < validation_index < decision_index < upload_index
    assert "scrapy crawl djinni" not in workflow
    assert "scrapy crawl dou" not in workflow


def test_workflow_validates_current_sqlite_before_upload_and_preserves_valid_partial_progress():
    workflow = _workflow()
    validation = _step_block(workflow, "Validate canonical SQLite state")
    upload = _step_block(workflow, "Persist canonical SQLite state to Cloudflare R2")

    assert "id: vacancy-db-state" in validation
    assert "if: always()" in validation
    assert "--require-current-schema" in validation
    assert "valid=true" in validation
    assert "valid=false" in validation
    decision = _step_block(workflow, "Decide whether canonical SQLite state may be persisted")
    assert "id: vacancy-db-crawl" in decision
    assert "successful_profiles" in decision
    assert "mutated_profiles" in decision
    assert 'upload=false' in decision
    assert 'if [[ "$RESTORED" == "true" && "$mutated_profiles" -gt 0 ]]; then' in decision
    assert 'elif [[ "$RESTORED" != "true" && "$successful_profiles" -gt 0 ]]; then' in decision
    assert 'RESTORED: ${{ steps.vacancy-db-restore.outputs.restored }}' in decision
    assert 'DJINNI_PYTHON_SUCCESS: ${{ steps.crawl-python.outputs.success }}' in decision
    assert 'DOU_SUPPORT_REMOTE_0_1_SUCCESS: ${{ steps.crawl-dou-support-remote-0-1.outputs.success }}' in decision
    assert 'DEFTECH_DATA_ENGINEER_SUCCESS: ${{ steps.crawl-deftech-data-engineer.outputs.success }}' in decision
    assert 'DJINNI_PYTHON_MUTATED: ${{ steps.crawl-python.outputs.state_mutated }}' in decision
    assert 'DOU_SUPPORT_REMOTE_0_1_MUTATED: ${{ steps.crawl-dou-support-remote-0-1.outputs.state_mutated }}' in decision
    assert 'DEFTECH_DATA_ENGINEER_MUTATED: ${{ steps.crawl-deftech-data-engineer.outputs.state_mutated }}' in decision
    assert (
        "if: always() && steps.vacancy-db-crawl.outputs.upload == 'true' "
        "&& steps.vacancy-db-state.outputs.valid == 'true'"
    ) in upload
    assert "aws s3 cp" in upload
    assert "--endpoint-url \"$R2_ENDPOINT_URL\"" in upload


def test_workflow_fails_closed_on_profile_failures_and_uses_current_actions_versions():
    workflow = _workflow()
    enforce = _step_block(workflow, "Fail workflow for unsuccessful scraper profiles")

    assert workflow.startswith("name: Run Job Scrapers Hourly")
    assert "actions/checkout@v4" in workflow
    assert "actions/setup-python@v5" in workflow
    assert "continue-on-error" not in workflow
    assert "if: always()" in enforce
    assert "exit 2" in enforce
    assert "failed_profiles" in enforce
    assert 'DEFTECH_DATA_ENGINEER_SUCCESS: ${{ steps.crawl-deftech-data-engineer.outputs.success }}' in enforce
    assert '"deftech-data-engineer:$DEFTECH_DATA_ENGINEER_SUCCESS"' in enforce


def test_workflow_keeps_single_writer_concurrency_and_never_commits_sqlite_state_to_git():
    workflow = _workflow()

    assert "concurrency:" in workflow
    assert "group: job-vacancy-crawler-${{ github.ref }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "queue: max" in workflow
    assert "if: github.ref == format('refs/heads/{0}', github.event.repository.default_branch)" in workflow
    assert "command -v aws" in workflow
    assert "aws --version" in workflow
    assert "jobs.db" not in workflow
    assert "git add" not in workflow
    assert "git commit" not in workflow
    assert "git push" not in workflow
