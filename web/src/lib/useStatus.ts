"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Status } from "./api";

/** Polls the service's cached status. Cheap: the service never forwards this to the module. */
export function useStatus(intervalMs = 5000) {
  const [status, setStatus] = useState<Status | null>(null);
  // Browser clock when that status arrived, so countdowns can run on the
  // service's clock (status.now plus time since) rather than trusting this
  // device's clock to agree with the NUC's.
  const [receivedAt, setReceivedAt] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await api<Status>("status");
      setStatus(s);
      setReceivedAt(Date.now());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch
    refresh();
    const id = setInterval(refresh, intervalMs);
    return () => clearInterval(id);
  }, [refresh, intervalMs]);

  return { status, receivedAt, error, refresh };
}

/** The service's clock now, in ms, ticking every second between status polls. */
export function useServiceNow(status: Status | null, receivedAt: number): number | null {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  if (!status || !receivedAt) return null;
  return Date.parse(status.now) + Math.max(0, tick - receivedAt);
}

/** Zone names, and flow for the zones that have one set (a missing zone means no GPM, never 0). */
export function useZones() {
  const [zones, setZones] = useState<{ names: Record<number, string>; gpm: Record<number, number> }>({ names: {}, gpm: {} });
  useEffect(() => {
    api<{ zone: number; name: string; gpm: number | null }[]>("zones")
      .then((zs) => setZones({
        names: Object.fromEntries(zs.map((z) => [z.zone, z.name])),
        gpm: Object.fromEntries(zs.filter((z) => z.gpm != null).map((z) => [z.zone, z.gpm as number])),
      }))
      .catch(() => {});
  }, []);
  return zones;
}

export function useZoneNames() {
  return useZones().names;
}

export function zoneLabel(zone: number, names: Record<number, string>): string {
  return names[zone] ? `${zone} · ${names[zone]}` : `Zone ${zone}`;
}
