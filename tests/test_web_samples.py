"""HTTP-level tests for the change-sampling page and API.

Also covers the required end-to-end scenario: a failing round leaves no
sample, and a later successful round with a correction exposes the real
before/after values through the sampler.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import demo_oai.main as demo_main
from app.main import create_app
from app.oai import OaiClient
from app.sampling import select_sample
from app.store import Store
from conftest import wait_for_outcome


@pytest.fixture
def web_app(tmp_path: Path, demo_app, repo):
    def factory() -> OaiClient:
        return OaiClient(
            "http://demo.local/oai",
            transport=httpx.ASGITransport(app=demo_app),
        )

    app = create_app(
        db_path=tmp_path / "web-samples.sqlite3",
        oai_url="http://demo.local/oai",
        client_factory=factory,
    )
    return app


def _poll(client: TestClient, predicate, attempts: int = 100) -> dict:
    import time

    for _ in range(attempts):
        payload = client.get("/api/status").json()
        if predicate(payload):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"condition not met; last={payload}")


def _harvest(client: TestClient) -> dict:
    assert client.post("/api/harvest").status_code == 202
    return _poll(
        client, lambda p: p["active"] is None and p["published"]["watermark"]
    )


def _wait_second() -> None:
    """Ensure demo mutations land in a later OAI second than the watermark."""
    import time

    time.sleep(1.05)


def test_samples_page_keeps_harvest_entry_and_renders_empty_state(web_app):
    with TestClient(web_app) as client:
        page = client.get("/samples")
        assert page.status_code == 200
        text = page.text
        # The harvest console remains reachable from the sampling page.
        assert 'href="/"' in text
        assert "开始 / 取消采集轮次" in text
        assert "还没有成功采集轮次" in text
        # The new nav entry exists on the main console too.
        assert 'href="/samples"' in client.get("/").text


def test_successful_round_is_sampleable_with_real_before_after(web_app, repo):
    with TestClient(web_app) as client:
        _harvest(client)

        _wait_second()
        repo.touch_record("oai:demo:0002", title="更正后的抽样标题")
        repo.add_record(title="抽样新增论文", creator="抽样作者")
        repo.withdraw("oai:demo:0004")
        status = _harvest(client)
        run_id = status["published"]["last_run_id"]

        payload = client.get(
            "/api/samples",
            params={"run_id": run_id, "sample_size": 20, "seed": 11},
        ).json()
        assert payload["sampleable"] is True
        assert payload["change_counts"]["corrected"] >= 1
        assert payload["change_counts"]["added"] >= 1
        assert payload["change_counts"]["withdrawn"] >= 1
        by_id = {item["identifier"]: item for item in payload["samples"]}

        corrected = by_id["oai:demo:0002"]
        assert corrected["change_type"] == "corrected"
        assert corrected["before_title"] == "高校图书馆数据治理实践"
        assert corrected["after_title"] == "更正后的抽样标题"

        withdrawn = by_id["oai:demo:0004"]
        assert withdrawn["change_type"] == "withdrawn"
        assert withdrawn["before_title"] == "基于 OAI-PMH 的元数据互操作"
        assert withdrawn["after_title"] is None

        added = next(
            item for item in payload["samples"] if item["change_type"] == "added"
        )
        assert added["before_title"] is None
        assert added["after_title"] == "抽样新增论文"
        assert added["after_creator"] == "抽样作者"

        # The HTML page shows the same evidence.
        page = client.get(
            "/samples",
            params={"run_id": run_id, "sample_size": 20, "seed": 11},
        )
        assert page.status_code == 200
        assert "更正后的抽样标题" in page.text
        assert "高校图书馆数据治理实践" in page.text
        assert "撤回" in page.text


def test_same_params_return_same_sample_repeatedly_and_after_restart(web_app, repo):
    with TestClient(web_app) as client:
        _harvest(client)
        _wait_second()
        for i in range(4):
            repo.add_record(title=f"稳定性新增 {i}")
        status = _harvest(client)
        run_id = status["published"]["last_run_id"]
        params = {"run_id": run_id, "sample_size": 3, "seed": 123}
        first = client.get("/api/samples", params=params).json()
        second = client.get("/api/samples", params=params).json()
        assert first == second

    # Reopen the same database: the evidence is insert-only and the sampler
    # is deterministic, so the sample is identical.
    db_path = web_app.state.store.db_path
    reopened_store = Store(db_path)
    try:
        changes = reopened_store.run_changes(run_id)
        direct = select_sample(changes, run_id=run_id, sample_size=3, seed=123)
        assert direct == first["samples"]
    finally:
        reopened_store.close()


def test_validation_errors(web_app, repo):
    with TestClient(web_app) as client:
        _harvest(client)
        run_id = client.get("/api/status").json()["published"]["last_run_id"]

        bad_size = client.get(
            "/api/samples", params={"run_id": run_id, "sample_size": 21, "seed": 0}
        )
        assert bad_size.status_code == 422
        assert "1～20" in bad_size.json()["detail"]

        zero = client.get(
            "/api/samples", params={"run_id": run_id, "sample_size": 0, "seed": 0}
        )
        assert zero.status_code == 422

        # The HTML page surfaces the problem instead of crashing.
        page = client.get(
            "/samples", params={"run_id": run_id, "sample_size": "abc", "seed": "x"}
        )
        assert page.status_code == 200
        assert "整数" in page.text


def test_missing_or_unknown_run_rejected(web_app, repo):
    with TestClient(web_app) as client:
        _harvest(client)
        assert client.get("/api/samples").status_code == 404
        missing = client.get(
            "/api/samples",
            params={"run_id": "deadbeef", "sample_size": 5, "seed": 0},
        )
        assert missing.status_code == 404


def test_failed_and_cancelled_rounds_are_not_selectable(web_app, repo):
    with TestClient(web_app) as client:
        _harvest(client)
        baseline_watermark = client.get("/api/status").json()["published"][
            "watermark"
        ]

        # Break the source so the next round fails without publishing.
        class Boom(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx.ConnectError("boom", request=request)

        web_app.state.harvester._client_factory = lambda: OaiClient(
            "http://demo.local/oai", transport=Boom()
        )
        assert client.post("/api/harvest").status_code == 202
        failed = _poll(client, lambda p: p["active"] is None)
        assert failed["last_run"]["status"] == "failed"
        failed_id = failed["last_run"]["id"]

        # The failed round cannot be sampled and did not move the watermark.
        rejected = client.get(
            "/api/samples",
            params={"run_id": failed_id, "sample_size": 5, "seed": 0},
        )
        assert rejected.status_code == 409
        assert (
            client.get("/api/status").json()["published"]["watermark"]
            == baseline_watermark
        )

        # Restore the source.  A later correction round succeeds and samples
        # with its real before/after values.
        web_app.state.harvester._client_factory = lambda: OaiClient(
            "http://demo.local/oai",
            transport=httpx.ASGITransport(app=demo_main.app),
        )
        _wait_second()
        repo.touch_record("oai:demo:0001", title="端到端更正标题")
        status = _harvest(client)
        run_id = status["published"]["last_run_id"]

        payload = client.get(
            "/api/samples",
            params={"run_id": run_id, "sample_size": 20, "seed": 4},
        ).json()
        assert payload["sampleable"] is True
        corrected = next(
            item
            for item in payload["samples"]
            if item["identifier"] == "oai:demo:0001"
        )
        assert corrected["change_type"] == "corrected"
        assert corrected["before_title"] == "数字人文中的文本挖掘方法"
        assert corrected["after_title"] == "端到端更正标题"

        # The selector offers successful rounds only — the failed id is absent.
        page = client.get("/samples").text
        assert failed_id not in page
        assert run_id in page


def test_legacy_successful_round_is_unsampleable_not_zero_changes(web_app):
    with TestClient(web_app) as client:
        store: Store = web_app.state.store
        # Fabricate a pre-tracking successful round and its catalogue row.
        run = store.create_run()
        store.set_window(run["id"], "2026-01-01T00:00:10Z")
        store.stage_records(
            run["id"],
            [
                {
                    "identifier": "oai:legacy:1",
                    "title": "旧轮书目",
                    "creator": None,
                    "datestamp": "2026-01-01T00:00:01Z",
                    "deleted": False,
                }
            ],
            0,
        )
        assert store.commit_run(run["id"])["committed"]
        store._conn.execute(
            "UPDATE runs SET changes_tracked = 0 WHERE id = ?", (run["id"],)
        )
        store._conn.execute("DELETE FROM run_changes WHERE run_id = ?", (run["id"],))

        payload = client.get(
            "/api/samples",
            params={"run_id": run["id"], "sample_size": 5, "seed": 0},
        ).json()
        assert payload["sampleable"] is False
        assert payload["samples"] == []
        assert payload["run"]["changes_tracked"] is False

        page = client.get(
            "/samples", params={"run_id": run["id"], "sample_size": 5, "seed": 0}
        )
        assert "不可抽样" in page.text
        assert "不代表本轮为零变更" in page.text


async def test_cancelled_round_is_not_sampleable(harvester, store, repo):
    first = await harvester.start_run()
    await wait_for_outcome(first)

    repo.set_meta("slow_seconds", "5")
    control = await harvester.start_run()
    await asyncio.sleep(0.2)
    await harvester.cancel_run(control.run_id, "网页取消", "user")
    await wait_for_outcome(control)

    assert store.run_changes(control.run_id) is None
    assert [run["id"] for run in store.list_successful_runs()] == [first.run_id]


async def test_harvester_rounds_record_evidence_through_real_demo_flow(
    harvester, store, repo
):
    first = await harvester.start_run()
    await wait_for_outcome(first)

    await asyncio.sleep(1.05)
    repo.touch_record("oai:demo:0003", title="流程内更正")
    repo.add_record(title="流程内新增", creator="新作者")
    repo.withdraw("oai:demo:0005")
    second = await harvester.start_run()
    await wait_for_outcome(second)

    changes = store.run_changes(second.run_id)
    types = {c["identifier"]: c["change_type"] for c in changes}
    assert types["oai:demo:0003"] == "corrected"
    assert types["oai:demo:0005"] == "withdrawn"
    added = next(c for c in changes if c["change_type"] == "added")
    assert added["after_title"] == "流程内新增"

    # A third, unchanged window publishes zero changes but stays sampleable.
    await asyncio.sleep(1.05)
    third = await harvester.start_run()
    await wait_for_outcome(third)
    assert store.run_changes(third.run_id) == []
    assert store.get_run(third.run_id)["changes_tracked"] == 1
