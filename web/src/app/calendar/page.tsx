"use client";

import { useEffect, useMemo, useState } from "react";
import { api, hm, WEEKDAYS, type Calendar, type Run, type Usage } from "@/lib/api";
import { Banner, StatusMark, StatusPill, Warning, type Level } from "@/lib/ui";
import { useZoneNames, zoneLabel } from "@/lib/useStatus";

// Dates are handled as "YYYY-MM-DD" strings and stepped in UTC, so the
// browser's own time zone (or a DST change in it) can never shift a day.
const iso = (d: Date) => d.toISOString().slice(0, 10);
const parse = (day: string) => new Date(`${day}T00:00:00Z`);
function addDays(day: string, n: number): string {
  const d = parse(day);
  d.setUTCDate(d.getUTCDate() + n);
  return iso(d);
}
const weekday = (day: string) => (parse(day).getUTCDay() + 6) % 7; // Mon=0, as in the plans

/** Six whole weeks starting on the Monday on or before the 1st. */
function gridFor(month: string): string[] {
  const first = `${month}-01`;
  const start = addDays(first, -weekday(first));
  return Array.from({ length: 42 }, (_, i) => addDays(start, i));
}

function shiftMonth(month: string, n: number): string {
  const d = parse(`${month}-01`);
  d.setUTCMonth(d.getUTCMonth() + n);
  return iso(d).slice(0, 7);
}

// Full class strings so Tailwind sees them. A plan keeps its colour by id,
// cycling the shared palette's three validated series colours. Chips are a
// tint with a coloured edge and dark text, as the pool app never puts text on
// a series fill (white on its orange and green is too faint).
const PLAN_COLORS = [1, 2, 3].map((n) => ({
  chip: `border-l-2 border-[var(--series-${n})] bg-[var(--series-${n})]/15 text-ink`,
  dot: `bg-[var(--series-${n})]`,
  off: `border-[var(--series-${n})]/70 text-ink-2`,
}));
// Spelled out for Tailwind's scanner, which can't see the template above.
// border-[var(--series-1)] bg-[var(--series-1)]/15 bg-[var(--series-1)] border-[var(--series-1)]/70
// border-[var(--series-2)] bg-[var(--series-2)]/15 bg-[var(--series-2)] border-[var(--series-2)]/70
// border-[var(--series-3)] bg-[var(--series-3)]/15 bg-[var(--series-3)] border-[var(--series-3)]/70
const colorFor = (planId: number | null) => PLAN_COLORS[((planId ?? 1) - 1) % PLAN_COLORS.length];

// Past runs are coloured by how they went, not by plan: that is what matters looking back.
const RUN_STYLE: Record<string, { chip: string; dot: string; label: string; level: Level }> = {
  done: { chip: "bg-[var(--status-good)]/15 text-ink", dot: "bg-[var(--status-good)]", label: "watered", level: "good" },
  running: { chip: "bg-accent/15 text-ink", dot: "bg-accent", label: "running", level: "info" },
  queued: { chip: "bg-accent/15 text-ink", dot: "bg-accent", label: "queued", level: "info" },
  stopped: { chip: "bg-[var(--status-warning)]/20 text-ink", dot: "bg-[var(--status-warning)]", label: "stopped", level: "warning" },
  interrupted: { chip: "bg-[var(--status-warning)]/20 text-ink", dot: "bg-[var(--status-warning)]", label: "interrupted", level: "warning" },
  skipped: { chip: "bg-surface-2 text-ink-2", dot: "bg-[var(--ink-muted)]", label: "skipped", level: "neutral" },
  failed: { chip: "bg-[var(--status-critical)]/15 text-ink", dot: "bg-[var(--status-critical)]", label: "failed", level: "critical" },
};
const runStyle = (status: string) => RUN_STYLE[status] ?? RUN_STYLE.failed;

/** Mirrors the service's watered_minutes: unknown (old, cut-short) cycles count as nothing here. */
function wateredMin(run: Run): number {
  return (run.detail.segments ?? []).reduce(
    (a, s) => a + (s.watered_min ?? (s.result === "ok" ? s.minutes : 0)), 0);
}

type Totals = { minutes: number; gallons: number; zones: Map<number, { minutes: number; gallons: number | null }>; noGpm: number[] };

