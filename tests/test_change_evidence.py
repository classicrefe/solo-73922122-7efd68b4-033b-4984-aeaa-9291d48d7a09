"""Change evidence captured at publish time (``run_changes``).

The evidence is written inside the same ``BEGIN IMMEDIATE`` transaction as
the catalogue and watermark, so a failed/cancelled round or a mid-commit
error leaves no evidence behind.  Unchanged closed-interval re-reads and
withdrawals of unknown identifiers are not changes.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.store import Store


def _stage(store, run_id, identifier, *, title=None, creator=None,
           datestamp, deleted=False):
    store.stage_records(
        run_id,
        [
            {
                "identifier": identifier,
                "title": title,
                "creator": creator,
                "datestamp": datestamp,
                "deleted": deleted,
            }
        ],
        0,
    )


def _publish(store: Store, run_id: str, until: str):
    store.set_window(run_id, until)
    result = store.commit_run(run_id)
    assert result["committed"] is True
    return result


def test_full_load_records_only_additions(store):
    run = store.create_run()
    _stage(store, run["id"], "oai:1", title="题一", creator="甲",
           datestamp="2026-01-01T00:00:01Z")
    _stage(store, run["id"], "oai:2", title="题二", creator="乙",
           datestamp="2026-01-01T00:00:02Z")
    _publish(store, run["id"], "2026-01-01T00:00:10Z")

    changes = store.run_changes(run["id"])
    assert [c["change_type"] for c in changes] == ["added", "added"]
    first = changes[0]
    assert first["before_title"] is None and first["before_creator"] is None
    assert first["after_title"] == "题一" and first["after_creator"] == "甲"
    assert store.get_run(run["id"])["changes_tracked"] == 1


def test_correction_keeps_real_before_and_after_values(store):
    first = store.create_run()
    _stage(store, first["id"], "oai:1", title="旧标题", creator="原作者",
           datestamp="2026-01-01T00:00:01Z")
    _publish(store, first["id"], "2026-01-01T00:00:10Z")

    second = store.create_run()
    _stage(store, second["id"], "oai:1", title="新标题", creator="原作者",
           datestamp="2026-01-01T00:00:11Z")
    _publish(store, second["id"], "2026-01-01T00:00:20Z")

    changes = store.run_changes(second["id"])
    assert len(changes) == 1
    change = changes[0]
    assert change["change_type"] == "corrected"
    assert change["before_title"] == "旧标题"
    assert change["before_creator"] == "原作者"
    assert change["after_title"] == "新标题"
    assert change["after_creator"] == "原作者"


def test_creator_only_change_is_a_correction(store):
    first = store.create_run()
    _stage(store, first["id"], "oai:1", title="题", creator="甲",
           datestamp="2026-01-01T00:00:01Z")
    _publish(store, first["id"], "2026-01-01T00:00:10Z")

    second = store.create_run()
    _stage(store, second["id"], "oai:1", title="题", creator="乙",
           datestamp="2026-01-01T00:00:11Z")
    _publish(store, second["id"], "2026-01-01T00:00:20Z")

    changes = store.run_changes(second["id"])
    assert len(changes) == 1
    assert changes[0]["change_type"] == "corrected"


def test_closed_interval_reread_of_unchanged_record_is_not_a_change(store):
    first = store.create_run()
    _stage(store, first["id"], "oai:1", title="题", creator="甲",
           datestamp="2026-01-01T00:00:01Z")
    _publish(store, first["id"], "2026-01-01T00:00:10Z")

    second = store.create_run()
    # Same identifier, same title/creator, even with a fresher datestamp:
    # the bibliography itself did not change.
    _stage(store, second["id"], "oai:1", title="题", creator="甲",
           datestamp="2026-01-01T00:00:11Z")
    _publish(store, second["id"], "2026-01-01T00:00:20Z")

    assert store.run_changes(second["id"]) == []
    # Zero changes is a real, trackable result — distinct from "unsampleable".
    assert store.get_run(second["id"])["changes_tracked"] == 1


def test_withdrawal_of_unknown_identifier_is_not_a_change(store):
    run = store.create_run()
    _stage(store, run["id"], "ghost", datestamp="2026-01-01T00:00:01Z",
           deleted=True)
    _publish(store, run["id"], "2026-01-01T00:00:10Z")

    assert store.run_changes(run["id"]) == []
    assert store.catalog_page()["total"] == 0


def test_known_withdrawal_records_before_values(store):
    first = store.create_run()
    _stage(store, first["id"], "oai:1", title="将被撤回", creator="甲",
           datestamp="2026-01-01T00:00:01Z")
    _publish(store, first["id"], "2026-01-01T00:00:10Z")

    second = store.create_run()
    _stage(store, second["id"], "oai:1",
           datestamp="2026-01-01T00:00:11Z", deleted=True)
    _publish(store, second["id"], "2026-01-01T00:00:20Z")

    changes = store.run_changes(second["id"])
    assert len(changes) == 1
    change = changes[0]
    assert change["change_type"] == "withdrawn"
    assert change["before_title"] == "将被撤回"
    assert change["before_creator"] == "甲"
    assert change["after_title"] is None and change["after_creator"] is None
    assert store.catalog_page()["total"] == 0


def test_later_rounds_never_rewrite_older_evidence(store):
    first = store.create_run()
    _stage(store, first["id"], "oai:1", title="v1", creator=None,
           datestamp="2026-01-01T00:00:01Z")
    _publish(store, first["id"], "2026-01-01T00:00:10Z")

    second = store.create_run()
    _stage(store, second["id"], "oai:1", title="v2", creator=None,
           datestamp="2026-01-01T00:00:11Z")
    _publish(store, second["id"], "2026-01-01T00:00:20Z")

    first_evidence = store.run_changes(first["id"])
    assert len(first_evidence) == 1
    assert first_evidence[0]["after_title"] == "v1"
    assert first_evidence[0]["change_type"] == "added"


def test_failed_and_cancelled_rounds_leave_no_evidence(store):
    failed = store.create_run()
    store.set_window(failed["id"], "2026-01-01T00:00:10Z")
    _stage(store, failed["id"], "oai:f", title="失败轮记录",
           datestamp="2026-01-01T00:00:01Z")
    store.finish_failed(failed["id"], "模拟失败")
    assert store.run_changes(failed["id"]) is None
    assert store.get_run(failed["id"])["changes_tracked"] == 0

    cancelled = store.create_run()
    store.set_window(cancelled["id"], "2026-01-01T00:00:20Z")
    _stage(store, cancelled["id"], "oai:c", title="取消轮记录",
           datestamp="2026-01-01T00:00:11Z")
    store.cancel_run(cancelled["id"], "模拟取消")
    assert store.run_changes(cancelled["id"]) is None
    assert store.get_run(cancelled["id"])["changes_tracked"] == 0
    assert store.catalog_page()["total"] == 0


def test_publish_write_failure_leaves_no_evidence(tmp_path, monkeypatch):
    db = tmp_path / "atomic-changes.sqlite3"
    store = Store(db)
    run = store.create_run()
    store.set_window(run["id"], "2026-01-01T00:00:10Z")
    _stage(store, run["id"], "oai:1", title="题", datestamp="2026-01-01T00:00:01Z")

    class ExplodingConnection:
        def __init__(self, inner):
            self._inner = inner
            self._inserts = 0

        def execute(self, sql, *args, **kwargs):
            if sql.strip().startswith("INSERT INTO run_changes"):
                self._inserts += 1
                if self._inserts == 1:
                    raise sqlite3.OperationalError("simulated disk error")
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real_conn = store._conn
    monkeypatch.setattr(store, "_conn", ExplodingConnection(real_conn))
    with pytest.raises(sqlite3.OperationalError):
        store.commit_run(run["id"])
    monkeypatch.setattr(store, "_conn", real_conn)

    # Transaction rolled back: no evidence, no catalogue, no watermark.
    assert store.run_changes(run["id"]) is None
    assert store.published_state()["watermark"] is None
    assert store.catalog_page()["total"] == 0
    store.finish_failed(run["id"], "simulated")
    store.close()


def test_legacy_successful_round_is_unsampleable_not_zero_changes(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    store = Store(db)
    run = store.create_run()
    _stage(store, run["id"], "oai:legacy:1", title="旧轮书目",
           datestamp="2025-12-31T00:00:00Z")
    _publish(store, run["id"], "2026-01-01T00:00:10Z")

    # Simulate a database written by the pre-change-tracking release: it has
    # no run_changes rows and the flag column defaults to 0.
    store._conn.execute("UPDATE runs SET changes_tracked = 0 WHERE id = ?", (run["id"],))
    store._conn.execute("DELETE FROM run_changes WHERE run_id = ?", (run["id"],))

    legacy = store.get_run(run["id"])
    assert legacy["status"] == "succeeded"
    assert legacy["changes_tracked"] == 0
    assert store.run_changes(run["id"]) == []  # evidence genuinely absent
    store.close()

    # Reopening runs the migration path without losing the round.
    reopened = Store(db)
    assert reopened.get_run(run["id"])["changes_tracked"] == 0
    assert [r["id"] for r in reopened.list_successful_runs()] == [run["id"]]
    reopened.close()


def test_evidence_survives_restart_and_keeps_order(tmp_path):
    db = tmp_path / "restart.sqlite3"
    store = Store(db)
    first = store.create_run()
    _stage(store, first["id"], "oai:1", title="v1",
           datestamp="2026-01-01T00:00:01Z")
    _publish(store, first["id"], "2026-01-01T00:00:10Z")

    second = store.create_run()
    _stage(store, second["id"], "oai:1", title="v2",
           datestamp="2026-01-01T00:00:11Z")
    _stage(store, second["id"], "oai:2", title="新增",
           datestamp="2026-01-01T00:00:12Z")
    _publish(store, second["id"], "2026-01-01T00:00:20Z")
    expected = store.run_changes(second["id"])
    store.close()

    reopened = Store(db)
    again = reopened.run_changes(second["id"])
    assert again == expected
    assert [c["identifier"] for c in again] == ["oai:1", "oai:2"]
    reopened.close()
