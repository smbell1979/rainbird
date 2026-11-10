"use client";

import { useState } from "react";
import { addDays, api, dayAndTime, dayLabel, describeDays, hm, type Status } from "@/lib/api";
import { Banner, StatusMark } from "@/lib/ui";
import { useServiceNow, useStatus, useZoneNames, zoneLabel } from "@/lib/useStatus";

/** "12:34", or "1:02:03" past an hour. */
function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const mm = String(Math.floor((s % 3600) / 60));
  const ss = String(s % 60).padStart(2, "0");
  return s >= 3600 ? `${Math.floor(s / 3600)}:${mm.padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

export default function StatusPage() {
  const { status, receivedAt, error, refresh } = useStatus();
  const names = useZoneNames();
  const now = useServiceNow(status, receivedAt);

  if (error && !status) return <Banner tone="error">{error}</Banner>;
  if (!status) return <p className="text-ink-muted">Loading…</p>;

  return (
    <div className="space-y-4">
      {error && <Banner tone="error">Lost contact with the service: {error}</Banner>}
      <Alerts status={status} />
      <NowCard status={status} names={names} now={now} onChange={refresh} />
      <div className="grid gap-4 md:grid-cols-2">
        <ManualRun status={status} names={names} onChange={refresh} />
        <NextRuns status={status} onChange={refresh} />
      </div>
      <ControllerCard status={status} names={names} />
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
      {status.hold && (
        <Banner tone="info">
          {status.now.slice(0, 10) < status.hold.resume_on
            ? <>Weather hold: no watering through {dayLabel(addDays(status.hold.resume_on, -1))}. Plans start
                again {dayLabel(status.hold.resume_on)}.</>
            : <>The weather hold is over: every plan that&apos;s on waters today at its usual time.</>}
        </Banner>
      )}
      {status.sensor_tripped && <Banner tone="warn">The rain sensor is tripped; scheduled plans will skip.</Banner>}
      {status.warnings.map((w) => (
        <Banner key={w} tone="warn">{w}</Banner>
      ))}
    </>
  );
}

function NowCard({ status, names, now, onChange }: {
  status: Status; names: Record<number, string>; now: number | null; onChange: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const cur = status.current;
  const watering = status.running.length > 0;
  const secondsUntil = (iso?: string) => (iso && now !== null ? Math.max(0, (Date.parse(iso) - now) / 1000) : null);
  // The status only changes when a cycle starts or ends, so the countdown is
  // worked out here each second from the cycle's end time.
  const cycleLeft = cur?.phase === "watering" ? secondsUntil(cur.ends_at) : null;
  const soakLeft = cur?.phase === "soaking" ? secondsUntil(cur.soak_until) : null;

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
              {/* A trailing "(...)" note, like "(Rain Bird PGM A)", is left to the smaller lists; the headline is the name. */}
              <div className="text-[28px] leading-tight font-semibold tracking-tight text-ink" title={cur.name}>
                {cur.name.replace(/\s*\([^()]*\)\s*$/, "") || cur.name}
              </div>
              <div className="text-sm text-ink-2">
                {cur.phase === "watering" && cur.zone != null && (
                  <>Watering {zoneLabel(cur.zone, names)} for {cur.minutes} min, until {hm(cur.ends_at)}</>
                )}
                {cur.phase === "soaking" && (
                  <>Soaking until {hm(cur.soak_until)}{soakLeft !== null && <span className="tabular"> · {clock(soakLeft)} left</span>}</>
                )}
                {cur.phase !== "watering" && cur.phase !== "soaking" && <>{cur.phase}…</>}
              </div>
              {cur.remaining && (
                <div className="flex flex-wrap gap-1.5 pt-1 text-xs">
                  {Object.entries(cur.remaining).map(([z, m]) => {
                    const isNow = cur.phase === "watering" && cur.zone === Number(z);
                    return (
                      <ZonePill key={z} label={zoneLabel(Number(z), names)} minutes={m}
                                state={m === 0 ? "done" : isNow ? "now" : "waiting"}
                                // remaining counts this whole cycle until it ends, so take
                                // the cycle out and put back what's left of it
                                secondsLeft={isNow && cycleLeft !== null && cur.minutes ? (m - cur.minutes) * 60 + cycleLeft : undefined}
                                progress={isNow && cycleLeft !== null && cur.minutes ? 1 - cycleLeft / (cur.minutes * 60) : undefined} />
                    );
                  })}
                </div>
              )}
            </>
          ) : (
            <>
              <div className="text-[28px] leading-tight font-semibold tracking-tight text-ink">
                {watering ? "Watering (not started here)" : "Idle"}
              </div>
              {/* Nothing else on this card names the zone when the app, the panel or a controller program started it. */}
              {watering && (
                <div className="text-sm text-ink-2">
                  {status.running.map((z) => zoneLabel(z, names)).join(", ")}, from the Rain Bird app, the panel or a
                  controller program
                </div>
              )}
            </>
          )}
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

/**
 * One zone of the current run. Each state pairs colour with a glyph or dot and
 * words, as the status marks do elsewhere: done is the green check, the zone
 * watering now has the accent ring and a live dot (the Controller card's
 * "connected" pill), and the rest stay plain until their turn.
 */
function ZonePill({ label, minutes, state, secondsLeft, progress }: {
  label: string; minutes: number; state: "done" | "now" | "waiting"; secondsLeft?: number; progress?: number;
}) {
  if (state === "done") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-[var(--status-good)]/12 py-1 pr-2.5 pl-1 text-ink-2">
        <StatusMark level="good" />
        {label}: done
      </span>
    );
  }
  if (state === "now") {
    // The pill fills from the left as the cycle runs: a deeper accent tint for
    // the part done. Two stops at the same point make a hard edge.
    const p = Math.round(Math.min(1, Math.max(0, progress ?? 0)) * 100);
    const fill = progress === undefined ? undefined : {
      background: `linear-gradient(90deg, color-mix(in srgb, var(--accent) 45%, transparent) ${p}%, color-mix(in srgb, var(--accent) 10%, transparent) ${p}%)`,
    };
    return (
      <span style={fill}
            className="tabular inline-flex items-center gap-1.5 rounded-full bg-accent/15 px-2.5 py-1 font-medium text-ink ring-1 ring-accent">
        <span className="h-2 w-2 rounded-full bg-accent motion-safe:animate-pulse" aria-hidden />
        {label}:{" "}
        {secondsLeft === undefined ? `${minutes} min left` : secondsLeft > 0 ? `${clock(secondsLeft)} left` : "finishing…"}
      </span>
    );
  }
  return (
    <span className="tabular rounded-full bg-surface-2 px-2.5 py-1 text-ink-2">
      {label}: {minutes} min to go
    </span>
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

function NextRuns({ status, onChange }: { status: Status; onChange: () => void }) {
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
      <WeatherHold status={status} onChange={onChange} />
    </section>
  );
}

/**
 * Named "weather hold", not "rain delay": the controller's own rain delay is a
 * different thing (the fallback keeps it at 1 day) and is shown below.
 */
function WeatherHold({ status, onChange }: { status: Status; onChange: () => void }) {
  const [days, setDays] = useState(3);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const today = status.now.slice(0, 10);
  const hold = status.hold;
  const holding = hold !== null && today < hold.resume_on;
  const valid = Number.isInteger(days) && days >= 1 && days <= 14;

  async function send(method: "PUT" | "DELETE") {
    setBusy(true);
    setNote(null);
    try {
      await api("hold", { method, body: method === "PUT" ? { days } : undefined });
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
      onChange();
    }
  }

  return (
    <div className="space-y-2 border-t border-hairline pt-3">
      <div className="text-sm font-medium text-ink">Weather hold</div>
      {holding ? (
        <p className="text-sm text-ink-2">
          No watering through {dayLabel(addDays(hold.resume_on, -1))}. On {dayLabel(hold.resume_on)},
          every plan that&apos;s on waters at its usual time, and every-few-days plans count their days from then.
        </p>
      ) : (
        <p className="text-xs text-ink-muted">
          Skip watering for a few days (after a storm, say), then water every plan on the day after.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <input className="input w-20" type="number" min={1} max={14} value={days}
               onChange={(e) => setDays(Number(e.target.value))} aria-label="Days to hold" />
        <span className="text-sm text-ink-muted">days</span>
        <button className="btn-primary" onClick={() => send("PUT")} disabled={busy || !valid}>
          {holding ? "Change" : "Hold"}
        </button>
        {holding && <button className="btn" onClick={() => send("DELETE")} disabled={busy}>Cancel hold</button>}
      </div>
      <p className="text-xs text-ink-muted">
        {valid
          ? <>Today counts as day 1: {days === 1 ? "holds today" : `holds today through ${dayLabel(addDays(today, days - 1))}`},
              plans start again {dayLabel(addDays(today, days))}.</>
          : "1 to 14 days."}
        {holding && " Cancelling puts plans back on their usual days."}
      </p>
      {note && <p className="text-sm text-ink-2">{note}</p>}
    </div>
  );
}

function ControllerCard({ status, names }: { status: Status; names: Record<number, string> }) {
  const { link, settings } = status;
  const watering = status.running.length > 0;
  return (
    <section className="card space-y-3 text-sm">
      <h2 className="h2">Controller</h2>
      <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-ink-2">
        <span>{link.model ?? "—"}</span>·<span>firmware {link.firmware ?? "—"}</span>·
        <LinkState link={link} />
        {link.last_ok && <>·<span className="tabular">last reply {new Date(link.last_ok * 1000).toLocaleTimeString()}</span></>}
      </p>
      {/* What the controller itself reports, whoever started it: the check behind the Now card. */}
      <p className="text-ink-2">
        <span className="text-ink">Running: {watering ? status.running.map((z) => zoneLabel(z, names)).join(", ") : "nothing"}</span>
        {status.running_read_at && <span className="tabular"> (read {hm(status.running_read_at)})</span>}
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
