"use client";

import { useCallback, useEffect, useState } from "react";
import { api, describeDays, type Plan, type ZoneEntry } from "@/lib/api";
import { Warning } from "@/lib/ui";
import { useZoneNames, zoneLabel } from "@/lib/useStatus";
import { PlanEditor } from "./PlanEditor";
import { Timeline } from "./Timeline";

/**
 * One zone as a pill: name, the minutes as written, and in brackets the
 * minutes that will run after the seasonal and plan adjust. Those come from the
 * service's preview, the same numbers the controller is sent.
 */
function ZoneChip({ plan, zone, names }: { plan: Plan; zone: ZoneEntry; names: Record<number, string> }) {
  const pv = plan.preview;
  const adjusted = pv && (pv.adjust.global_pct !== 100 || pv.adjust.plan_pct !== 100);
  const runs = pv?.zones.find((z) => z.zone === zone.zone)?.total_min ?? 0;
  return (
    <span className="tabular inline-flex flex-wrap items-baseline gap-x-1.5 rounded-full bg-surface-2 px-3 py-1 text-sm">
      <span className="text-ink-2">{zoneLabel(zone.zone, names)}</span>
      <span className="font-medium text-ink">{zone.total_min} min</span>
      {adjusted && (runs > 0
        ? <span className="text-ink-2" title="After the seasonal and plan adjust">({runs} min)</span>
        : <span className="text-[var(--status-critical)]">(0 min: won&apos;t water)</span>)}
      {zone.cycle_min > 0 && (
        <span className="text-xs text-ink-muted">≤{zone.cycle_min} min cycles, {zone.soak_min} min soak</span>
      )}
    </span>
  );
}

export default function PlansPage() {
  const [plans, setPlans] = useState<Plan[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Plan | "new" | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const names = useZoneNames();

  const load = useCallback(async () => {
    try {
      setPlans(await api<Plan[]>("plans"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch
    load();
  }, [load]);

  async function act(fn: () => Promise<unknown>, done: string) {
    setNote(null);
    try {
      await fn();
      setNote(done);
      await load();
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  if (editing) {
    return (
      <PlanEditor
        plan={editing === "new" ? null : editing}
        names={names}
        onDone={async (saved) => {
          setEditing(null);
          if (saved) setNote(`Saved "${saved.name}".`);
          await load();
        }}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="h1">Plans</h1>
        <button className="btn-primary" onClick={() => setEditing("new")}>New plan</button>
      </div>
      {error && <p className="text-sm text-[var(--status-critical)]">{error}</p>}
      {note && <p className="text-sm">{note}</p>}
      {plans?.length === 0 && (
        <p className="text-sm text-ink-muted">
          No plans yet. Until there is one, nothing waters from here. The controller&apos;s own programs stay held
          back by the rain delay while the fallback is on (see Settings).
        </p>
      )}
      {plans?.map((p) => (
        <section key={p.id} className="card space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="font-semibold">
                {p.name} {!p.enabled && <span className="ml-1 text-xs font-normal text-ink-muted">(off)</span>}
                {!p.show_on_calendar && <span className="ml-1 text-xs font-normal text-ink-muted">(not on calendar)</span>}
              </div>
              <div className="text-sm text-ink-2">
                {describeDays(p.days)} at {p.start_time}
                {p.preview && <> · {p.preview.water_min} min of water over about {Math.round(p.preview.duration_min)} min</>}
                {p.adjust_pct !== 100 && <> · plan adjust {p.adjust_pct}%</>}
              </div>
            </div>
            <div className="flex gap-2">
              <button className="btn" onClick={() => act(() => api(`plans/${p.id}/run`, { method: "POST" }), `Queued "${p.name}" to run now.`)}>
                Run now
              </button>
              <button className="btn" onClick={() => setEditing(p)}>Edit</button>
              <button className="btn" onClick={() => {
                if (confirm(`Delete plan "${p.name}"?`)) act(() => api(`plans/${p.id}`, { method: "DELETE" }), `Deleted "${p.name}".`);
              }}>Delete</button>
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {p.zones.map((z) => <ZoneChip key={z.zone} plan={p} zone={z} names={names} />)}
          </div>
          {p.preview && <Timeline preview={p.preview} names={names} />}
          {p.preview?.warnings.map((w) => <Warning key={w}>{w}</Warning>)}
        </section>
      ))}
    </div>
  );
}
