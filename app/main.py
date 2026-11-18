"""Web application for the institutional-repository harvester.

Pages
-----
``/``                          catalogue browser, watermark, run controls
``/runs``                      round history (windows, attempts, restarts, errors)
``/samples``                   change-sampling inspector for successful rounds
``/status``                    statically rendered status JSON
``GET  /api/status``           polling endpoint used by the page
``GET  /api/catalog``          catalogue JSON (search + pagination)
``GET  /api/samples``          deterministic change-sample JSON for one round
``POST /api/harvest``          start a round
``POST /api/harvest/cancel``   cancel the active round without waiting for it
``POST /api/harvest/restart``  cancel (if any) and immediately start a new round
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .harvest import HarvestConfig, Harvester
from .sampling import CHANGE_LABELS, SamplingError, select_sample, validate_params
from .store import Store

BASE_DIR = Path(__file__).resolve().parent


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


def _oai_url() -> str:
    return os.environ.get("OAI_URL", "http://demo-oai:8000/oai")


def create_app(
    *,
    db_path: str | Path | None = None,
    oai_url: str | None = None,
    read_timeout: float | None = None,
    max_attempts: int | None = None,
    client_factory: Any = None,
) -> FastAPI:
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(db_path or (data_dir / "harvester.sqlite3"))
    config = HarvestConfig(
        oai_url=oai_url or _oai_url(),
        read_timeout=float(
            read_timeout
            if read_timeout is not None
            else os.environ.get("OAI_READ_TIMEOUT", "20")
        ),
        connect_timeout=float(os.environ.get("OAI_CONNECT_TIMEOUT", "10")),
        max_attempts=int(
            max_attempts
            if max_attempts is not None
            else os.environ.get("HARVEST_MAX_ATTEMPTS", "3")
        ),
        retry_backoff_ms=int(os.environ.get("HARVEST_RETRY_BACKOFF_MS", "300")),
    )
    harvester = Harvester(store, config, client_factory=client_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # A fresh process owns no in-flight rounds; Store already marked any
        # 'running' rows from a previous process as failed without publishing.
        yield
        await harvester.shutdown()
        store.close()

    app = FastAPI(title="机构库采集控制台", lifespan=lifespan)
    app.state.store = store
    app.state.harvester = harvester
    app.state.config = config
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def status_payload() -> dict[str, Any]:
        active = store.get_active_run()
        published = store.published_state()
        last = store.list_runs(limit=1)
        return {
            "active": active,
            "published": published,
            "last_run": last[0] if last else None,
            "config": {
                "oai_url": config.oai_url,
                "max_attempts": config.max_attempts,
                "read_timeout": config.read_timeout,
            },
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        q: str = "",
        page: int = 1,
        page_size: int = 20,
        message: str | None = None,
    ) -> HTMLResponse:
        catalogue = store.catalog_page(q, page, page_size)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "catalogue": catalogue,
                "published": store.published_state(),
                "status": status_payload(),
                "runs": store.list_runs(limit=10),
                "message": message,
            },
        )

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "runs.html",
            {
                "runs": store.list_runs(limit=50),
                "published": store.published_state(),
                "status": status_payload(),
            },
        )

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(status_payload())

    @app.get("/status")
    async def status_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "status.html", {"status": status_payload()}
        )

    @app.post("/api/harvest")
    async def start_harvest() -> JSONResponse:
        try:
            control = await harvester.start_run()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(
            {"run_id": control.run_id, "status": "running"}, status_code=202
        )

    @app.post("/api/harvest/cancel")
    async def cancel_harvest() -> JSONResponse:
        active = store.get_active_run()
        if active is None:
            raise HTTPException(status_code=409, detail="当前没有正在运行的采集轮次")
        await harvester.cancel_run(active["id"], "用户在网页取消", "user")
        return JSONResponse({"run_id": active["id"], "status": "cancelled"})

    @app.post("/api/harvest/restart")
    async def restart_harvest() -> JSONResponse:
        """Cancel the active round (if any) and immediately start a new one.

        ``cancel_run`` returns as soon as SQLite has recorded the cancellation
        and the old asyncio task has been asked to stop; it never waits for a
        slow HTTP page.  The new run gets a new ``run_id`` and its own window,
        and the commit ownership check makes the old round unable to publish.
        """
        active = store.get_active_run()
        if active is not None:
            await harvester.cancel_run(
                active["id"], "用户取消了本轮以开始新一轮", "superseded"
            )
        try:
            control = await harvester.start_run()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(
            {"run_id": control.run_id, "status": "running"}, status_code=202
        )

    @app.get("/api/catalog")
    async def api_catalog(q: str = "", page: int = 1, page_size: int = 20) -> JSONResponse:
        return JSONResponse(store.catalog_page(q, page, page_size))

    def _sample_payload(
        run_id: str | None, sample_size: int, seed: int
    ) -> dict[str, Any]:
        try:
            validate_params(sample_size, seed)
        except SamplingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if not run_id:
            raise HTTPException(status_code=404, detail="请选择一个成功采集轮次")
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="轮次不存在")
        if run["status"] != "succeeded":
            raise HTTPException(
                status_code=409, detail="只有成功采集的轮次才能抽样"
            )
        changes = store.run_changes(run_id)
        if changes is None:  # defensive: get_run already established success
            raise HTTPException(
                status_code=409, detail="只有成功采集的轮次才能抽样"
            )

        # Legacy successful rounds predate change evidence: they are
        # unsampleable, never reported as "zero changes".
        sampleable = bool(run.get("changes_tracked"))
        type_counts = {change_type: 0 for change_type in CHANGE_LABELS}
        for change in changes:
            type_counts[change["change_type"]] += 1

        payload: dict[str, Any] = {
            "run": {
                "id": run["id"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "window_from": run["window_from"],
                "window_until": run["window_until"],
                "watermark_after": run["watermark_after"],
                "changes_tracked": sampleable,
            },
            "params": {"sample_size": sample_size, "seed": seed},
            "sampleable": sampleable,
            "change_counts": type_counts,
            "total_changes": len(changes),
            "samples": [],
        }
        if sampleable:
            payload["samples"] = select_sample(
                changes, run_id=run_id, sample_size=sample_size, seed=seed
            )
        return payload

    @app.get("/api/samples")
    async def api_samples(
        run_id: str | None = None, sample_size: int = 5, seed: int = 0
    ) -> JSONResponse:
        return JSONResponse(_sample_payload(run_id, sample_size, seed))

    @app.get("/samples", response_class=HTMLResponse)
    async def samples_page(
        request: Request,
        run_id: str | None = None,
        sample_size: str = "5",
        seed: str = "0",
    ) -> HTMLResponse:
        successful = store.list_successful_runs()
        selected_id = run_id or (successful[0]["id"] if successful else None)

        # Parse the form values by hand so a typo renders an in-page message
        # instead of FastAPI's raw 422 page.
        error: str | None = None
        parsed_size = 5
        parsed_seed = 0
        try:
            parsed_size = int(str(sample_size).strip())
            parsed_seed = int(str(seed).strip())
        except (TypeError, ValueError):
            error = "样本数与种子都必须是整数（样本数 1～20）。"

        result: dict[str, Any] | None = None
        if error is None and selected_id:
            try:
                result = _sample_payload(selected_id, parsed_size, parsed_seed)
            except HTTPException as exc:
                error = str(exc.detail)

        return templates.TemplateResponse(
            request,
            "samples.html",
            {
                "runs": successful,
                "selected_id": selected_id,
                "sample_size": parsed_size if error is None else sample_size,
                "seed": parsed_seed if error is None else seed,
                "result": result,
                "error": error,
                "change_labels": CHANGE_LABELS,
                "published": store.published_state(),
            },
        )

    return app


app = create_app()