function totalsFor(usage: Usage | undefined, days: string[]): Totals {
  const zones = new Map<number, { minutes: number; gallons: number | null }>();
  let minutes = 0;
  let gallons = 0;
  for (const day of days) {
    const u = usage?.days[day];
    if (!u) continue;
    minutes += u.minutes;
    gallons += u.gallons;
    for (const [z, v] of Object.entries(u.zones)) {
      const t = zones.get(Number(z)) ?? { minutes: 0, gallons: null };
      t.minutes += v.minutes;
      if (v.gallons !== null) t.gallons = (t.gallons ?? 0) + v.gallons;
      zones.set(Number(z), t);
    }
  }
  const noGpm = [...zones].filter(([, v]) => v.gallons === null).map(([z]) => z).sort((a, b) => a - b);
  return { minutes, gallons, zones, noGpm };
}

function fmtMin(m: number): string {
  const total = Math.round(m);
  if (total < 60) return `${total} min`;
  return total % 60 ? `${Math.floor(total / 60)} h ${total % 60} min` : `${total / 60} h`;
}

/** "+" when some zones have no GPM: the number is a floor, not the total. */
function fmtGal(t: Pick<Totals, "gallons" | "noGpm" | "zones">): string | null {
  if (t.noGpm.length === t.zones.size) return null; // no GPM for anything that ran
  return `${Math.round(t.gallons).toLocaleString()}${t.noGpm.length ? "+" : ""} gal`;
}

// Seven day columns, plus a week-total column where there's room for one.
const COLS = "grid grid-cols-7 sm:grid-cols-[repeat(7,minmax(0,1fr))_5.5rem]";

type Expected = Calendar["expected"][number];
type Controller = Calendar["controller"][number];
type Day = { runs: Run[]; expected: Expected[]; controller: Controller[] };

export default function CalendarPage() {
  const names = useZoneNames();
  const [month, setMonth] = useState(() => iso(new Date()).slice(0, 7));
  const [selected, setSelected] = useState<string | null>(null);
  const [showOff, setShowOff] = useState(true);
  const [cal, setCal] = useState<Calendar | null>(null);
  const [error, setError] = useState<string | null>(null);
  const grid = useMemo(() => gridFor(month), [month]);

  useEffect(() => {
    let live = true;
    const load = () =>
      api<Calendar>(`calendar?start=${grid[0]}&days=${grid.length}`)
        .then((c) => { if (live) { setCal(c); setError(null); } })
        .catch((e) => live && setError((e as Error).message));
    load();
    const id = setInterval(load, 60000);
    return () => { live = false; clearInterval(id); };
  }, [grid]);

  const today = cal?.now.slice(0, 10) ?? iso(new Date());
  const byDay = useMemo(() => {
    const out: Record<string, Day> = {};
    const get = (d: string) => (out[d] ??= { runs: [], expected: [], controller: [] });
    if (!cal || cal.first !== grid[0]) return out; // a stale reply for another month
    for (const r of cal.runs) get((r.started_at ?? r.queued_at).slice(0, 10)).runs.push(r);
    for (const e of cal.expected) if (showOff || e.enabled) get(e.at.slice(0, 10)).expected.push(e);
    for (const c of cal.controller) get(c.at.slice(0, 10)).controller.push(c);
    return out;
  }, [cal, grid, showOff]);

  const hasOff = cal?.expected.some((e) => !e.enabled) ?? false;
  const usage = cal && cal.first === grid[0] ? cal.usage : undefined;
  const weeks = useMemo(() => Array.from({ length: 6 }, (_, w) => grid.slice(w * 7, w * 7 + 7)), [grid]);
  const shown = selected ?? today;
  const title = parse(`${month}-01`).toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="h1 mr-auto">{title}</h1>
        <button className="btn" onClick={() => setMonth(shiftMonth(month, -1))} aria-label="Previous month">‹</button>
        <button className="btn" onClick={() => { setMonth(today.slice(0, 7)); setSelected(null); }}>Today</button>
        <button className="btn" onClick={() => setMonth(shiftMonth(month, 1))} aria-label="Next month">›</button>
      </div>
      {error && <Banner tone="error">{error}</Banner>}

      <div className="card overflow-hidden p-0">
        <div className={`${COLS} border-b border-hairline text-center text-[11px] font-medium text-ink-muted`}>
          {WEEKDAYS.map((w) => <div key={w} className="py-1.5">{w}</div>)}
          <div className="hidden border-l border-hairline py-1.5 sm:block">Week</div>
        </div>
        <div className={COLS}>
          {weeks.map((week, row) => [
            ...week.map((day, col) => {
              const d = byDay[day];
              const u = usage?.days[day];
              const inMonth = day.startsWith(month);
              const isToday = day === today;
              return (
                <button
                  key={day}
                  onClick={() => setSelected(day)}
                  className={`flex min-h-16 flex-col border-hairline p-1 text-left sm:min-h-24
                    ${col ? "border-l" : ""} ${row ? "border-t" : ""}
                    ${inMonth ? "" : "bg-surface-2/50 text-ink-muted"}
                    ${shown === day ? "ring-2 ring-inset ring-accent" : "hover:bg-surface-2/60"}`}
                >
                  <div className={`mb-0.5 text-xs ${isToday ? "inline-flex h-5 w-5 items-center justify-center rounded-full bg-accent font-semibold text-accent-ink" : ""}`}>
                    {Number(day.slice(8))}
                  </div>
                  {d && <DayCell day={d} />}
                  {u && (
                    <div className="mt-auto hidden pt-0.5 text-[10px] leading-3 text-ink-muted sm:block">
                      {fmtMin(u.minutes)}{(() => { const g = fmtGal(totalsFor(usage, [day])); return g && <> · {g}</>; })()}
                    </div>
                  )}
                </button>
              );
            }),
            <WeekCell key={`w${row}`} totals={totalsFor(usage, week)} first={row === 0} />,
          ])}
        </div>
      </div>

      <div className="flex flex-wrap items-start gap-x-6 gap-y-2">
        <Legend />
        {hasOff && (
          <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-xs text-ink-muted select-none">
            <input type="checkbox" checked={showOff}
                   onChange={(e) => setShowOff(e.target.checked)} />
            Show plans that are off
          </label>
        )}
      </div>
      <DayDetail day={shown} today={today} data={byDay[shown]} usage={usage} names={names} />
      <MonthSummary title={title} month={month} grid={grid} weeks={weeks} usage={usage} names={names} />
    </div>
  );
}

