"use client";

import { useEffect, useState } from "react";
import { api, dayAndTime, hm, type Event, type Run } from "@/lib/api";
import { Banner, StatusMark, StatusPill, type Level } from "@/lib/ui";

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
                  <span className="ml-2 text-xs text-ink-muted">{r.kind}</span>
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
                  <p className="tabular">
                    {r.detail.segments.map((s, i) => (
                      <span key={i} className="mr-3">
                        +{Math.round(s.at_min)}m zone {s.zone} {s.watered_min ?? s.minutes}m{s.result !== "ok" && ` (${s.result})`}
                      </span>
                    ))}
                  </p>
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
