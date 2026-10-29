"use client";

import { useEffect, useState } from "react";
import type { Preview, Segment } from "@/lib/api";
import { zoneLabel } from "@/lib/useStatus";

const COMPARE_KEY = "rainbird.timeline.compare";
const COMPARE_EVENT = "rainbird-timeline-compare";

/**
 * Whether timelines show the as-written lane next to the adjusted one. One
 * setting for every timeline on the page (an event keeps them in step), kept
 * per browser. Starts on and reads storage after mount, so the server render
 * and the first client render agree.
 */
function useCompare(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(true);
  useEffect(() => {
    const read = () => {
      try {
        setOn(localStorage.getItem(COMPARE_KEY) !== "off");
      } catch {
        // storage blocked (private window): keep the default
      }
    };
    read();
    window.addEventListener(COMPARE_EVENT, read);
    return () => window.removeEventListener(COMPARE_EVENT, read);
  }, []);
  const set = (value: boolean) => {
    setOn(value);
    try {
      localStorage.setItem(COMPARE_KEY, value ? "on" : "off");
    } catch {
      // not remembered, but still applies to this timeline
    }
    window.dispatchEvent(new Event(COMPARE_EVENT));
  };
  return [on, set];
}

/**
 * One row per zone, a bar per cycle, so the soaks between cycles are visible.
 * When an adjust is in effect and comparing is on, each zone gets two lanes on
 * the same time axis: a faint one for the plan as written and a solid one for
 * what will run.
 */
export function Timeline({ preview, names }: { preview: Preview; names: Record<number, string> }) {
  const [compareOn, setCompare] = useCompare();
  const { global_pct, plan_pct } = preview.adjust;
  const adjusted = global_pct !== 100 || plan_pct !== 100;
  const compare = adjusted && compareOn;
  const written = preview.as_written;
  // From the bars themselves: duration_min is rounded to 0.1, and a bar ending
  // at 2.25 on a 2.2 axis ran past the track's edge.
  const end = (segs: { start_min: number; minutes: number }[]) => Math.max(0, ...segs.map((s) => s.start_min + s.minutes));
  const total = Math.max(end(preview.segments), compare ? end(written.segments) : 0, 1);
  const zones = [...new Set([...(compare ? written.segments : []), ...preview.segments].map((s) => s.zone))];
  const ticks = tickMarks(total);
  const pct = Math.round((global_pct * plan_pct) / 100);

  return (
    <div className="space-y-1">
      {adjusted && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pb-1 text-xs text-ink-muted">
          {compare ? (
            <>
              <span>
                Planned {written.water_min} min → adjusted <b className="text-ink">{preview.water_min} min</b>
                {" "}({global_pct}% seasonal × {plan_pct}% plan = {pct}%) · finishes after ~{Math.round(written.duration_min)} → ~{Math.round(preview.duration_min)} min
              </span>
              <span className="flex items-center gap-1"><Swatch faint /> planned</span>
              <span className="flex items-center gap-1"><Swatch /> will run</span>
            </>
          ) : (
            <span>Adjusted to {pct}% · {preview.water_min} min</span>
          )}
          <label className="ml-auto flex cursor-pointer items-center gap-1.5 select-none">
            <input type="checkbox" checked={compareOn}
                   onChange={(e) => setCompare(e.target.checked)} />
            Compare with planned
          </label>
        </div>
      )}
      {zones.map((zone) => (
        <div key={zone} className="flex items-center gap-2 text-xs">
          <div className="w-28 shrink-0 truncate text-ink-muted">{zoneLabel(zone, names)}</div>
          <div className="flex-1 space-y-0.5">
            {compare && <Lane segments={written.segments.filter((s) => s.zone === zone)} total={total} faint />}
            <Lane segments={preview.segments.filter((s) => s.zone === zone)} total={total}
                  empty={adjusted ? "rounds to 0 min: won't water" : undefined} />
          </div>
        </div>
      ))}
      <div className="flex gap-2 text-[10px] text-ink-muted">
        <div className="w-28 shrink-0" />
        <div className="relative h-3 flex-1">
          {ticks.map((t) => (
            <span key={t} className="absolute -translate-x-1/2" style={{ left: `${(t / total) * 100}%` }}>
              {t}m
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

const SOLID = "bg-[var(--series-3)]";
const FAINT = "bg-[var(--series-3)]/30 ring-1 ring-inset ring-[var(--series-3)]/60";

function Lane({ segments, total, faint, empty }: { segments: Segment[]; total: number; faint?: boolean; empty?: string }) {
  return (
    <div className={`relative rounded bg-surface-2 ${faint ? "h-2" : "h-4"}`}>
      {segments.map((s, i) => (
        <div
          key={i}
          className={`absolute top-0 rounded ${faint ? `h-2 ${FAINT}` : `h-4 ${SOLID}`}`}
          style={{ left: `${(s.start_min / total) * 100}%`, width: `max(2px, ${(s.minutes / total) * 100}%)` }}
          title={`${faint ? "Planned: " : ""}${s.minutes} min starting at +${Math.round(s.start_min)} min`}
        />
      ))}
      {!segments.length && empty && (
        <span className="absolute inset-0 flex items-center px-2 text-[10px] font-medium text-[var(--status-critical)]">{empty}</span>
      )}
    </div>
  );
}

function Swatch({ faint }: { faint?: boolean }) {
  return <span className={`inline-block h-2 w-4 rounded ${faint ? FAINT : SOLID}`} />;
}

function tickMarks(total: number): number[] {
  const step = [5, 10, 15, 30, 60, 120].find((s) => total / s <= 8) ?? 240;
  const out = [];
  for (let t = 0; t <= total; t += step) out.push(t);
  return out;
}
