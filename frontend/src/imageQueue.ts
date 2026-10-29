/**
 * Pictures that load a few at a time. A page of plate reads asked for ~100 pictures at once through the hub, which
 * takes 32 requests per server at a time: the rest were refused ("too many requests to this site at once"), and a
 * button pressed meanwhile was too (TSS-DT 2026-10-28). Here at most LIMIT load at once; one that fails is tried again
 * after a pause, a few times: a refused request, or a vehicle picture the server is still cutting from the recording
 * (it answers 404 at once and queues it).
 */
import { useEffect, useState } from "react";

export const LIMIT = 4;
export const TRIES = 4;
export const RETRY_MS = 6000;

/** At most `limit` holders at once; the rest wait in order. */
export class Slots {
  private active = 0;
  private waiting: (() => void)[] = [];
  constructor(private limit: number) {}
  /** Resolves with a release function when a slot is free. */
  take(): Promise<() => void> {
    return new Promise((resolve) => {
      const grant = () => {
        this.active++;
        let done = false;
        resolve(() => {
          if (done) return;
          done = true;
          this.active--;
          this.waiting.shift()?.();
        });
      };
      if (this.active < this.limit) grant(); else this.waiting.push(grant);
    });
  }
  get busy() { return this.active; }
  get queued() { return this.waiting.length; }
}

const slots = new Slots(LIMIT);

/** The URL again with a try number, so the browser asks once more instead of reusing the failed answer. */
export function retryUrl(url: string, attempt: number): string {
  return attempt ? `${url}${url.includes("?") ? "&" : "?"}try=${attempt}` : url;
}

/**
 * An <img>'s src through the shared slots: null while waiting for a slot; `failed` once every try failed. Pass
 * onLoad / onError to the <img>.
 */
export function useQueuedImage(url: string | null, tries = TRIES, retryMs = RETRY_MS) {
  const [src, setSrc] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const [release, setRelease] = useState<(() => void) | null>(null);
  useEffect(() => { setAttempt(0); setFailed(false); }, [url]);
  useEffect(() => {
    if (!url || failed) { setSrc(null); return; }
    let alive = true;
    let free: (() => void) | null = null;
    const t = setTimeout(() => {
      slots.take().then((rel) => {
        if (!alive) { rel(); return; }
        free = rel;
        setRelease(() => rel);
        setSrc(retryUrl(url, attempt));
      });
    }, attempt ? retryMs : 0);
    return () => { alive = false; clearTimeout(t); free?.(); };
  }, [url, attempt, failed, retryMs]);
  const onLoad = () => { release?.(); };
  const onError = () => {
    release?.();
    if (attempt + 1 < tries) setAttempt(attempt + 1); else setFailed(true);
  };
  return { src, failed, onLoad, onError };
}
