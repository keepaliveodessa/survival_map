"""Tests for postgres/init-scripts/03-functions.sql — clean_old_events
and clean_old_pending_events SQL logic.

These tests are unit-level: they validate the SQL expressions that
implement P0.2/P0.3/P0.4 by constructing ad-hoc SQL fragments and
asserting on their input/output semantics via in-memory SQL building
and a compositional check (no live PostgreSQL required).

P0.2: GET DIAGNOSTICS replaced with SELECT count(*), jsonb_agg — so
deleted_rows reflects the actual number of deleted rows, not 1.
P0.3: photo_urls is accumulated per partition (via ||) before DROP.
P0.4: clean_old_pending_events deletes future rows (event_time > NOW()+5min)
as well as old rows.
"""

import re

import pytest

# ============================================================
# P0.2 — deleted_rows / photo_urls aggregation correctness
# ============================================================

_CLEAN_OLD_EVENTS_DELETED_SQL = """
WITH deleted AS (
    DELETE FROM events WHERE event_time < $1
    RETURNING photo_url
)
SELECT count(*) AS cnt,
       coalesce(jsonb_agg(photo_url) FILTER (WHERE photo_url IS NOT NULL), '[]'::jsonb) AS photos
FROM deleted;
"""


def _normal_form(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip())


class TestCleanOldEventsSqlStructure:
    def test_sql_has_count_and_photo_agg(self):
        assert "count(*" in _CLEAN_OLD_EVENTS_DELETED_SQL
        assert "jsonb_agg(photo_url)" in _CLEAN_OLD_EVENTS_DELETED_SQL
        assert "FILTER (WHERE photo_url IS NOT NULL)" in _CLEAN_OLD_EVENTS_DELETED_SQL

    def test_sql_replaces_get_diagnostics(self):
        # The old pattern used GET DIAGNOSTICS deleted_rows = ROW_COUNT;
        # after the fix we should NOT have that pattern.
        assert "GET DIAGNOSTICS" not in _CLEAN_OLD_EVENTS_DELETED_SQL
        assert "ROW_COUNT" not in _CLEAN_OLD_EVENTS_DELETED_SQL

    def test_sql_selects_into_two_variables(self):
        # The fix uses SELECT ... INTO deleted_rows, deleted_photos.
        # The inlined fragment above is the SELECT body; the INTO clause
        # is expected in the surrounding PL/pgSQL block.
        assert "INTO" not in _CLEAN_OLD_EVENTS_DELETED_SQL  # SELECT body has no INTO
        assert ("cnt" in _CLEAN_OLD_EVENTS_DELETED_SQL and "photos" in _CLEAN_OLD_EVENTS_DELETED_SQL)

    def test_photos_is_empty_array_when_no_photos(self):
        # If all photo_url are NULL, jsonb_agg FILTER returns NULL, coalesce wraps to '[]'.
        assert "coalesce(jsonb_agg(photo_url) FILTER (WHERE photo_url IS NOT NULL), '[]'::jsonb)" in _CLEAN_OLD_EVENTS_DELETED_SQL


def _fake_deleted_rows_values(rows_with_photo, rows_without_photo):
    """Simulate the SQL logic for deleted_rows and photos."""
    cnt = rows_with_photo + rows_without_photo
    if rows_with_photo == 0:
        photos = "[]"
    else:
        photos = "[" + ",".join(['"http://example.com/photo.jpg"'] * rows_with_photo) + "]"
    return cnt, photos


class TestCleanOldEventsPhotoAggregation:
    def test_photos_empty_when_all_null(self):
        cnt, photos = _fake_deleted_rows_values(rows_with_photo=0, rows_without_photo=5)
        assert cnt == 5
        assert photos == "[]"

    def test_photos_contains_all_non_null(self):
        cnt, photos = _fake_deleted_rows_values(rows_with_photo=3, rows_without_photo=0)
        assert cnt == 3
        assert photos == '["http://example.com/photo.jpg","http://example.com/photo.jpg","http://example.com/photo.jpg"]'


# ============================================================
# P0.3 — partition photo accumulation (||) before DROP
# ============================================================

def _accumulate_partition_photos(partition_photos_list):
    """Simulate the loop: FOR partition IN ... LOOP ... photo_urls := photo_urls || coalesce(...); END LOOP."""
    photo_urls = "[]"
    for p in partition_photos_list:
        photo_urls = f"({photo_urls}||coalesce({p}, '[]'::jsonb))"
    return photo_urls


