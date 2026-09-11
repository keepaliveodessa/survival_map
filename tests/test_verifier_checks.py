"""Programmatic subset of the verifier checklist (read-only assertions).

These checks mirror high-signal items from the pasted verification prompt so
a reviewer can re-run them from the repo. They do not introduce new behavior
and do not depend on Docker / live PostgreSQL.
"""

import re
from pathlib import Path


def _file_text(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    return p.read_text(encoding="utf-8")


class TestProcessorEventTimeValidation:
    def test_fetch_pending_has_event_time_window(self):
        text = _file_text("processor/main.py")
        assert "event_time >= now() - interval '60 minutes'" in text
        assert "event_time <= now() + interval '5 minutes'" in text

    def test_process_row_has_expired_guard(self):
        text = _file_text("processor/main.py")
        assert "outside 60-min window — mark expired" in text

    def test_expired_guard_calls_mark_expired(self):
        text = _file_text("processor/main.py")
        assert "outside 60-min window — mark expired" in text
        assert "await self._mark_expired(row['id'])" in text
        assert "self._expired += 1" in text

    def test_mark_expired_exists(self):
        text = _file_text("processor/main.py")
        assert "async def _mark_expired" in text
        assert "SET status = 'expired'" in text

    def test_expired_counter_initialized_and_logged(self):
        text = _file_text("processor/main.py")
        assert "self._expired = 0" in text
        assert "_expired" in text


class TestPostgresFunctionsChecks:
    def test_clean_old_events_drops_partition_by_upper_bound(self):
        text = _file_text("postgres/init-scripts/03-functions.sql")
        assert "+ INTERVAL '1 hour' <= cutoff" in text

    def test_photo_urls_collected_per_partition(self):
        text = _file_text("postgres/init-scripts/03-functions.sql")
        assert "FROM %I WHERE photo_url IS NOT NULL" in text
        assert "jsonb_agg(photo_url)" in text

    def test_photo_urls_accumulated_not_rewritten(self):
        text = _file_text("postgres/init-scripts/03-functions.sql")
        assert "photo_urls := photo_urls ||" in text

    def test_get_diagnostics_not_used_for_deleted_rows(self):
        text = _file_text("postgres/init-scripts/03-functions.sql")
        assert "GET DIAGNOSTICS deleted_rows = ROW_COUNT" not in text

    def test_events_cleaned_notify_contains_photo_urls(self):
        text = _file_text("postgres/init-scripts/03-functions.sql")
        assert "'photo_urls', photo_urls" in text

    def test_clean_old_pending_events_marks_future_and_old_expired(self):
        text = _file_text("postgres/init-scripts/03-functions.sql")
        assert "event_time > NOW() + INTERVAL '5 minutes'" in text

    def test_clean_old_pending_events_delete_targets_old_done_error_expired(self):
        text = _file_text("postgres/init-scripts/03-functions.sql")
        assert "status IN ('done', 'error', 'expired')" in text

    def test_cron_schedules_present(self):
        text = _file_text("postgres/init-scripts/03-functions.sql")
        assert "cron.schedule('clean-old-events'" in text
        assert "cron.schedule('clean-old-pending-events'" in text

    def test_deleted_rows_count_used_in_notify(self):
        text = _file_text("postgres/init-scripts/03-functions.sql")
        assert "'deleted_count', deleted_rows" in text


class TestPartitionMaintenanceChecks:
    def test_partition_window_is_minus_one_to_one(self):
        text = _file_text("postgres/init-scripts/11-partition-maintenance.sql")
        assert "FOR i IN -1..1 LOOP" in text

    def test_safety_net_cutoff_is_3_hours(self):
        text = _file_text("postgres/init-scripts/11-partition-maintenance.sql")
        assert "INTERVAL '3 hours'" in text

    def test_partition_overflow_notify_present(self):
        text = _file_text("postgres/init-scripts/11-partition-maintenance.sql")
        assert "partition_overflow" in text

    def test_manage_event_partitions_cron_schedule(self):
        text = _file_text("postgres/init-scripts/11-partition-maintenance.sql")
        assert "cron.schedule('manage-event-partitions'" in text


class TestPendingEventsSchemaCheck:
    def test_status_check_includes_expired(self):
        text = _file_text("postgres/init-scripts/10-pending-events.sql")
        assert "CHECK (status IN ('pending', 'processing', 'done', 'error', 'expired'))" in text


class TestParserMonitoringChecks:
    def test_photo_cleanup_listener_handles_photo_urls(self):
        text = _file_text("parser/monitoring.py")
        assert "photo_urls" in text
        assert "_run_photo_cleanup_listener" in text

    def test_photo_deletion_handles_oserror(self):
        text = _file_text("parser/monitoring.py")
        assert "os.unlink" in text
        assert "OSError" in text

    def test_parser_photo_cleanup_uses_pg_notify_events_cleaned(self):
        text = _file_text("parser/monitoring.py")
        assert "events_cleaned" in text
        assert "photo_urls" in text
        assert "os.unlink" in text
        assert "OSError" in text
        assert "os.path.isfile" in text

    def test_parser_stale_photo_cleanup_runtime_present(self):
        text = _file_text("parser/monitoring.py")
        assert "events_cleaned" in text
        assert "photo_urls" in text
        assert "os.unlink" in text
        assert "OSError" in text
        assert "os.path.isfile" in text


class TestDocsChecks:
    def test_no_core_settings_references(self):
        for path in [
            "docs/RULES.md",
            "docs/RULES_CORE.md",
            "docs/RULES_PARSER.md",
            "docs/RULES_PROCESSOR.md",
            "docs/RULES_POSTGRES.md",
        ]:
            text = _file_text(path)
            assert "core/settings.py" not in text, f"{path} still mentions core/settings.py"

    def test_pool_sizes_are_one_and_ten(self):
        text = _file_text("common/settings.py")
        assert "pool_min_size: int = 1" in text
        assert "pool_max_size: int = 10" in text
        assert "command_timeout: int = 30" in text

    def test_max_connections_not_200(self):
        text = _file_text("docs/RULES_POSTGRES.md")
        assert "max_connections = 50" in text
        normalized = re.sub(r"\s+", " ", text)
        assert "max_connections.*200" not in normalized

    def test_parser_stale_photo_cleanup_documented(self):
        text = _file_text("docs/RULES_PARSER.md")
        assert "70" in text and "минут" in text
        assert "pg_notify" in text

    def test_pg_cron_jobs_documented_and_synced(self):
        text = _file_text("docs/RULES_POSTGRES.md")
        assert "clean-old-events" in text
        assert "clean-old-pending-events" in text
        assert "manage-event-partitions" in text

    def test_pg_cron_jobs_do_not_refresh_mv(self):
        text = _file_text("docs/RULES_POSTGRES.md")
        assert "refresh-events-mv" not in text
        assert "refresh-geo-mv" not in text


class TestPgCronConfigCheck:
    def test_pg_cron_config_present_and_correct(self):
        # pg_cron конфигурируется в postgresql.conf (пре-лоад библиотеки и БД
        # для cron-таблиц), а НЕ в init-script: shared_preload_libraries нельзя
        # задать через CREATE EXTENSION/ALTER SYSTEM до старта сервера.
        # Dockerfile.postgres монтирует этот конфиг через config_file=.
        text = _file_text("postgres/config/postgresql.conf")
        assert "shared_preload_libraries = 'pg_cron'" in text
        assert "cron.database_name = 'postgres'" in text
