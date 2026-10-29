"""HTTP API for the web page. Binds to localhost: the page is the only client.

Every read here is served from the service's cached state or the database.
Nothing on this API makes the page's refresh rate reach the module.
"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .scheduler import Service


class ZoneIn(BaseModel):
    zone: int = Field(ge=1, le=12)
    total_min: int = Field(ge=1, le=360)
    cycle_min: int = Field(0, ge=0, le=120)
    soak_min: int = Field(0, ge=0, le=240)


class DaysIn(BaseModel):
    weekdays: list[int] | None = None  # Mon=0
    every: int | None = Field(None, ge=1, le=30)
    anchor: dt.date | None = None

    @model_validator(mode="after")
    def one_form(self) -> "DaysIn":
        if (self.weekdays is None) == (self.every is None):
            raise ValueError("give either weekdays or every+anchor")
        if self.weekdays is not None:
            if not self.weekdays or any(d not in range(7) for d in self.weekdays):
                raise ValueError("weekdays must be a non-empty list of 0 (Mon) to 6 (Sun)")
            self.weekdays = sorted(set(self.weekdays))
        elif self.anchor is None:
            raise ValueError("every needs an anchor date")
        return self

    def stored(self) -> dict[str, Any]:
        if self.weekdays is not None:
            return {"weekdays": self.weekdays}
        return {"every": self.every, "anchor": self.anchor.isoformat()}  # type: ignore[union-attr]


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    enabled: bool = True
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    days: DaysIn
    adjust_pct: int = Field(100, ge=0, le=200)
    zones: list[ZoneIn] = Field(min_length=1)
    show_on_calendar: bool = True

    @field_validator("zones")
    @classmethod
    def unique_zones(cls, zones: list[ZoneIn]) -> list[ZoneIn]:
        numbers = [z.zone for z in zones]
        if len(numbers) != len(set(numbers)):
            raise ValueError("each zone can appear only once in a plan")
        return zones

    def stored(self) -> dict[str, Any]:
        return {**self.model_dump(exclude={"days"}), "days": self.days.stored()}


class SettingsIn(BaseModel):
    global_adjust_pct: int | None = Field(None, ge=0, le=200)
    fallback_enabled: bool | None = None
    skip_when_sensor_tripped: bool | None = None
    catch_up_minutes: int | None = Field(None, ge=0, le=240)


class ZoneRunIn(BaseModel):
    minutes: int = Field(ge=1, le=120)


class ZoneUpdateIn(BaseModel):
    """Either or both. gpm null clears it."""
    name: str | None = Field(None, max_length=40)
    gpm: float | None = Field(None, gt=0, le=100)


def create_app(service: Service) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        yield
        await service.shutdown()

    app = FastAPI(title="Rain Bird cycle-and-soak", lifespan=lifespan)
    store = service.store

    def check_zones(plan: PlanIn) -> None:
        available = service.state.available_zones
        missing = [z.zone for z in plan.zones if available and z.zone not in available]
        if missing:
            raise HTTPException(422, f"zone(s) {missing} are not on this controller")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return service.status()

    @app.get("/api/plans")
    def plans() -> list[dict[str, Any]]:
        return [{**p, "preview": service.preview_plan(p)} for p in store.plans()]

    @app.get("/api/plans/{plan_id}")
    def plan(plan_id: int) -> dict[str, Any]:
        p = store.plan(plan_id)
        if p is None:
            raise HTTPException(404, "no such plan")
        return {**p, "preview": service.preview_plan(p)}

    @app.post("/api/plans/preview")
    def preview(plan: PlanIn) -> dict[str, Any]:
        return service.preview_plan(plan.stored())

    @app.post("/api/plans")
    def create_plan(plan: PlanIn) -> dict[str, Any]:
        check_zones(plan)
        saved = store.save_plan(plan.stored())
        service.plan_saved(saved)
        service.event("info", f"Plan '{saved['name']}' created")
        return saved

    @app.put("/api/plans/{plan_id}")
    def update_plan(plan_id: int, plan: PlanIn) -> dict[str, Any]:
        check_zones(plan)
        try:
            saved = store.save_plan(plan.stored(), plan_id)
        except KeyError:
            raise HTTPException(404, "no such plan")
        service.plan_saved(saved)
        service.event("info", f"Plan '{saved['name']}' saved")
        return saved

    @app.delete("/api/plans/{plan_id}")
    def delete_plan(plan_id: int) -> dict[str, Any]:
        p = store.plan(plan_id)
        if p is None:
            raise HTTPException(404, "no such plan")
        store.delete_plan(plan_id)
        service.event("info", f"Plan '{p['name']}' deleted")
        return {"deleted": plan_id}

    @app.post("/api/plans/{plan_id}/run")
    def run_plan(plan_id: int) -> dict[str, Any]:
        p = store.plan(plan_id)
        if p is None:
            raise HTTPException(404, "no such plan")
        return {"run_id": service.enqueue_plan(p, "plan-now")}

    @app.post("/api/zones/{zone}/run")
    def run_zone(zone: int, body: ZoneRunIn) -> dict[str, Any]:
        if service.state.available_zones and zone not in service.state.available_zones:
            raise HTTPException(422, f"zone {zone} is not on this controller")
        return {"run_id": service.enqueue_zone(zone, body.minutes)}

    @app.post("/api/stop")
    async def stop() -> dict[str, Any]:
        return {"confirmed": await service.stop_all()}

    @app.get("/api/zones")
    def zones() -> list[dict[str, Any]]:
        names, gpm = store.zone_names(), store.zone_gpm()
        available = service.state.available_zones or list(range(1, 13))
        return [{"zone": z, "name": names.get(z, ""), "gpm": gpm.get(z)} for z in available]

    @app.put("/api/zones/{zone}")
    def update_zone(zone: int, body: ZoneUpdateIn) -> dict[str, Any]:
        if not 1 <= zone <= 12:
            raise HTTPException(422, "zone must be 1-12")
        if "name" in body.model_fields_set:
            store.set_zone_name(zone, (body.name or "").strip())
        if "gpm" in body.model_fields_set:
            store.set_zone_gpm(zone, body.gpm)
            service.event("info", f"Zone {zone} flow set to {body.gpm} GPM" if body.gpm else f"Zone {zone} flow cleared")
        return {"zone": zone, "name": store.zone_names().get(zone, ""), "gpm": store.zone_gpm().get(zone)}

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        return store.settings()

    @app.put("/api/settings")
    def update_settings(body: SettingsIn) -> dict[str, Any]:
        changes = body.model_dump(exclude_none=True)
        result = store.update_settings(changes)
        if changes:
            service.event("info", "Settings changed", **changes)
            service._refresh_warnings()
        return result

    @app.get("/api/runs")
    def runs(limit: int = 50) -> list[dict[str, Any]]:
        return store.runs(min(limit, 500))

    @app.get("/api/calendar")
    def calendar(start: dt.date, days: int = 42) -> dict[str, Any]:
        # 42 = a six-week month grid; the cap keeps a bad query from listing years.
        if not 1 <= days <= 93:
            raise HTTPException(422, "days must be 1-93")
        return service.calendar(start, days)

    @app.get("/api/events")
    def events(limit: int = 100) -> list[dict[str, Any]]:
        return store.events(min(limit, 1000))

    return app
