// Reconnect backoff for the voice session and the panel event stream: fast at first, then every few seconds, forever.

// Delay before attempt `attempt` (0-based): 0.5 s, 1 s, 2 s, 4 s, then 5 s.
export const backoff = (attempt: number): number => Math.min(500 * 2 ** attempt, 5000);

export function retrier(run: () => void) {
  let timer: ReturnType<typeof setTimeout> | undefined, attempt = 0;
  return {
    schedule() {  // no-op if already scheduled
      if (timer !== undefined) return;
      timer = setTimeout(() => { timer = undefined; run(); }, backoff(attempt++));
    },
    cancel() { clearTimeout(timer); timer = undefined; attempt = 0; },  // also resets the backoff
    reset() { attempt = 0; },  // after success
  };
}

// A sleep that wake() can cut short.
export function napper() {
  let wake: (() => void) | null = null;
  return {
    nap(ms: number): Promise<void> {
      return new Promise((resolve) => {
        const done = () => { clearTimeout(timer); wake = null; resolve(); };
        const timer = setTimeout(done, ms);
        wake = done;
      });
    },
    wake() { wake?.(); },
  };
}
