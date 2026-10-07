// Trying again after JARVIS goes away (a restart, the network): quickly at first, then every few seconds, for as
// long as the page is open. The voice session (state/session.ts) and the panel's event stream (state/panel.ts) use it.

// The pause before try number `attempt` (0 for the first): 0.5 s, 1 s, 2 s, 4 s, then 5 s each time.
export const backoff = (attempt: number): number => Math.min(500 * 2 ** attempt, 5000);

export function retrier(run: () => void) {
  let timer: ReturnType<typeof setTimeout> | undefined, attempt = 0;
  return {
    schedule() {  // run again after the next pause, unless already waiting to
      if (timer !== undefined) return;
      timer = setTimeout(() => { timer = undefined; run(); }, backoff(attempt++));
    },
    cancel() { clearTimeout(timer); timer = undefined; attempt = 0; },  // stop waiting, and start the pauses over
    reset() { attempt = 0; },  // it worked: the next drop starts with the shortest pause
  };
}

// A pause that something can cut short: the event stream's wait before following again.
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
