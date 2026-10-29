"use client";

import { useState } from "react";
import { api, dayAndTime, describeDays, hm, type Status } from "@/lib/api";
import { Banner, StatusMark } from "@/lib/ui";
import { useStatus, useZoneNames, zoneLabel } from "@/lib/useStatus";

export default function StatusPage() {
  const { status, error, refresh } = useStatus();
  const names = useZoneNames();

  if (error && !status) return <Banner tone="error">{error}</Banner>;
  if (!status) return <p className="text-ink-muted">Loading…</p>;

  return (
    <div className="space-y-4">
      {error && <Banner tone="error">Lost contact with the service: {error}</Banner>}
      <Alerts status={status} />
      <NowCard status={status} names={names} onChange={refresh} />
      <div className="grid gap-4 md:grid-cols-2">
        <ManualRun status={status} names={names} onChange={refresh} />
        <NextRuns status={status} />
      </div>
      <ControllerCard status={status} />
    </div>
  );
}

function Alerts({ status }: { status: Status }) {
  const { link } = status;
  return (
    <>
      {link.auth_failed && (
        <Banner tone="error">
          The controller rejected the PIN. Nothing more is being sent to it. Fix the PIN file and restart
          the service.
        </Banner>
      )}
      {!link.connected && !link.auth_failed && (
        <Banner tone="error">Not connected to the controller yet. {link.last_error}</Banner>
      )}
      {link.connected && link.consecutive_failures >= 3 && (
        <Banner tone="warn">
          The module has missed {link.consecutive_failures} requests in a row. Last: {link.last_error}
        </Banner>
      )}
      {status.paused_by_rain_delay && (
        <Banner tone="warn">
          A {status.rain_delay}-day rain delay is set on the controller, so plans are paused until it ends.
        </Banner>
      )}
      {status.sensor_tripped && <Banner tone="warn">The rain sensor is tripped; scheduled plans will skip.</Banner>}
      {status.warnings.map((w) => (
        <Banner key={w} tone="warn">{w}</Banner>
      ))}
    </>
  );
}

