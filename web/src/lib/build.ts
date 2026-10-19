// Reload into a newer build when the server serves one. This build's id is <meta name="alfred-build">
// (vite.config.ts); the served id arrives in /api/events' hello and /api/token's reply. Dev server: no meta, no reloads.

const RELOADED_KEY = "alfred.reloadedFor";  // sessionStorage: build this tab last reloaded for
const RESUME_KEY = "alfred.resume";  // sessionStorage: session to resume after reload, "muted" | "open"

export interface Page {
  build: string;  // this page's build, "" if unknown
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  reload(): void;
}

const tabStorage = (): Page["storage"] => { try { return sessionStorage; } catch { return null; } };
const browser = (): Page => ({
  build: (typeof document !== "undefined" && document.querySelector('meta[name="alfred-build"]')?.getAttribute("content")) || "",
  storage: tabStorage(),
  reload: () => location.reload(),
});

type Resume = { muted: boolean } | null;
export interface Session {
  busy(): boolean;  // voice session running: defer the reload
  resume(): Resume;  // session being reconnected, to carry across the reload
}

export function builds(page: Page = browser()) {
  let served = "";
  let session: Session = { busy: () => false, resume: () => null };
  const get = (k: string) => { try { return page.storage?.getItem(k) ?? null; } catch { return null; } };
  const set = (k: string, v: string | null) => {
    try { v == null ? page.storage?.removeItem(k) : page.storage?.setItem(k, v); } catch {}
  };

  // Reload now if a newer build is pending and not already tried by this tab (avoids loops), carrying `resume`
  // across. True if reloading.
  function reload(resume: Resume = session.resume()): boolean {
    if (!page.build || !served || served === page.build || get(RELOADED_KEY) === served) return false;
    set(RELOADED_KEY, served);
    if (resume) set(RESUME_KEY, resume.muted ? "muted" : "open");
    page.reload();
    return true;
  }

  return {
    // Record the served build id (non-strings keep the last) and reload unless a voice session runs. True if reloading.
    served(id: unknown, resume?: { muted: boolean }): boolean {
      if (typeof id === "string" && id) served = id;
      return !session.busy() && reload(resume);
    },
    follow(s: Session) { session = s; },
    reload,
    // Session to resume after a build reload; returned once.
    takeResume(): Resume {
      const v = get(RESUME_KEY);
      set(RESUME_KEY, null);
      return v ? { muted: v === "muted" } : null;
    },
  };
}

export const build = builds();