/** Chips with times on wider screens; on a phone a cell only fits coloured dots. */
function DayCell({ day }: { day: Day }) {
  return (
    <>
      <div className="hidden space-y-0.5 sm:block">
        {day.runs.map((r) => (
          <div key={`r${r.id}`} className={`truncate rounded-sm px-1 text-[10px] leading-4 ${runStyle(r.status).chip}`}>
            {hm(r.started_at ?? r.queued_at)} {r.name}
          </div>
        ))}
        {day.expected.map((e) => (
          <ExpectedChip key={`e${e.plan_id}${e.at}`} e={e} />
        ))}
        {day.controller.map((c) => (
          <div key={`c${c.name}${c.at}`} className={`truncate px-1 text-[10px] leading-4 text-ink-muted ${c.held ? "line-through" : ""}`}>
            {hm(c.at)} {c.name}
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-1 sm:hidden">
        {day.runs.map((r) => <span key={`r${r.id}`} className={`h-2 w-2 rounded-full ${runStyle(r.status).dot}`} />)}
        {day.expected.map((e) => {
          const c = colorFor(e.plan_id);
          return <span key={`e${e.plan_id}${e.at}`}
                       className={`h-2 w-2 rounded-full ${e.enabled && !e.held ? c.dot : `border ${c.off}`}`} />;
        })}
      </div>
    </>
  );
}

function ExpectedChip({ e }: { e: Expected }) {
  const c = colorFor(e.plan_id);
  const style = e.enabled && !e.held ? c.chip : `border border-dashed ${c.off}`;
  return (
    <div className={`truncate rounded-sm px-1 text-[10px] leading-4 ${style} ${e.held ? "line-through" : ""}`}
         title={e.held ? `held: ${e.held}` : e.enabled ? undefined : "plan is off"}>
      {hm(e.at)} {e.name}
    </div>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
      <span className="flex items-center gap-1"><span className="h-3 w-5 rounded-sm border-l-2 border-[var(--series-1)] bg-[var(--series-1)]/15" /> expected (a colour per plan)</span>
      <span className="flex items-center gap-1"><span className="h-3 w-5 rounded-sm border border-dashed border-[var(--ink-muted)]" /> plan is off</span>
      <span className="flex items-center gap-1"><span className={`h-3 w-5 rounded ${RUN_STYLE.done.chip}`} /> watered</span>
      <span className="flex items-center gap-1"><span className={`h-3 w-5 rounded ${RUN_STYLE.skipped.chip}`} /> skipped</span>
      <span className="flex items-center gap-1"><span className={`h-3 w-5 rounded ${RUN_STYLE.failed.chip}`} /> failed</span>
      <span><span className="line-through">struck through</span> = held back by a rain delay</span>
    </div>
  );
}

function DayDetail({ day, today, data, usage, names }: {
  day: string; today: string; data?: Day; usage?: Usage; names: Record<number, string>;
}) {
  const label = parse(day).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", timeZone: "UTC" });
  const empty = !data || (!data.runs.length && !data.expected.length && !data.controller.length);
  const totals = totalsFor(usage, [day]);
  const gal = fmtGal(totals);
  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4">
        <h2 className="h2">{day === today ? `Today, ${label}` : label}</h2>
        {totals.minutes > 0 && (
          <span className="tabular text-sm text-ink-2">
            Watered {fmtMin(totals.minutes)}{gal && <> · {gal}</>}
          </span>
        )}
      </div>
      {empty && <p className="text-sm text-ink-muted">{day < today ? "Nothing ran." : "Nothing scheduled."}</p>}

      {data?.runs.map((r) => {
        const st = runStyle(r.status);
        const watered = wateredMin(r);
        return (
          <div key={r.id} className="space-y-1 text-sm">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <StatusPill level={st.level}>{st.label}</StatusPill>
              <span className="font-medium">{r.name}</span>
              <span className="tabular text-ink-muted">
                {hm(r.started_at ?? r.queued_at)}{r.finished_at && <> – {hm(r.finished_at)}</>}
                {watered > 0 && <> · {+watered.toFixed(1)} min of water</>}
              </span>
            </div>
            {(r.detail.reason || r.detail.error) && (
              <p className="text-xs text-ink-muted">{r.detail.reason ?? r.detail.error}</p>
            )}
            {r.detail.segments && r.detail.segments.length > 0 && (
              <ZoneList zones={totalsByZone(r, usage?.gpm ?? {})} names={names} />
            )}
          </div>
        );
      })}

      {data?.expected.map((e) => {
        const c = colorFor(e.plan_id);
        return (
          <div key={`${e.plan_id}${e.at}`} className="space-y-1 text-sm">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${e.enabled && !e.held ? c.chip : `border border-dashed ${c.off}`}`}>
                {e.held ? "held" : e.enabled ? "expected" : "off"}
              </span>
              <span className="font-medium">{e.name}</span>
              <span className="tabular text-ink-muted">
                {hm(e.at)} – ~{hm(e.ends_at)} · {e.water_min} min of water
              </span>
            </div>
            {e.held && <Warning>Won&apos;t run while the {e.held} lasts.</Warning>}
            {!e.enabled && <p className="text-xs text-ink-muted">This plan is off; turn it on in Plans for it to run.</p>}
            <ZoneList zones={e.zones} names={names} />
          </div>
        );
      })}

      {data?.controller.map((c) => (
        <p key={`${c.name}${c.at}`} className="text-xs text-ink-muted">
          Controller {c.name} is due at {hm(c.at)} (zones {c.zones.join(", ")})
          {c.held ? ": held back by the daily rain delay." : ": the fallback is off, so it will water."}
        </p>
      ))}
    </section>
  );
}

type ZoneLine = { zone: number; minutes: number; gallons?: number | null; note?: string };

function totalsByZone(run: Run, gpm: Record<string, number>): ZoneLine[] {
  const out = new Map<number, ZoneLine>();
  for (const s of run.detail.segments ?? []) {
    const z = out.get(s.zone) ?? { zone: s.zone, minutes: 0 };
    const m = s.watered_min ?? (s.result === "ok" ? s.minutes : undefined);
    if (m !== undefined) z.minutes += m;
    if (s.result !== "ok") z.note = m === undefined ? `${s.result}, time not recorded` : s.result;
    out.set(s.zone, z);
  }
  return [...out.values()].map((z) => ({ ...z, gallons: gpm[z.zone] ? z.minutes * gpm[z.zone] : null }));
}

function ZoneList({ zones, names }: { zones: ZoneLine[]; names: Record<number, string> }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-0.5 pl-1 text-xs text-ink-2">
      {zones.map((z) => (
        <li key={z.zone}>
          {zoneLabel(z.zone, names)}: {+z.minutes.toFixed(1)} min
          {z.gallons != null && <> · {Math.round(z.gallons).toLocaleString()} gal</>}
          {z.note && <span className="text-ink-2"> ({z.note})</span>}
        </li>
      ))}
    </ul>
  );
}

function WeekCell({ totals, first }: { totals: Totals; first: boolean }) {
  const gal = fmtGal(totals);
  return (
    <div className={`hidden border-l border-hairline bg-surface-2/50 p-1.5 text-[10px] leading-4 text-ink-2 sm:block
                     ${first ? "" : "border-t"}`}>
      {totals.minutes > 0 ? (
        <>
          <div className="font-medium text-ink">{fmtMin(totals.minutes)}</div>
          {gal && <div>{gal}</div>}
        </>
      ) : <div className="text-ink-muted">—</div>}
    </div>
  );
}

/** Month totals by zone, plus weekly totals for phones (no week column there). */
function MonthSummary({ title, month, grid, weeks, usage, names }: {
  title: string; month: string; grid: string[]; weeks: string[][]; usage?: Usage; names: Record<number, string>;
}) {
  const days = grid.filter((d) => d.startsWith(month));
  const t = totalsFor(usage, days);
  const gal = fmtGal(t);
  const unknown = (usage?.unknown ?? []).filter((u) => u.day.startsWith(month));
  const anyGpm = Object.keys(usage?.gpm ?? {}).length > 0;
  return (
    <section className="card space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4">
        <h2 className="h2">{title} water</h2>
        <span className="text-sm text-ink-2">
          {t.minutes > 0 ? <>{fmtMin(t.minutes)}{gal && <> · {gal}</>}</> : "None yet"}
        </span>
      </div>

      {t.zones.size > 0 && (
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-ink-muted">
            <tr>
              <th className="font-normal">Zone</th>
              <th className="text-right font-normal">Time</th>
              <th className="text-right font-normal">Gallons</th>
            </tr>
          </thead>
          <tbody>
            {[...t.zones].sort(([a], [b]) => a - b).map(([zone, v]) => (
              <tr key={zone} className="border-t border-hairline">
                <td className="py-0.5">{zoneLabel(zone, names)}</td>
                <td className="text-right">{fmtMin(v.minutes)}</td>
                <td className="text-right">
                  {v.gallons === null ? <span className="text-ink-muted">no GPM</span> : Math.round(v.gallons).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <ul className="space-y-0.5 text-sm sm:hidden">
        {weeks.map((week) => {
          const w = totalsFor(usage, week);
          const g = fmtGal(w);
          return w.minutes > 0 && (
            <li key={week[0]} className="flex justify-between">
              <span className="text-ink-muted">
                Week of {parse(week[0]).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" })}
              </span>
              <span>{fmtMin(w.minutes)}{g && <> · {g}</>}</span>
            </li>
          );
        })}
      </ul>

      <div className="space-y-1 text-xs text-ink-muted">
        {!anyGpm && <p>Set a GPM for each zone in Settings to see gallons.</p>}
        {anyGpm && t.noGpm.length > 0 && (
          <p>
            Gallons marked + leave out zone{t.noGpm.length > 1 ? "s" : ""} {t.noGpm.join(", ")}, which
            {t.noGpm.length > 1 ? " have" : " has"} no GPM set.
          </p>
        )}
        {unknown.length > 0 && (
          <p className="flex items-start gap-1.5 text-ink"><StatusMark level="warning" className="mt-px" />
            {unknown.length} cut-short cycle{unknown.length > 1 ? "s" : ""} from before cut-short time was recorded
            {unknown.length > 1 ? " aren't" : " isn't"} counted.
          </p>
        )}
        <p>
          Counts this service&apos;s runs only: water from the Rain Bird app, the panel or the controller&apos;s own
          programs isn&apos;t included.
        </p>
      </div>
    </section>
  );
}
