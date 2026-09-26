import { useCallback, useEffect, useRef, useState } from "react";

export interface PollState<T> {
  data: T | null;
  error: string | null;
  loading: boolean; // true until the first response (success or failure)
  updatedAt: number | null; // ms epoch of the last successful response
  stale: boolean; // last success is older than 3 intervals, or the latest attempt failed
  refresh: () => void;
}

/**
 * Bounded polling: one request in flight at a time, paused while the tab is hidden, and backing
 * off (doubling, up to 60 s) after failures. The previous data is kept on failure and marked
 * stale rather than cleared.
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
  deps: unknown[],
): PollState<T> {
  // Each result is tagged with the query (deps) it answers, so a render right after the query
  // changes can never show the previous query's data under the new query's controls.
  const key = JSON.stringify(deps);
  const [tagged, setTagged] = useState<{ key: string; value: T } | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [tick, setTick] = useState(0);
  const failures = useRef(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    setTagged(null);
    setLoading(true);
    setError(null);
    setUpdatedAt(null);
    failures.current = 0;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let cancelled = false;

    const schedule = () => {
      const delay = Math.min(intervalMs * 2 ** failures.current, 60_000);
      timer = setTimeout(run, delay);
    };
    const runKey = key;
    const run = async () => {
      if (cancelled) return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        schedule();
        return;
      }
      controller = new AbortController();
      try {
        const result = await fetcherRef.current(controller.signal);
        if (cancelled) return;
        setTagged({ key: runKey, value: result });
        setError(null);
        setErrorKey(null);
        setUpdatedAt(Date.now());
        failures.current = 0;
      } catch (err) {
        if (cancelled || (err instanceof DOMException && err.name === "AbortError")) return;
        setError(err instanceof Error ? err.message : String(err));
        setErrorKey(runKey);
        failures.current += 1;
      } finally {
        if (!cancelled) {
          setLoading(false);
          setNow(Date.now());
          schedule();
        }
      }
    };
    run();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, intervalMs, tick]);

  const refresh = useCallback(() => setTick((t) => t + 1), []);
  const current = tagged !== null && tagged.key === key;
  const data = current ? tagged.value : null;
  const currentError = errorKey === key ? error : null;
  const stale =
    currentError !== null || (current && updatedAt !== null && now - updatedAt > 3 * intervalMs);
  return {
    data,
    error: currentError,
    loading: loading || (!current && currentError === null),
    updatedAt: current ? updatedAt : null,
    stale,
    refresh,
  };
}
