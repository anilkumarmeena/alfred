// Which build of the page this is, and reloading once jarvis.web serves a newer one (after `make web-build` and a
// restart), rather than running stale code. The build adds <meta name="jarvis-build"> (vite.config.ts); jarvis.web
// says what it serves now in /api/events' hello and /api/token's reply. Under the dev server there's no meta, so
// nothing here reloads.

const RELOADED_KEY = "jarvis.reloadedFor";  // sessionStorage: the build this tab last reloaded to get
const RESUME_KEY = "jarvis.resume";  // sessionStorage: a voice session to bring back after the reload, "muted" or "open"

export interface Page {
  build: string;  // this page's build, "" if it doesn't know
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  reload(): void;
}

const tabStorage = (): Page["storage"] => { try { return sessionStorage; } catch { return null; } };
const browser = (): Page => ({
  build: (typeof document !== "undefined" && document.querySelector('meta[name="jarvis-build"]')?.getAttribute("content")) || "",
  storage: tabStorage(),
  reload: () => location.reload(),
});

type Resume = { muted: boolean } | null;
export interface Session {
  busy(): boolean;  // a voice session is going, which a reload would cut off: wait for it to end
  resume(): Resume;  // a lost one is coming back, which the reloaded page should carry on with
}

export function builds(page: Page = browser()) {
  let served = "";
  let session: Session = { busy: () => false, resume: () => null };
  const get = (k: string) => { try { return page.storage?.getItem(k) ?? null; } catch { return null; } };
  const set = (k: string, v: string | null) => {
    try { v == null ? page.storage?.removeItem(k) : page.storage?.setItem(k, v); } catch {}
  };

  // Now, if newer than this page and not one this tab already reloaded for (if the reload didn't take, don't loop),
  // bringing the voice session back after it if `resume` (or the session) says how. True if reloading.
  function reload(resume: Resume = session.resume()): boolean {
    if (!page.build || !served || served === page.build || get(RELOADED_KEY) === served) return false;
    set(RELOADED_KEY, served);
    if (resume) set(RESUME_KEY, resume.muted ? "muted" : "open");
    page.reload();
    return true;
  }

  return {
    // What jarvis.web serves now (a build id, or anything else to leave it as it was): reloads at once into a newer
    // build unless a voice session is going. True if reloading.
    served(id: unknown, resume?: { muted: boolean }): boolean {
      if (typeof id === "string" && id) served = id;
      return !session.busy() && reload(resume);
    },
    follow(s: Session) { session = s; },
    reload,
    // The session the page was running when it reloaded for a new build, once: bring it back.
    takeResume(): Resume {
      const v = get(RESUME_KEY);
      set(RESUME_KEY, null);
      return v ? { muted: v === "muted" } : null;
    },
  };
}

export const build = builds();
