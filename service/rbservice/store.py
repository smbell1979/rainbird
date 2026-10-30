"""SQLite storage: settings, zone names, plans, run history, events.

Only this service opens the database. The web page goes through the API, so
there is never a second writer.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zones (
    zone INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    gpm REAL                           -- gallons per minute, rough; NULL = not set
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    start_time TEXT NOT NULL,          -- HH:MM in the service time zone
    days TEXT NOT NULL,                -- {"weekdays": [0..6]} (Mon=0) or {"every": n, "anchor": "YYYY-MM-DD"}
    adjust_pct INTEGER NOT NULL DEFAULT 100,
    zones TEXT NOT NULL,               -- [{"zone", "total_min", "cycle_min", "soak_min"}]
    last_started_date TEXT,            -- date the schedule last fired or was marked missed
    show_on_calendar INTEGER NOT NULL DEFAULT 1  -- upcoming runs on the calendar; past runs always show
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                -- scheduled | plan-now | zone
    plan_id INTEGER,
    name TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL,              -- queued | running | done | stopped | interrupted | failed | skipped
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "global_adjust_pct": 100,
    "fallback_enabled": True,
    "skip_when_sensor_tripped": True,
    "catch_up_minutes": 30,
}


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Columns added after the first deploy. CREATE TABLE IF NOT EXISTS
        leaves an existing table as it was, so add them here."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(plans)")}
        if "show_on_calendar" not in have:
            self.db.execute("ALTER TABLE plans ADD COLUMN show_on_calendar INTEGER NOT NULL DEFAULT 1")
        if "gpm" not in {r["name"] for r in self.db.execute("PRAGMA table_info(zones)")}:
            self.db.execute("ALTER TABLE zones ADD COLUMN gpm REAL")

    # --- settings ------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        stored = {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT * FROM settings")}
        return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        unknown = set(values) - set(DEFAULT_SETTINGS)
        if unknown:
            raise ValueError(f"unknown settings: {sorted(unknown)}")
        for key, value in values.items():
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))
        return self.settings()

    # --- weather hold --------------------------------------------------------
    # Kept in the settings table but outside DEFAULT_SETTINGS, so the Settings
    # page's PUT can never write it, and a stale tab can't put back a hold
    # that was cancelled.

    def hold(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = 'weather_hold'").fetchone()
        return json.loads(row["value"]) if row else None

    def set_hold(self, hold: dict[str, Any] | None) -> None:
        if hold is None:
            self.db.execute("DELETE FROM settings WHERE key = 'weather_hold'")
        else:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('weather_hold', ?)", (json.dumps(hold),))

    # --- zones ---------------------------------------------------------------

    def zone_names(self) -> dict[int, str]:
        return {r["zone"]: r["name"] for r in self.db.execute("SELECT * FROM zones")}

    def zone_gpm(self) -> dict[int, float]:
        return {r["zone"]: r["gpm"] for r in self.db.execute("SELECT * FROM zones WHERE gpm IS NOT NULL")}

    # Upserts, not INSERT OR REPLACE: a replace rewrites the whole row, so
    # naming a zone would wipe its GPM and the reverse.
    def set_zone_name(self, zone: int, name: str) -> None:
        self.db.execute("INSERT INTO zones (zone, name) VALUES (?, ?) "
                        "ON CONFLICT(zone) DO UPDATE SET name = excluded.name", (zone, name))

    def set_zone_gpm(self, zone: int, gpm: float | None) -> None:
        self.db.execute("INSERT INTO zones (zone, gpm) VALUES (?, ?) "
                        "ON CONFLICT(zone) DO UPDATE SET gpm = excluded.gpm", (zone, gpm))

    # --- plans ---------------------------------------------------------------

    @staticmethod
    def _plan(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "start_time": row["start_time"],
            "days": json.loads(row["days"]),
            "adjust_pct": row["adjust_pct"],
            "zones": json.loads(row["zones"]),
            "last_started_date": row["last_started_date"],
            "show_on_calendar": bool(row["show_on_calendar"]),
        }

    def plans(self) -> list[dict[str, Any]]:
        return [self._plan(r) for r in self.db.execute("SELECT * FROM plans ORDER BY start_time, id")]

    def plan(self, plan_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return self._plan(row) if row else None

    def save_plan(self, plan: dict[str, Any], plan_id: int | None = None) -> dict[str, Any]:
        values = (plan["name"], int(plan["enabled"]), plan["start_time"], json.dumps(plan["days"]),
                  plan["adjust_pct"], json.dumps(plan["zones"]), int(plan.get("show_on_calendar", True)))
        if plan_id is None:
            cur = self.db.execute(
                "INSERT INTO plans (name, enabled, start_time, days, adjust_pct, zones, show_on_calendar) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", values)
            plan_id = cur.lastrowid
        else:
            cur = self.db.execute(
                "UPDATE plans SET name=?, enabled=?, start_time=?, days=?, adjust_pct=?, zones=?, show_on_calendar=? "
                "WHERE id=?", (*values, plan_id))
            if cur.rowcount == 0:
                raise KeyError(plan_id)
        return self.plan(plan_id)  # type: ignore[return-value]

    def delete_plan(self, plan_id: int) -> None:
        self.db.execute("DELETE FROM plans WHERE id = ?", (plan_id,))

    def mark_plan_started(self, plan_id: int, day: dt.date) -> None:
        self.db.execute("UPDATE plans SET last_started_date = ? WHERE id = ?", (day.isoformat(), plan_id))

    # --- runs ----------------------------------------------------------------

    def add_run(self, kind: str, name: str, at: dt.datetime, plan_id: int | None = None,
                detail: dict[str, Any] | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (kind, plan_id, name, queued_at, status, detail) VALUES (?, ?, ?, ?, 'queued', ?)",
            (kind, plan_id, name, at.isoformat(), json.dumps(detail or {})))
        return cur.lastrowid  # type: ignore[return-value]

    def update_run(self, run_id: int, **fields: Any) -> None:
        if "detail" in fields:
            fields["detail"] = json.dumps(fields["detail"], default=str)
        for key in ("started_at", "finished_at"):
            if isinstance(fields.get(key), dt.datetime):
                fields[key] = fields[key].isoformat()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    def runs_between(self, first: dt.date, last: dt.date) -> list[dict[str, Any]]:
        """Runs queued on days first..last inclusive. Times are stored as ISO
        strings in the service's time zone, so the first ten characters are the
        local date."""
        rows = self.db.execute(
            "SELECT * FROM runs WHERE substr(queued_at, 1, 10) BETWEEN ? AND ? ORDER BY queued_at, id",
            (first.isoformat(), last.isoformat()))
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    def interrupted_runs_on_startup(self, at: dt.datetime) -> int:
        """Runs left 'running' or 'queued' by a previous process that died."""
        cur = self.db.execute(
            "UPDATE runs SET status = 'failed', finished_at = ?, "
            "detail = json_set(detail, '$.error', 'service restarted during the run') "
            "WHERE status IN ('running', 'queued')", (at.isoformat(),))
        return cur.rowcount

    # --- events --------------------------------------------------------------

    def add_event(self, at: dt.datetime, level: str, message: str, **data: Any) -> None:
        self.db.execute("INSERT INTO events (at, level, message, data) VALUES (?, ?, ?, ?)",
                        (at.isoformat(), level, message, json.dumps(data, default=str)))

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]