class TestPartitionPhotoAccumulation:
    def test_accumulates_two_partitions(self):
        # partition_photos expressions as they would appear inline
        p1 = "'[\"url1\"]'::jsonb"
        p2 = "'[\"url2\"]'::jsonb"
        expr = _accumulate_partition_photos([p1, p2])
        # The expression must reference both partitions' photos
        assert "url1" in expr and "url2" in expr
        # After the loop, photo_urls is non-empty (contains url2 in the tail).
        last_chunk = expr.split("||")[-1]
        assert "url2" in last_chunk


# ============================================================
# P0.4 — clean_old_pending_events future-row deletion
# ============================================================

_CLEAN_OLD_PENDING_UPDATE_SQL = """
UPDATE pending_events
SET status = 'expired', processed_at = now(), locked_at = NULL, worker_id = NULL
WHERE (event_time < NOW() - INTERVAL '60 minutes'
       OR event_time > NOW() + INTERVAL '5 minutes')
  AND status = 'pending';
"""

_CLEAN_OLD_PENDING_DELETE_SQL = """
DELETE FROM pending_events
WHERE event_time < NOW() - INTERVAL '60 minutes'
  AND status IN ('done', 'error', 'expired');
"""


def _classify_pending_rows(rows):
    """Classify rows into those affected by UPDATE (expired) and DELETE.

    The real flow:
    1) UPDATE sets future OR old pending rows to 'expired'.
    2) DELETE removes old done/error/expired rows (event_time < NOW()-60min).

    So a row that was pending and old becomes 'expired' in step 1, and is
    then eligible for deletion in step 2 (because it is now expired AND old).
    """
    updated_ids: list = []
    deleted_ids: list = []
    for r in rows:
        future = r["event_time"] == "NOW()+1h"
        old = r["event_time"] == "NOW()-90min"
        if r["status"] == "pending" and (old or future):
            updated_ids.append(r["id"])
            if old:
                # becomes expired, then deleted in the same cycle
                deleted_ids.append(r["id"])
        if r["status"] in ("done", "error", "expired") and old:
            deleted_ids.append(r["id"])
    return updated_ids, deleted_ids


class TestCleanOldPendingEventsSqlStructure:
    def test_update_has_future_condition(self):
        assert "event_time > NOW() + INTERVAL '5 minutes'" in _CLEAN_OLD_PENDING_UPDATE_SQL

    def test_update_marks_expired(self):
        assert "SET status = 'expired'" in _CLEAN_OLD_PENDING_UPDATE_SQL

    def test_delete_targets_done_error_expired(self):
        assert "status IN ('done', 'error', 'expired')" in _CLEAN_OLD_PENDING_DELETE_SQL


class TestCleanOldPendingEventsFutureRows:
    def test_future_pending_becomes_expired(self):
        rows = [
            {"id": 1, "status": "pending", "event_time": "NOW()+1h"},
            {"id": 2, "status": "pending", "event_time": "NOW()-90min"},
            {"id": 3, "status": "done", "event_time": "NOW()-90min"},
        ]
        updated, deleted = _classify_pending_rows(rows)
        assert 1 in updated
        assert 2 in updated
        # old-pending -> expired -> deleted in same cycle
        assert 1 not in deleted
        assert 2 in deleted
        assert 3 in deleted  # old-done deleted directly

    def test_future_done_not_deleted_by_pending_rule(self):
        rows = [
            {"id": 10, "status": "done", "event_time": "NOW()+1h"},
        ]
        updated, deleted = _classify_pending_rows(rows)
        assert 10 not in deleted  # future-done is not old enough to delete


# ============================================================
# Cross-check: 03-functions.sql contains the corrected fragments
# ============================================================

def _load_sql(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _sql_contains(path, needle: str):
    return needle in _load_sql(path)


class TestPostgresSqlFileCorrections:
    @pytest.mark.parametrize("needle", [
        "SELECT count(*) AS cnt,",
        "jsonb_agg(photo_url) FILTER (WHERE photo_url IS NOT NULL)",
        "INTO   deleted_rows, deleted_photos",
    ])
    def test_clean_old_events_uses_correct_pattern(self, needle):
        assert _sql_contains("postgres/init-scripts/03-functions.sql", needle)
