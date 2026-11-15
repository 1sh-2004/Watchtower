"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, SecretStr

from . import __version__
from .config import Config, ConfigError, load_config
from .db import Database
from .metrics import SentinelCollector
from .runtime import Runtime
from .settings import SettingsStore, secret_env_name

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}
LOOPBACK_CLIENTS = {"127.0.0.1", "::1"}


class ApiKeyIn(BaseModel):
    value: Optional[SecretStr] = None      # blank on an existing service = keep the stored key
    header: str = "Authorization"
    scheme: Optional[str] = "Bearer"
    expires_on: Optional[str] = None


class ServiceIn(BaseModel):
    name: str
    url: str = ""                         # blank on an existing dashboard service = keep its URL
    method: str = "GET"
    timeout: float = 5.0
    expected_status: List[int] = [200]
    interval_seconds: int = 30
    latency_warn_ms: float = 1000.0
    failures_before_alert: int = 2
    api_key: Optional[ApiKeyIn] = None
    slo: Optional[Dict[str, float]] = None

    def raw(self) -> Dict[str, Any]:
        d = self.model_dump(exclude={"api_key"})
        if self.api_key:
            d["api_key"] = {"value": self.api_key.value.get_secret_value() if self.api_key.value else None,
                            "header": self.api_key.header, "scheme": self.api_key.scheme,
                            "expires_on": self.api_key.expires_on}
        return {k: v for k, v in d.items() if v is not None}


class AwsIn(BaseModel):
    enabled: bool = True
    regions: List[str] = []
    profile: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[SecretStr] = None
    session_token: Optional[SecretStr] = None
    use_environment: bool = False          # true = drop stored keys, use profile / env / instance role
    monthly_budget_usd: float = 0
    budget_warn_pct: float = 80
    idle_cpu_percent: float = 5
    idle_days: int = 7
    interval_minutes: int = 360
    flag_unexpected_regions: bool = True

    def raw(self) -> Dict[str, Any]:
        d = self.model_dump(exclude={"secret_access_key", "session_token"})
        d["secret_access_key"] = self.secret_access_key.get_secret_value() if self.secret_access_key else None
        d["session_token"] = self.session_token.get_secret_value() if self.session_token else None
        return d


def local_only(request: Request) -> None:
    """Settings can hold secrets, and the dashboard has no login, so only the machine itself may use them.

    Three checks: the connection comes from loopback (or SENTINEL_SETTINGS_TRUST_NETWORK=1, set in
    docker-compose where the port is published to 127.0.0.1 only); the Host header names localhost, which
    defeats DNS-rebinding; and any Origin header is localhost too, which blocks cross-site requests."""
    client = request.client.host if request.client else None
    if client not in LOOPBACK_CLIENTS and os.environ.get("SENTINEL_SETTINGS_TRUST_NETWORK") != "1":
        raise HTTPException(403, "settings can only be changed from the machine Sentinel runs on (localhost)")
    host = urlparse("//" + request.headers.get("host", "")).hostname
    if host not in LOCAL_HOSTNAMES:
        raise HTTPException(403, "open the dashboard as http://127.0.0.1:<port> or http://localhost:<port> to change settings")
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in LOCAL_HOSTNAMES:
        raise HTTPException(403, "cross-site request refused")


