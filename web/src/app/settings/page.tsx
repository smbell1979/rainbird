"use client";

import { useEffect, useState } from "react";
import { api, type Settings } from "@/lib/api";

type Zone = { zone: number; name: string; gpm: number | null };

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [zones, setZones] = useState<Zone[]>([]);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    api<Settings>("settings").then(setSettings).catch((e) => setNote((e as Error).message));
    api<Zone[]>("zones").then(setZones).catch(() => {});
  }, []);

  async function save(patch: Partial<Settings>) {
    setNote(null);
    try {
      setSettings(await api<Settings>("settings", { method: "PUT", body: patch }));
      setNote("Saved.");
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  async function saveZone(zone: number, patch: Partial<Pick<Zone, "name" | "gpm">>, done: string) {
    try {
      const saved = await api<Zone>(`zones/${zone}`, { method: "PUT", body: patch });
      setZones((zs) => zs.map((z) => (z.zone === zone ? saved : z)));
      setNote(done);
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  function saveGpm(z: Zone, text: string) {
    const value = text.trim() === "" ? null : Number(text);
    if (value === z.gpm) return;
    if (value !== null && !(value > 0 && value <= 100)) {
      setNote(`Zone ${z.zone}: GPM must be a number above 0 and at most 100 (or empty to clear).`);
      return;
    }
    saveZone(z.zone, { gpm: value }, value === null ? `Zone ${z.zone} GPM cleared.` : `Zone ${z.zone} set to ${value} GPM.`);
  }

  if (!settings) return <p className="text-ink-muted">{note ?? "Loading…"}</p>;

  return (
    <div className="space-y-4">
      <h1 className="h1">Settings</h1>
      {note && <p className="text-sm text-ink-2">{note}</p>}

      <section className="card space-y-3">
        <h2 className="h2">Seasonal adjust</h2>
        <div className="flex items-center gap-2">
          <input className="input w-24" type="number" min={0} max={200} defaultValue={settings.global_adjust_pct}
                 onBlur={(e) => Number(e.target.value) !== settings.global_adjust_pct && save({ global_adjust_pct: Number(e.target.value) })} />
          <span className="text-sm text-ink-2">% of every plan&apos;s run times</span>
        </div>
        <p className="text-xs text-ink-muted">
          Like the controller&apos;s seasonal adjust: 100% runs plans unchanged, 50% halves them. Plans can add their
          own adjust on top. Manual zone runs are never adjusted.
        </p>
      </section>

      <section className="card space-y-3 text-sm text-ink-2">
        <h2 className="h2">Safety</h2>
        <label className="flex items-start gap-2">
          <input type="checkbox" className="mt-1" checked={settings.fallback_enabled}
                 onChange={(e) => save({ fallback_enabled: e.target.checked })} />
          <span>
            <b className="font-medium text-ink">Hold the controller&apos;s own programs back as a backup.</b> Each night just after midnight a 1-day rain
            delay is set, so the programs stored on the controller don&apos;t run while this service waters. If this
            service stops, the delay runs out and those programs take over within about a day. A longer rain delay
            set in the app is never lowered, and pauses plans here too.
          </span>
        </label>
        <label className="flex items-start gap-2">
          <input type="checkbox" className="mt-1" checked={settings.skip_when_sensor_tripped}
                 onChange={(e) => save({ skip_when_sensor_tripped: e.target.checked })} />
          <span className="text-ink">Skip scheduled plans while the rain sensor is tripped.</span>
        </label>
        <label className="flex items-center gap-2">
          Start a plan up to
          <input className="input w-20" type="number" min={0} max={240} defaultValue={settings.catch_up_minutes}
                 onBlur={(e) => Number(e.target.value) !== settings.catch_up_minutes && save({ catch_up_minutes: Number(e.target.value) })} />
          min late (e.g. after a restart); later than that it&apos;s logged as missed.
        </label>
      </section>

      <section className="card space-y-3">
        <h2 className="h2">Zones</h2>
        <p className="text-xs text-ink-muted">
          GPM is a rough gallons per minute for the zone, used for the gallon totals on the Calendar. One way to find
          it: read the water meter, run just that zone for 5 minutes, read it again and divide by 5. Leave it empty
          if you don&apos;t know; that zone&apos;s minutes are still counted, and its gallons are reported as missing
          rather than as zero. Changing a GPM restates past gallons too.
        </p>
        <div className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
          {zones.map((z) => (
            <div key={z.zone} className="flex min-w-0 items-center gap-2 text-sm">
              <span className="tabular w-14 shrink-0 text-ink-muted">Zone {z.zone}</span>
              <input className="input w-0 min-w-0 flex-1" defaultValue={z.name} placeholder="e.g. Front lawn"
                     aria-label={`Zone ${z.zone} name`}
                     onBlur={(e) => e.target.value !== z.name && saveZone(z.zone, { name: e.target.value }, `Zone ${z.zone} named.`)} />
              <input className="input w-20" type="number" min={0} max={100} step={0.1} inputMode="decimal"
                     defaultValue={z.gpm ?? ""} placeholder="GPM" aria-label={`Zone ${z.zone} gallons per minute`}
                     onBlur={(e) => saveGpm(z, e.target.value)} />
              <span className="text-xs text-ink-muted">GPM</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