function NowCard({ status, names, onChange }: { status: Status; names: Record<number, string>; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const cur = status.current;
  const watering = status.running.length > 0;

  async function stop() {
    setBusy(true);
    setNote(null);
    try {
      const r = await api<{ confirmed: boolean }>("stop", { method: "POST" });
      setNote(r.confirmed ? "Stopped. Nothing is running." : "Stop sent, but the controller didn't confirm it.");
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
      onChange();
    }
  }

  return (
    <section className="card">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 space-y-1">
          <div className="text-xs font-medium text-ink-2">Now</div>
          {cur ? (
            <>
              <div className="text-[28px] leading-tight font-semibold tracking-tight text-ink">{cur.name}</div>
              <div className="text-sm text-ink-2">
                {cur.phase === "watering" && cur.zone != null && (
                  <>Watering {zoneLabel(cur.zone, names)} for {cur.minutes} min, until {hm(cur.ends_at)}</>
                )}
                {cur.phase === "soaking" && <>Soaking until {hm(cur.soak_until)}</>}
                {cur.phase !== "watering" && cur.phase !== "soaking" && <>{cur.phase}…</>}
              </div>
              {cur.remaining && (
                <div className="flex flex-wrap gap-1.5 pt-1 text-xs">
                  {Object.entries(cur.remaining).map(([z, m]) => (
                    <span key={z} className="tabular rounded-full bg-surface-2 px-2.5 py-1 text-ink-2">
                      {zoneLabel(Number(z), names)}: {m} min left
                    </span>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="text-[28px] leading-tight font-semibold tracking-tight text-ink">
              {watering ? "Watering (not started here)" : "Idle"}
            </div>
          )}
          <div className="pt-1 text-xs text-ink-muted">
            Running on the controller: {watering ? status.running.map((z) => zoneLabel(z, names)).join(", ") : "nothing"}
            {status.running_read_at && <> · read {hm(status.running_read_at)}</>}
          </div>
          {status.queued.length > 0 && (
            <div className="text-xs text-ink-muted">Queued: {status.queued.map((q) => q.name).join(", ")}</div>
          )}
        </div>
        <button className="btn-danger shrink-0" onClick={stop} disabled={busy}>
          {busy ? "Stopping…" : "Stop all"}
        </button>
      </div>
      {note && <p className="pt-2 text-sm text-ink-2">{note}</p>}
    </section>
  );
}

function ManualRun({ status, names, onChange }: { status: Status; names: Record<number, string>; onChange: () => void }) {
  const zones = status.available_zones.length ? status.available_zones : Array.from({ length: 12 }, (_, i) => i + 1);
  const [zone, setZone] = useState(zones[0]);
  const [minutes, setMinutes] = useState(5);
  const [note, setNote] = useState<string | null>(null);

  async function run() {
    setNote(null);
    try {
      await api(`zones/${zone}/run`, { method: "POST", body: { minutes } });
      setNote(`Queued ${zoneLabel(zone, names)} for ${minutes} min.`);
      onChange();
    } catch (e) {
      setNote((e as Error).message);
    }
  }

  return (
    <section className="card space-y-3">
      <h2 className="h2">Run one zone</h2>
      <div className="flex flex-wrap items-center gap-2">
        <select className="input" value={zone} onChange={(e) => setZone(Number(e.target.value))}>
          {zones.map((z) => (
            <option key={z} value={z}>{zoneLabel(z, names)}</option>
          ))}
        </select>
        <input className="input w-20" type="number" min={1} max={120} value={minutes}
               onChange={(e) => setMinutes(Number(e.target.value))} />
        <span className="text-sm text-ink-muted">min</span>
        <button className="btn-primary" onClick={run}>Start</button>
      </div>
      <p className="text-xs text-ink-muted">Exactly this long: seasonal adjust doesn&apos;t apply to manual runs.</p>
      {note && <p className="text-sm text-ink-2">{note}</p>}
    </section>
  );
}

function NextRuns({ status }: { status: Status }) {
  return (
    <section className="card space-y-3">
      <h2 className="h2">Coming up</h2>
      {status.next_runs.length === 0 && <p className="text-sm text-ink-muted">No plans scheduled.</p>}
      <ul className="space-y-1 text-sm">
        {status.next_runs.map((r) => (
          <li key={r.plan_id} className="flex justify-between gap-3">
            <span>{r.name}</span>
            <span className="tabular text-ink-2">{dayAndTime(r.at, status.now)}</span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-ink-muted">Seasonal adjust: {status.settings.global_adjust_pct}%</p>
    </section>
  );
}

function ControllerCard({ status }: { status: Status }) {
  const { link, settings } = status;
  return (
    <section className="card space-y-3 text-sm">
      <h2 className="h2">Controller</h2>
      <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-ink-2">
        <span>{link.model ?? "—"}</span>·<span>firmware {link.firmware ?? "—"}</span>·
        <LinkState link={link} />
        {link.last_ok && <>·<span className="tabular">last reply {new Date(link.last_ok * 1000).toLocaleTimeString()}</span></>}
      </p>
      <p className="text-ink-2">
        <span className="text-ink">Rain delay: {status.rain_delay ?? "—"} day(s)</span>
        {settings.fallback_enabled
          ? " (the service keeps this at 1 so the controller's own programs stay held back as a backup)"
          : " (fallback off: the controller's own programs run as normal)"}
      </p>
      {status.controller_programs.length > 0 && (
        <div>
          <div className="pb-1 text-xs text-ink-muted">The controller&apos;s own programs (the backup if this service stops)</div>
          <ul className="space-y-0.5 text-ink-2">
            {status.controller_programs.map((p) => (
              <li key={p.name}>
                <span className="text-ink">{p.name}</span>: {p.frequency === "CUSTOM" ? describeDays({ weekdays: p.weekdays }) :
                  p.frequency === "CYCLIC" ? `every ${p.period} days` : `${p.frequency.toLowerCase()} days`}
                {" at "}{p.starts.join(", ")} · zones {p.zones.join(", ")}
              </li>
            ))}
          </ul>
        </div>
      )}
      <LinkErrorLine link={link} />
    </section>
  );
}

/**
 * The weather app's "Live" pill. Green only when connected with nothing
 * failing right now; a retry in progress isn't "all good".
 */
function LinkState({ link }: { link: Status["link"] }) {
  const [dot, label, ink] = !link.connected
    ? ["var(--status-critical)", "not connected", "text-[var(--status-critical)]"]
    : link.consecutive_failures > 0
      ? ["var(--status-serious)", "connected, retrying", "text-ink"]
      : ["var(--status-good)", "connected", "text-[var(--status-good)]"];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full bg-surface-2 px-2.5 py-1 text-xs font-medium ${ink}`}>
      <span className="h-2 w-2 rounded-full" style={{ background: dot }} aria-hidden />
      {label}
    </span>
  );
}

/**
 * The module drops a few percent of requests and the link retries them, so a
 * recovered blip is routine and shown quietly, with the detail one tap away.
 * Only failures still in a row stand out (3+ also raise the banner above).
 */
function LinkErrorLine({ link }: { link: Status["link"] }) {
  if (!link.last_error) return null;
  const at = link.last_error_at ? new Date(link.last_error_at * 1000).toLocaleTimeString() : "";
  if (link.consecutive_failures === 0) {
    return (
      <details className="text-xs text-ink-muted">
        <summary className="cursor-pointer select-none">Last missed reply {at}, recovered on retry</summary>
        <p className="pt-1 break-words">{link.last_error}</p>
      </details>
    );
  }
  const failing = link.consecutive_failures >= 3;
  return (
    <p className={`flex items-start gap-1.5 text-xs break-words ${failing ? "text-[var(--status-critical)]" : "text-ink"}`}>
      <StatusMark level={failing ? "critical" : "warning"} className="mt-px" />
      <span>{link.consecutive_failures} missed in a row, retrying (last {at}): {link.last_error}</span>
    </p>
  );
}
