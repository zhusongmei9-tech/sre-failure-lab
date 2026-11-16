"""Authenticated simulation controls, actual API snapshots and a read-only console."""

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from prometheus_client import Gauge
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.deployment import DeploymentController
from app.healing import HealingController
from app.reporting import build_report, render_markdown

ASSETS = Path(__file__).with_name("static")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Enabled(StrictModel):
    enabled: StrictBool


class Probe(StrictModel):
    service: Literal["api", "redis"]
    healthy: StrictBool


class Service(StrictModel):
    service: Literal["api", "redis"]


class Release(StrictModel):
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ReleaseAction(StrictModel):
    release_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class CandidateEvidence(ReleaseAction):
    ready: StrictBool
    smoke_ok: StrictBool


class HealthEvidence(ReleaseAction):
    healthy: StrictBool


def metric_snapshot(metrics, started_at, ended_at=None):
    ended_at = ended_at or datetime.now(UTC)
    samples = [sample for family in metrics.registry.collect() for sample in family.samples]
    counts = {
        sample.labels["status_code"]: int(sample.value)
        for sample in samples
        if sample.name == "lab_http_requests_total"
    }
    buckets = sorted(
        (float(s.labels["le"]), s.value)
        for s in samples
        if s.name == "lab_http_request_duration_seconds_bucket"
    )
    total = buckets[-1][1] if buckets else 0
    quantile = None
    if total:
        rank = total * 0.95
        lower, before = 0.0, 0.0
        for upper, count in buckets:
            if count >= rank:
                if math.isfinite(upper) and count > before:
                    quantile = lower + (upper - lower) * (rank - before) / (count - before)
                break
            lower, before = upper, count
    return {
        "status_counts": counts,
        "duration_seconds": max((ended_at - started_at).total_seconds(), 0.000001),
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "p95_seconds": quantile,
        "p95_method": "prometheus_histogram_estimate",
    }


def install_control_routes(app, lab_auth):
    events = app.state.events
    started_at = datetime.now(UTC)
    healing = HealingController(on_event=events.append)
    deployment = DeploymentController(on_event=events.append)
    app.state.healing = healing
    app.state.deployment = deployment
    registry = app.state.metrics.registry
    attempts = Gauge(
        "lab_simulation_healing_attempts",
        "Simulated repair attempts in current incident",
        ["service"],
        registry=registry,
    )
    locked = Gauge(
        "lab_simulation_healing_locked_out",
        "Simulated repair budget exhausted",
        ["service"],
        registry=registry,
    )
    for service in ("api", "redis"):
        attempts.labels(service).set_function(
            lambda service=service: healing.status()["services"][service]["attempts"]
        )
        locked.labels(service).set_function(
            lambda service=service: int(healing.status()["services"][service]["locked_out"])
        )
    release_lock = Gauge(
        "lab_simulation_release_locked", "Simulated release in progress", registry=registry
    )
    release_lock.set_function(lambda: int(deployment.status()["locked"]))
    router = APIRouter(prefix="/lab", tags=["simulation & reports"], dependencies=lab_auth)

    def perform(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def report(target=0.99):
        return build_report(
            metric_snapshot(app.state.metrics, started_at),
            list(reversed(events.latest(200))),
            scope="component",
            target=target,
        )

    @router.get("/overview")
    async def overview():
        return {
            "simulation_only": True,
            "real_actions_enabled": False,
            "application": {
                "version": app.version,
                **app.state.faults.status(),
                "fallback_enabled": app.state.fallback_enabled,
            },
            "healing": healing.status(),
            "deployment": deployment.status(),
            "report": report(),
            "events": events.latest(50),
        }

    @router.get("/healing")
    async def healing_status():
        return healing.status()

    @router.post("/healing/config")
    async def healing_config(body: Enabled):
        return perform(healing.set_enabled, body.enabled)

    @router.post("/healing/probe")
    async def healing_probe(body: Probe):
        return perform(healing.observe, body.service, body.healthy)

    @router.post("/healing/reset")
    async def healing_reset(body: Service):
        return perform(healing.reset, body.service)

    @router.get("/deployment")
    async def deployment_status():
        return deployment.status()

    @router.post("/deployment/begin")
    async def deployment_begin(body: Release):
        return perform(deployment.begin, body.version)

    @router.post("/deployment/candidate-check")
    async def candidate_check(body: CandidateEvidence):
        return perform(
            deployment.check_candidate, body.ready, body.smoke_ok, release_id=body.release_id
        )

    @router.post("/deployment/observe")
    async def deployment_observe(body: HealthEvidence):
        return perform(deployment.observe, body.healthy, release_id=body.release_id)

    @router.post("/deployment/rollback")
    async def deployment_rollback(body: ReleaseAction):
        return perform(deployment.rollback, release_id=body.release_id)

    @router.post("/deployment/cancel")
    async def deployment_cancel(body: ReleaseAction):
        return perform(deployment.cancel, release_id=body.release_id)

    @router.get("/report")
    async def download_report(
        format: Literal["json", "markdown"] = "json",
        target: Annotated[float, Query(gt=0, lt=1)] = 0.99,
    ):
        result = report(target)
        if format == "markdown":
            return Response(
                render_markdown(result),
                media_type="text/markdown",
                headers={"Content-Disposition": 'attachment; filename="sre-lab-report.md"'},
            )
        return result

    app.include_router(router)

    @app.get("/console", include_in_schema=False)
    async def console():
        return FileResponse(
            ASSETS / "console.html",
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.get("/console/assets/{name}", include_in_schema=False)
    async def console_asset(name: Literal["console.js", "console.css"]):
        return FileResponse(ASSETS / name, headers={"X-Content-Type-Options": "nosniff"})
