"use client";

import { useEffect, useState } from "react";
import { api, dayAndTime, hm, type Event, type Run } from "@/lib/api";
import { Banner, StatusMark, StatusPill, type Level } from "@/lib/ui";
import { useZoneNames, zoneLabel } from "@/lib/useStatus";

type Seg = NonNullable<Run["detail"]["segments"]>[number];

// Old cut-short cycles have no watered_min; only a finished one can stand in its minutes.
const watered = (s: Seg) => s.watered_min ?? (s.result === "ok" ? s.minutes : 0);

/** " · 6 zones · 72 min of water", or nothing when no zone ran. */
function waterLine(r: Run): string {
  const segs = r.detail.segments ?? [];
  const minutes = segs.reduce((a, s) => a + watered(s), 0);
  if (!segs.length) return "";
  const zones = new Set(segs.map((s) => s.zone)).size;
  return ` · ${zones} zone${zones > 1 ? "s" : ""} · ${+minutes.toFixed(1)} min of water`;
}

/** HH:MM of a run's start plus some minutes. Clock arithmetic on the service's
 * own local time (as hm shows it), so the browser's zone never shifts it. */
function clockPlus(iso: string, minutes: number): string {
  const [h, m] = hm(iso).split(":").map(Number);
  const t = (h * 60 + m + Math.round(minutes)) % 1440;
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

// Tint plus a coloured edge and dark text, as the calendar's chips: the pool
// app never puts text on a series fill.
const BAR: Record<string, string> = {
  ok: "border-l-2 border-[var(--series-3)] bg-[var(--series-3)]/25",
  stopped: "border-l-2 border-[var(--status-warning)] bg-[var(--status-warning)]/25",
  interrupted: "border-l-2 border-[var(--status-warning)] bg-[var(--status-warning)]/25",
  failed: "border-l-2 border-[var(--status-critical)] bg-[var(--status-critical)]/20",
};

/**
 * A strip with a bar per cycle across the run's length (the gaps are soaks
 * and the time between zones), then a line per cycle with its clock time.
 */
function Segments({ run, names }: { run: Run; names: Record<number, string> }) {
  const segs = run.detail.segments ?? [];
  const start = run.started_at ?? run.queued_at;
  const total = Math.max(1, ...segs.map((s) => s.at_min + Math.max(watered(s), s.minutes)));
  return (
    <div className="space-y-2 pt-1">
      <div className="relative h-5 rounded bg-surface-2">
        {segs.map((s, i) => (
          <div key={i}
               className={`absolute top-0 flex h-5 items-center overflow-hidden rounded-sm pl-1 text-[10px] font-medium text-ink ${BAR[s.result] ?? BAR.failed}`}
               style={{ left: `${(s.at_min / total) * 100}%`, width: `max(3px, ${(Math.max(watered(s), 0.1) / total) * 100}%)` }}
               title={`${clockPlus(start, s.at_min)} ${zoneLabel(s.zone, names)}: ${+watered(s).toFixed(1)} min`}>
            {s.zone}
          </div>
        ))}
      </div>
      <ol className="tabular grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1">
        {segs.map((s, i) => (
          <li key={i} className="contents">
            <span className="text-ink-muted">{clockPlus(start, s.at_min)}</span>
            <span className="flex min-w-0 items-center gap-1.5 text-ink">
              <StatusMark level={s.result === "ok" ? "good" : s.result === "failed" ? "critical" : "warning"} />
              <span className="truncate">{zoneLabel(s.zone, names)}</span>
              {s.result !== "ok" && <span className="shrink-0 text-ink-2">{s.result}</span>}
            </span>
            <span className="text-right text-ink-2">
              {+watered(s).toFixed(1)} min{s.result !== "ok" && <span className="text-ink-muted"> of {s.minutes}</span>}
            </span>
          </li>
        ))}
      </ol>
      {segs.some((s) => s.note) && (
        <ul className="space-y-0.5 text-ink-muted">
          {segs.filter((s) => s.note).map((s, i) => <li key={i}>{zoneLabel(s.zone, names)}: {s.note}</li>)}
        </ul>
      )}
    </div>
  );
}

const RUN_LEVEL: Record<string, Level> = {
  done: "good",
  running: "info",
  queued: "info",
  skipped: "neutral",
  stopped: "warning",
  interrupted: "warning",
  failed: "critical",
};

const EVENT_LEVEL: Record<string, Level | undefined> = { error: "critical", warn: "warning" };

export default function HistoryPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState<string | null>(null);
  const names = useZoneNames();

  useEffect(() => {
    const load = () =>
      Promise.all([api<Run[]>("runs?limit=50"), api<Event[]>("events?limit=200")])
        .then(([r, e]) => { setRuns(r); setEvents(e); setError(null); })
        .catch((e) => setError((e as Error).message));
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="space-y-8">
      {error && <Banner tone="error">{error}</Banner>}
      <section className="space-y-3">
        <h1 className="h1">Runs</h1>
        {runs.length === 0 && <p className="text-sm text-ink-muted">Nothing has run yet.</p>}
        <div className="overflow-hidden rounded-2xl border border-hairline bg-surface">
          {runs.map((r, i) => (
            <details key={r.id} className={`group px-4 py-3 sm:px-5 ${i ? "border-t border-hairline" : ""}`}>
              <summary className="flex cursor-pointer flex-wrap items-center justify-between gap-2 text-sm">
                <span>
                  <span className="font-medium text-ink">{r.name}</span>
                  <span className="ml-2 text-xs text-ink-muted">{r.kind}{waterLine(r)}</span>
                </span>
                <span className="flex items-center gap-3 text-ink-2">
                  <span className="tabular">
                    {dayAndTime(r.started_at ?? r.queued_at)}
                    {r.finished_at && <> – {hm(r.finished_at)}</>}
                  </span>
                  <StatusPill level={RUN_LEVEL[r.status] ?? "critical"}>{r.status}</StatusPill>
                </span>
              </summary>
              <div className="space-y-1 pt-2 text-xs text-ink-2">
                {(r.detail.reason || r.detail.error) && <p>{r.detail.reason ?? r.detail.error}</p>}
                {r.detail.warnings?.map((w) => (
                  <p key={w} className="flex items-start gap-1.5"><StatusMark level="warning" className="mt-px" />{w}</p>
                ))}
                {r.detail.segments && r.detail.segments.length > 0 && (
                  <Segments run={r} names={names} />
                )}
              </div>
            </details>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold tracking-tight text-ink">Events</h2>
        <ul className="card space-y-1.5 text-sm">
          {events.map((e) => {
            const level = EVENT_LEVEL[e.level];
            return (
              <li key={e.id} className="flex gap-3">
                <span className="tabular w-28 shrink-0 text-xs leading-5 text-ink-muted">{dayAndTime(e.at)}</span>
                <span className={`flex min-w-0 items-start gap-1.5 ${level === "critical" ? "text-[var(--status-critical)]" : level ? "text-ink" : "text-ink-2"}`}>
                  {level && <StatusMark level={level} className="mt-0.5" />}
                  <span className="min-w-0 break-words">{e.message}</span>
                </span>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
