"use client";

import { useEffect, useState } from "react";
import { api, WEEKDAYS, type Plan, type PlanInput, type Preview, type ZoneEntry } from "@/lib/api";
import { Warning } from "@/lib/ui";
import { zoneLabel } from "@/lib/useStatus";
import { Timeline } from "./Timeline";

const today = () => new Date().toLocaleDateString("sv"); // YYYY-MM-DD in local time

/** planner.adjusted_minutes: whole minutes, half rounded up, in integers so 12 at 50% is exactly 6. */
function adjustedMinutes(base: number, ...pcts: number[]): number {
  const num = pcts.reduce((a, p) => a * p, base);
  const den = 100 ** pcts.length;
  return Math.floor((2 * num + den) / (2 * den));
}

function blank(): PlanInput {
  return {
    name: "",
    enabled: true,
    start_time: "06:00",
    days: { weekdays: [0, 1, 2, 3, 4, 5, 6] },
    adjust_pct: 100,
    zones: [{ zone: 1, total_min: 10, cycle_min: 5, soak_min: 20 }],
    show_on_calendar: true,
  };
}

export function PlanEditor({ plan, names, onDone }: {
  plan: Plan | null;
  names: Record<number, string>;
  onDone: (saved: Plan | null) => void;
}) {
  const [draft, setDraft] = useState<PlanInput>(() =>
    plan ? { name: plan.name, enabled: plan.enabled, start_time: plan.start_time, days: plan.days,
             adjust_pct: plan.adjust_pct, zones: plan.zones, show_on_calendar: plan.show_on_calendar } : blank());
  const [zones, setZones] = useState<number[]>(Array.from({ length: 12 }, (_, i) => i + 1));
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api<{ zone: number }[]>("zones").then((zs) => setZones(zs.map((z) => z.zone))).catch(() => {});
  }, []);

  // Live preview from the service's own planner, so what's shown is what will run.
  useEffect(() => {
    const id = setTimeout(() => {
      api<Preview>("plans/preview", { method: "POST", body: { ...draft, name: draft.name || "preview" } })
        .then((p) => { setPreview(p); setPreviewError(null); })
        .catch((e) => setPreviewError((e as Error).message));
    }, 350);
    return () => clearTimeout(id);
  }, [draft]);

  const set = (patch: Partial<PlanInput>) => setDraft((d) => ({ ...d, ...patch }));
  const setZone = (i: number, patch: Partial<ZoneEntry>) =>
    set({ zones: draft.zones.map((z, j) => (j === i ? { ...z, ...patch } : z)) });
  const unused = zones.filter((z) => !draft.zones.some((e) => e.zone === z));
  const byWeekday = "weekdays" in draft.days;
  // The seasonal % is the service's (Settings); the plan % is the draft's own,
  // so both columns follow edits before the preview comes back.
  const seasonal = preview?.adjust.global_pct;
  // What will run comes from the service's preview, not this page's arithmetic.
  // It is rounded once from total x seasonal x plan, so on rare totals it can
  // differ by a minute from rounding the seasonal column again.
  const runs = (zone: number) => preview?.zones.find((p) => p.zone === zone)?.total_min ?? 0;

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      const saved = plan
        ? await api<Plan>(`plans/${plan.id}`, { method: "PUT", body: draft })
        : await api<Plan>("plans", { method: "POST", body: draft });
      onDone(saved);
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="h1">{plan ? `Edit "${plan.name}"` : "New plan"}</h1>

      <section className="card grid gap-4 sm:grid-cols-2">
        <label className="space-y-1">
          <div className="text-sm font-medium text-ink">Name</div>
          <input className="input w-full" value={draft.name} onChange={(e) => set({ name: e.target.value })}
                 placeholder="Front lawn" />
        </label>
        <label className="space-y-1">
          <div className="text-sm font-medium text-ink">Start time</div>
          <input className="input" type="time" value={draft.start_time}
                 onChange={(e) => set({ start_time: e.target.value })} />
        </label>

        <div className="space-y-1 sm:col-span-2">
          <div className="text-sm font-medium text-ink">Days</div>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label className="flex items-center gap-1">
              <input type="radio" checked={byWeekday}
                     onChange={() => set({ days: { weekdays: [0, 1, 2, 3, 4, 5, 6] } })} /> Days of the week
            </label>
            <label className="flex items-center gap-1">
              <input type="radio" checked={!byWeekday}
                     onChange={() => set({ days: { every: 3, anchor: today() } })} /> Every N days
            </label>
          </div>
          {"weekdays" in draft.days ? (
            <div className="flex flex-wrap gap-1 pt-1">
              {WEEKDAYS.map((name, d) => {
                const days = draft.days as { weekdays: number[] };
                const on = days.weekdays.includes(d);
                return (
                  <button key={d} type="button"
                          className={on ? "btn-primary" : "btn"}
                          onClick={() => set({ days: { weekdays: on ? days.weekdays.filter((x) => x !== d) : [...days.weekdays, d].sort() } })}>
                    {name}
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-2 pt-1 text-sm">
              every
              <input className="input w-16" type="number" min={1} max={30} value={draft.days.every}
                     onChange={(e) => set({ days: { ...(draft.days as { every: number; anchor: string }), every: Number(e.target.value) } })} />
              days, starting
              <input className="input" type="date" value={draft.days.anchor}
                     onChange={(e) => set({ days: { ...(draft.days as { every: number; anchor: string }), anchor: e.target.value } })} />
            </div>
          )}
        </div>

        <label className="space-y-1">
          <div className="text-sm font-medium text-ink">Plan adjust %</div>
          <input className="input w-24" type="number" min={0} max={200} value={draft.adjust_pct}
                 onChange={(e) => set({ adjust_pct: Number(e.target.value) })} />
          <div className="text-xs text-ink-muted">On top of the seasonal adjust in Settings.</div>
        </label>
        <div className="space-y-1 self-end text-sm">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={draft.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
            Enabled (runs on schedule)
          </label>
          <label className="flex items-center gap-2" title="Past runs of this plan stay on the calendar either way">
            <input type="checkbox" checked={draft.show_on_calendar}
                   onChange={(e) => set({ show_on_calendar: e.target.checked })} />
            Show upcoming runs on calendar
          </label>
        </div>
      </section>

      <section className="card space-y-2">
        <h2 className="h2">Zones</h2>
        {/* Scrolls sideways inside the card on a narrow phone rather than pushing the page wider. */}
        <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-ink-muted">
            <tr>
              <th className="py-1">Zone</th><th>Total min</th>
              <th className="px-2 text-right whitespace-nowrap" title="Total min x the seasonal adjust in Settings">
                Seasonal {seasonal ?? "…"}%
              </th>
              <th className="px-2 text-right whitespace-nowrap" title="Then x this plan's adjust: the minutes that will run">
                Plan {draft.adjust_pct}% · runs
              </th>
              <th className="pl-2">Max per cycle</th><th>Soak min</th><th />
            </tr>
          </thead>
          <tbody>
            {draft.zones.map((z, i) => (
              <tr key={i}>
                <td className="py-1">
                  <select className="input" value={z.zone} onChange={(e) => setZone(i, { zone: Number(e.target.value) })}>
                    {[z.zone, ...unused].sort((a, b) => a - b).map((n) => (
                      <option key={n} value={n}>{zoneLabel(n, names)}</option>
                    ))}
                  </select>
                </td>
                <td><input className="input w-16 px-2 sm:w-20 sm:px-3" type="number" min={1} max={360} value={z.total_min}
                           onChange={(e) => setZone(i, { total_min: Number(e.target.value) })} /></td>
                <td className="tabular px-2 text-right text-ink">
                  {seasonal === undefined ? "…" : adjustedMinutes(z.total_min, seasonal)}
                </td>
                <td className="tabular px-2 text-right font-medium">
                  {!preview || previewError ? <span className="text-ink-muted">…</span>
                    : runs(z.zone) === 0
                      ? <span className="text-[var(--status-critical)]" title="Rounds to 0 min: won't water">0 min</span>
                      : <span className="text-ink">{runs(z.zone)} min</span>}
                </td>
                <td className="pl-2"><input className="input w-16 px-2 sm:w-20 sm:px-3" type="number" min={0} max={120} value={z.cycle_min}
                           onChange={(e) => setZone(i, { cycle_min: Number(e.target.value) })} />
                  {z.cycle_min === 0 && <span className="ml-1 text-xs text-ink-muted">no cycling</span>}</td>
                <td><input className="input w-16 px-2 sm:w-20 sm:px-3" type="number" min={0} max={240} value={z.soak_min}
                           onChange={(e) => setZone(i, { soak_min: Number(e.target.value) })} /></td>
                <td className="text-right">
                  <button className="btn" disabled={draft.zones.length === 1}
                          onClick={() => set({ zones: draft.zones.filter((_, j) => j !== i) })}>Remove</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        <button className="btn" disabled={!unused.length}
                onClick={() => set({ zones: [...draft.zones, { zone: unused[0], total_min: 10, cycle_min: 5, soak_min: 20 }] })}>
          Add zone
        </button>
      </section>

      <section className="card space-y-2">
        <h2 className="h2">Preview</h2>
        {previewError && <p className="text-sm text-[var(--status-critical)]">{previewError}</p>}
        {preview && !previewError && (
          <>
            <p className="text-sm text-ink-2">
              {preview.water_min} min of water, finishing about {Math.round(preview.duration_min)} min after the start
              (seasonal {preview.adjust.global_pct}% × plan {preview.adjust.plan_pct}%).
            </p>
            {(preview.adjust.global_pct !== 100 || preview.adjust.plan_pct !== 100) && (
              <p className="text-xs text-ink-muted">
                After adjustment: {preview.zones.map((z) => `${zoneLabel(z.zone, names)} ${z.total_min} min`).join(", ")}
              </p>
            )}
            <Timeline preview={preview} names={names} />
            {preview.warnings.map((w) => <Warning key={w}>{w}</Warning>)}
          </>
        )}
      </section>

      {saveError && <p className="text-sm text-[var(--status-critical)]">{saveError}</p>}
      <div className="flex gap-2">
        <button className="btn-primary" onClick={save} disabled={saving || !draft.name.trim()}>
          {saving ? "Saving…" : "Save"}
        </button>
        <button className="btn" onClick={() => onDone(null)}>Cancel</button>
      </div>
    </div>
  );
}