def create_app(cfg: Optional[Config] = None, *, start_background: bool = True,
               runtime: Optional[Runtime] = None, settings_path: Optional[str] = None) -> FastAPI:
    cfg = cfg or (runtime.cfg if runtime else load_config())
    db = runtime.db if runtime else Database(cfg.db_path)
    rt = runtime or Runtime(cfg, db)
    if rt.settings is None:
        rt.settings = SettingsStore(settings_path or str(Path(cfg.db_path).with_name("sentinel.settings.json")))
        rt.load_saved()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_background:
            await rt.start()
        try:
            yield
        finally:
            if start_background:
                await rt.stop()

    app = FastAPI(title="Sentinel", version=__version__, lifespan=lifespan)
    app.state.runtime = rt

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    # ---- pages -----------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    # ---- local system -----------------------------------------------------------
    @app.get("/api/system/current")
    async def system_current() -> Dict[str, Any]:
        sample = rt.latest_system or await asyncio.to_thread(rt.system.sample)
        open_sys = [a for a in await asyncio.to_thread(db.list_alerts, "open", 200) if a["source"] == "system"]
        return {**sample, "alerts": [{"type": a["key"].split(":")[1], "message": a["message"]} for a in open_sys]}

    @app.get("/api/system/processes")
    async def system_processes() -> Dict[str, Any]:
        return rt.latest_processes or await asyncio.to_thread(rt.system.top_processes)

    @app.get("/api/system/history")
    async def system_history(hours: int = Query(1, ge=1, le=168)) -> Dict[str, Any]:
        return await asyncio.to_thread(db.system_history, hours)

    # ---- services ---------------------------------------------------------------
    @app.get("/api/services")
    async def services() -> Dict[str, Any]:
        def build():
            rows = []
            for svc in cfg.services:
                latest = rt.latest_services.get(svc.name)
                host = urlparse(svc.url).hostname  # never expose the full URL: it may carry tokens
                rows.append({
                    "name": svc.name, "host": host, "latency_warn_ms": svc.latency_warn_ms,
                    "source": svc.source, "key_set": rt.key_state(svc),
                    "latest": latest,
                    "stats_1h": db.service_stats(svc.name, 1),
                    "stats_24h": db.service_stats(svc.name, 24),
                })
            return rows
        return {"services": await asyncio.to_thread(build)}

    @app.get("/api/services/{name}/latency")
    async def service_latency(name: str, hours: int = Query(1, ge=1, le=168)) -> Dict[str, Any]:
        if name not in {s.name for s in cfg.services}:
            raise HTTPException(404, "unknown service")
        return await asyncio.to_thread(db.service_latency_series, name, hours)

    # ---- cloud --------------------------------------------------------------------
    @app.get("/api/cloud/aws")
    async def cloud_aws() -> Dict[str, Any]:
        if not cfg.aws.enabled:
            return {"enabled": False}
        if rt.latest_aws is None:
            return {"enabled": True, "pending": True}
        return rt.latest_aws

    # ---- settings (localhost only) ------------------------------------------------------
    def service_view(svc) -> Dict[str, Any]:
        return {
            "name": svc.name, "host": urlparse(svc.url).hostname, "source": svc.source,
            "editable": svc.source == "ui", "method": svc.method, "timeout": svc.timeout,
            "expected_status": svc.expected_status, "interval_seconds": svc.interval_seconds,
            "latency_warn_ms": svc.latency_warn_ms, "failures_before_alert": svc.failures_before_alert,
            "key_configured": svc.api_key is not None, "key_set": rt.key_state(svc),
            "key_header": svc.api_key.header if svc.api_key else None,
            "key_scheme": svc.api_key.scheme if svc.api_key else None,
            "key_expires_on": svc.api_key.expires_on.isoformat() if svc.api_key and svc.api_key.expires_on else None,
            "slo": {"target": svc.slo.target, "window_days": svc.slo.window_days} if svc.slo else None,
        }

    def bad_request(exc: Exception):
        return HTTPException(400, str(exc))

    @app.get("/api/settings", dependencies=[Depends(local_only)])
    async def settings_get() -> Dict[str, Any]:
        return {"services": [service_view(s) for s in cfg.services], "aws": rt.aws_view()}

    @app.post("/api/settings/test/service", dependencies=[Depends(local_only)])
    async def settings_test_service(body: ServiceIn) -> Dict[str, Any]:
        try:
            svc, secret = rt.build_service(body.raw())
        except (ConfigError, ValueError) as exc:
            raise bad_request(exc)
        return await rt.test_service(svc, secret)

    @app.put("/api/settings/services", dependencies=[Depends(local_only)])
    async def settings_put_service(body: ServiceIn) -> Dict[str, Any]:
        try:
            svc, secret = rt.build_service(body.raw())
            await rt.upsert_service(svc, secret)
        except (ConfigError, ValueError) as exc:
            raise bad_request(exc)
        return service_view(svc)

    @app.delete("/api/settings/services/{name}", dependencies=[Depends(local_only)])
    async def settings_delete_service(name: str) -> Dict[str, Any]:
        try:
            removed = await rt.remove_service(name)
        except ConfigError as exc:
            raise bad_request(exc)
        if not removed:
            raise HTTPException(404, "unknown service")
        return {"removed": name}

    @app.post("/api/settings/test/aws", dependencies=[Depends(local_only)])
    async def settings_test_aws(body: AwsIn) -> Dict[str, Any]:
        try:
            aws_cfg = rt.build_aws(body.raw())
        except (ConfigError, ValueError) as exc:
            raise bad_request(exc)
        return await asyncio.to_thread(rt.test_aws, aws_cfg)

    @app.put("/api/settings/aws", dependencies=[Depends(local_only)])
    async def settings_put_aws(body: AwsIn) -> Dict[str, Any]:
        try:
            await rt.set_aws(rt.build_aws(body.raw()))
        except (ConfigError, ValueError) as exc:
            raise bad_request(exc)
        return rt.aws_view()

    @app.delete("/api/settings/aws", dependencies=[Depends(local_only)])
    async def settings_reset_aws() -> Dict[str, Any]:
        await rt.reset_aws()
        return rt.aws_view()

    # ---- SLOs and incidents -------------------------------------------------------
    @app.get("/api/slo")
    async def slo_reports() -> Dict[str, Any]:
        return {"slos": await asyncio.to_thread(rt.slo_reports)}

    @app.get("/api/incidents")
    async def incidents(status: Optional[str] = Query(None, pattern="^(open|resolved)$"),
                        limit: int = Query(50, ge=1, le=200)) -> Dict[str, Any]:
        return {"incidents": await asyncio.to_thread(rt.incidents.list, status, limit)}

    @app.get("/api/incidents/{incident_id}")
    async def incident(incident_id: int) -> Dict[str, Any]:
        found = await asyncio.to_thread(rt.incidents.view, incident_id)
        if found is None:
            raise HTTPException(404, "unknown incident")
        return found

    # ---- alerts & insights ------------------------------------------------------------
    @app.get("/api/alerts")
    async def alerts(status: Optional[str] = Query(None, pattern="^(open|resolved)$"),
                     limit: int = Query(100, ge=1, le=500)) -> Dict[str, Any]:
        return {"alerts": await asyncio.to_thread(db.list_alerts, status, limit)}

    @app.get("/api/insights")
    async def insights() -> Dict[str, Any]:
        return await rt.insights()

    @app.get("/api/health")
    async def health():
        db_ok = await asyncio.to_thread(db.ping)
        body = {
            "status": "ok" if db_ok and not rt.errors else ("degraded" if db_ok else "down"),
            "version": __version__,
            "uptime_seconds": int(time.time() - rt.started_at),
            "database": db_ok,
            "task_errors": rt.errors,
            "aws_enabled": cfg.aws.enabled,
            "services_configured": len(cfg.services),
        }
        return JSONResponse(body, status_code=200 if db_ok else 503)

    if cfg.metrics_enabled:
        registry = CollectorRegistry(auto_describe=False)
        registry.register(SentinelCollector(rt))

        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> Response:
            body = await asyncio.to_thread(generate_latest, registry)
            return Response(body, media_type=CONTENT_TYPE_LATEST)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
