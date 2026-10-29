"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Status } from "./api";

/** Polls the service's cached status. Cheap: the service never forwards this to the module. */
export function useStatus(intervalMs = 5000) {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api<Status>("status"));
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

  return { status, error, refresh };
}

export function useZoneNames() {
  const [names, setNames] = useState<Record<number, string>>({});
  useEffect(() => {
    api<{ zone: number; name: string }[]>("zones")
      .then((zs) => setNames(Object.fromEntries(zs.map((z) => [z.zone, z.name]))))
      .catch(() => {});
  }, []);
  return names;
}

export function zoneLabel(zone: number, names: Record<number, string>): string {
  return names[zone] ? `${zone} · ${names[zone]}` : `Zone ${zone}`;
}
