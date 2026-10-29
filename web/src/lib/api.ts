"use client";

export type ZoneEntry = { zone: number; total_min: number; cycle_min: number; soak_min: number };
export type Days = { weekdays: number[] } | { every: number; anchor: string };

export type Plan = {
  id: number;
  name: string;
  enabled: boolean;
  start_time: string;
  days: Days;
  adjust_pct: number;
  zones: ZoneEntry[];
  /** Upcoming runs appear on the calendar; past runs always do. */
  show_on_calendar: boolean;
  last_started_date: string | null;
  preview?: Preview;
};
export type PlanInput = Omit<Plan, "id" | "last_started_date" | "preview">;

export type Segment = { zone: number; start_min: number; minutes: number };

export type Preview = {
  segments: Segment[];
  /** The plan as written, before seasonal and plan adjust. */
  as_written: { segments: Segment[]; water_min: number; duration_min: number };
  zones: ZoneEntry[];
  warnings: string[];
  water_min: number;
  duration_min: number;
  adjust: { global_pct: number; plan_pct: number };
};

export type ControllerProgram = {
  name: string;
  frequency: string;
  weekdays: number[];
  period: number | null;
  first_due: string | null;
  starts: string[];
  zones: number[];
};

export type Status = {
  now: string;
  link: {
    connected: boolean;
    model: string | null;
    firmware: string | null;
    last_ok: number | null;
    last_error: string | null;
    last_error_at: number | null;
    consecutive_failures: number;
    auth_failed: boolean;
  };
  running: number[];
  running_read_at: string | null;
  rain_delay: number | null;
  paused_by_rain_delay: boolean;
  sensor_tripped: boolean | null;
  available_zones: number[];
  controller_programs: ControllerProgram[];
  warnings: string[];
  current: null | {
    run_id: number;
    name: string;
    kind: string;
    phase: string;
    started_at: string;
    zone?: number | null;
    minutes?: number | null;
    ends_at?: string;
    soak_until?: string;
    remaining?: Record<string, number>;
  };
  queued: { run_id: number; name: string; kind: string }[];
  next_runs: { plan_id: number; name: string; at: string }[];
  settings: Settings;
};

export type Settings = {
  global_adjust_pct: number;
  fallback_enabled: boolean;
  skip_when_sensor_tripped: boolean;
  catch_up_minutes: number;
};

export type Run = {
  id: number;
  kind: string;
  plan_id: number | null;
  name: string;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  detail: {
    reason?: string;
    error?: string;
    warnings?: string[];
    /** watered_min: how long the valve was open (missing on cycles cut short before 2026-10-29). */
    segments?: { zone: number; minutes: number; at_min: number; result: string; note: string; watered_min?: number }[];
  };
};

export type Calendar = {
  now: string;
  first: string;
  last: string;
  expected: {
    plan_id: number;
    name: string;
    enabled: boolean;
    at: string;
    ends_at: string;
    water_min: number;
    zones: { zone: number; minutes: number }[];
    held: string | null;
  }[];
  controller: { name: string; at: string; zones: number[]; held: boolean }[];
  runs: Run[];
  usage: Usage;
};

export type DayUsage = {
  minutes: number;
  /** Only zones with a GPM; a zone without one has gallons null in `zones`. */
  gallons: number;
  zones: Record<string, { minutes: number; gallons: number | null }>;
};

export type Usage = {
  days: Record<string, DayUsage>;
  no_gpm: number[];
  unknown: { run_id: number; day: string; zone: number; result: string | null }[];
  gpm: Record<string, number>;
};

export type Event ={ id: number; at: string; level: string; message: string };

export async function api<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const res = await fetch(`/api/svc/${path}`, {
    method: init?.method ?? "GET",
    body: init?.body === undefined ? undefined : JSON.stringify(init.body),
    headers: init?.body === undefined ? undefined : { "content-type": "application/json" },
    cache: "no-store",
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error(describeError(data, res.status));
  return data as T;
}

function describeError(data: unknown, status: number): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc: [...], msg}]
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`)
      .join("; ");
  }
  return `Request failed (${status})`;
}

/** Service times are ISO strings already in the service's time zone; show them as-is. */
export function hm(iso: string | null | undefined): string {
  return iso ? iso.slice(11, 16) : "—";
}

export function dayAndTime(iso: string | null | undefined, todayIso?: string): string {
  if (!iso) return "—";
  const day = iso.slice(0, 10);
  if (todayIso && day === todayIso.slice(0, 10)) return `today ${hm(iso)}`;
  const d = new Date(`${day}T12:00:00`);
  return `${d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })} ${hm(iso)}`;
}

export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function describeDays(days: Days): string {
  if ("weekdays" in days) {
    return days.weekdays.length === 7 ? "every day" : days.weekdays.map((d) => WEEKDAYS[d]).join(" ");
  }
  return `every ${days.every} days from ${days.anchor}`;
}
