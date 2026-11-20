import asyncio
import json
import logging
import os
import random
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from app.config import Settings
from app.control import install_control_routes
from app.events import EventStore
from app.faults import FaultSpec, FaultState, LabConfig
from app.logging_config import configure_logging
from app.metrics import Metrics
from app.workload import burn_cpu

logger = logging.getLogger("lab")


class Product(BaseModel):
    id: int
    name: str
    price_cents: int
    currency: str = "CNY"
    stock: int


# A deliberately small, read-only source of truth for the first lab.
PRODUCTS = {
    1: Product(id=1, name="机械键盘", price_cents=29900, stock=20),
    2: Product(id=2, name="无线鼠标", price_cents=9900, stock=50),
    3: Product(id=3, name="USB-C 扩展坞", price_cents=19900, stock=15),
}


def create_app(settings: Settings | None = None, redis_client=None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        configure_logging()
        app.state.redis = (
            redis_client
            if redis_client is not None
            else Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=settings.redis_timeout_seconds,
                socket_timeout=settings.redis_timeout_seconds,
                retry=Retry(NoBackoff(), 0),
            )
        )
        logger.info(
            "service_started",
            extra={
                "fields": {
                    "cache_ttl_seconds": settings.cache_ttl_seconds,
                    "redis_timeout_seconds": settings.redis_timeout_seconds,
                }
            },
        )
        try:
            yield
        finally:
            await app.state.redis.aclose()
            logger.info("service_stopped")

    app = FastAPI(title="SRE Failure Lab", version=os.getenv("APP_VERSION", "0.3.0"), lifespan=lifespan)
    metrics = Metrics()
    app.state.metrics = metrics
    cpu_lock = asyncio.Lock()
    events = EventStore()
    app.state.events = events
    faults = FaultState(on_event=events.append)
    app.state.faults = faults
    app.state.fallback_enabled = settings.fallback_enabled

    def fault_active(kind):
        active = faults.active()
        return int(active is not None and active.kind == kind)

    for kind in ("latency", "errors", "cpu"):
        metrics.fault.labels(kind).set_function(lambda kind=kind: fault_active(kind))

    if settings.lab_enabled:
        bearer = HTTPBearer(auto_error=False)

        async def authorize_lab(
            credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        ):
            supplied = credentials.credentials if credentials else ""
            if not secrets.compare_digest(supplied.encode(), settings.lab_token.encode()):
                raise HTTPException(
                    status_code=401,
                    detail="Valid lab token required",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        lab_auth = [Depends(authorize_lab)]
        install_control_routes(app, lab_auth)

        @app.get("/lab/status", tags=["lab"], dependencies=lab_auth)
        async def lab_status():
            return {**faults.status(), "fallback_enabled": app.state.fallback_enabled}

        @app.post("/lab/faults", tags=["lab"], dependencies=lab_auth)
        async def set_fault(spec: FaultSpec):
            try:
                faults.set(spec)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return faults.status()

        @app.delete("/lab/faults", tags=["lab"], dependencies=lab_auth)
        async def clear_fault():
            faults.clear()
            return faults.status()

        @app.patch("/lab/config", tags=["lab"], dependencies=lab_auth)
        async def configure_lab(config: LabConfig):
            app.state.fallback_enabled = config.fallback_enabled
            events.append("fallback_changed", config.model_dump())
            return config

        @app.get("/lab/events", tags=["lab"], dependencies=lab_auth)
        async def lab_events(limit: Annotated[int, Query(ge=1, le=200)] = 50):
            faults.active()  # Observe expiry before returning history.
            return {"events": events.latest(limit), "capacity": 200, "persistent": False}

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        supplied_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_id if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", supplied_id) else uuid.uuid4().hex
        )
        request.state.request_id = request_id
        request.state.cache_result = "not_applicable"
        request.state.error_type = None
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            request.state.error_type = type(exc).__name__
            logger.exception("unhandled_exception", extra={"fields": {"request_id": request_id}})
            response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
        response.headers["X-Request-ID"] = request_id
        response.headers["X-App-Version"] = app.version
        if request.url.path.startswith("/lab/"):
            response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        duration = time.perf_counter() - started
        if getattr(route, "path", None) == "/products/{product_id}":
            metrics.observe(response.status_code, duration, request.state.cache_result)
        fields = {
            "request_id": request_id,
            "method": request.method,
            "route": getattr(route, "path", "unmatched"),
            "status_code": response.status_code,
            "duration_ms": round(duration * 1000, 2),
            "cache_result": request.state.cache_result,
            "error_type": request.state.error_type,
        }
        logger.log(
            logging.ERROR if response.status_code >= 500 else logging.INFO,
            "http_request",
            extra={"fields": fields},
        )
        return response

    @app.get("/version", tags=["health"])
    async def version():
        return {"version": app.version, "deployment_simulation_is_separate": True}

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    @app.get("/health/live", tags=["health"])
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def ready(request: Request):
        try:
            async with asyncio.timeout(settings.redis_timeout_seconds):
                await request.app.state.redis.ping()
        except (RedisError, TimeoutError) as exc:
            request.state.error_type = type(exc).__name__
            if app.state.fallback_enabled:
                return {"status": "degraded", "redis": "unavailable", "source": "ok"}
            return JSONResponse(
                status_code=503, content={"status": "not_ready", "redis": "unavailable"}
            )
        return {"status": "ready", "redis": "ok"}

    async def track_product_request():
        with metrics.inflight.track_inprogress():
            yield

    @app.get(
        "/products/{product_id}",
        response_model=Product,
        tags=["products"],
        dependencies=[Depends(track_product_request)],
    )
    async def get_product(
        product_id: Annotated[int, Path(gt=0)], request: Request, response: Response
    ):
        fault = faults.active()
        if fault is not None:
            if fault.kind == "latency":
                await asyncio.sleep(fault.delay_ms / 1000)
            elif fault.kind == "cpu":
                # Reject excess work instead of building an unbounded CPU queue.
                if cpu_lock.locked():
                    metrics.cpu_rejected.inc()
                    raise HTTPException(status_code=429, detail="CPU experiment busy")
                async with cpu_lock:
                    # Shield the worker from request cancellation so the lock is
                    # held until its bounded work actually finishes.
                    worker = asyncio.create_task(asyncio.to_thread(burn_cpu, fault.cpu_iterations))
                    try:
                        completed, _ = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        await worker
                        raise
                    metrics.cpu_iterations.inc(completed)
                    if completed < fault.cpu_iterations:
                        metrics.cpu_capped.inc()
            elif fault.kind == "errors" and random.random() < fault.error_rate:
                request.state.error_type = "InjectedError"
                raise HTTPException(status_code=500, detail="Injected failure")
        product = PRODUCTS.get(product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        key = f"lab:product:{product_id}"
        source_read = False
        try:
            # A total deadline bounds Redis work, including connection establishment.
            async with asyncio.timeout(settings.redis_timeout_seconds):
                with metrics.redis_get_duration.time():
                    cached = await request.app.state.redis.get(key)
                if cached is not None:
                    try:
                        product = Product.model_validate_json(cached)
                    except (ValidationError, json.JSONDecodeError):
                        request.state.cache_result = "invalid"
                    else:
                        if product.id != product_id:
                            request.state.cache_result = "invalid"
                            product = PRODUCTS[product_id]
                        else:
                            request.state.cache_result = "hit"
                            response.headers["X-Cache"] = "HIT"
                            return product
                if request.state.cache_result != "invalid":
                    request.state.cache_result = "miss"
                metrics.source.inc()
                source_read = True
                payload = product.model_dump_json()
                with metrics.redis_set_duration.time():
                    await request.app.state.redis.set(key, payload, ex=settings.cache_ttl_seconds)
        except (RedisError, TimeoutError) as exc:
            request.state.cache_result = "error"
            request.state.error_type = type(exc).__name__
            if app.state.fallback_enabled:
                # The local source is always available in this toy workload.
                if not source_read:
                    metrics.source.inc()
                metrics.fallback.inc()
                response.headers["X-Cache"] = "BYPASS"
                return PRODUCTS[product_id]
            raise HTTPException(status_code=503, detail="Cache unavailable") from exc
        response.headers["X-Cache"] = "MISS"
        return product

    return app


app = create_app()
