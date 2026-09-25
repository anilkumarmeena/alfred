import { PASSCODE_KEY, storage } from "./storage";

// POST JSON to jarvis.web with the saved passcode, which every endpoint but /api/push/key checks.
export function postAuthed(path: string, fields: Record<string, unknown>): Promise<Response> {
  return fetch(path, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ passcode: storage.get(PASSCODE_KEY), ...fields }),
  });
}

// jarvis.web turned the saved passcode down.
export class Unauthorized extends Error {}

// postAuthed, then the reply's JSON. Throws Unauthorized for a wrong passcode, or the server's error.
export async function postAuthedJson(path: string, fields: Record<string, unknown>): Promise<Record<string, any>> {
  const res = await postAuthed(path, fields);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) throw new Unauthorized("Wrong passcode.");
  if (!res.ok) throw new Error(data.error || "Couldn't reach JARVIS.");
  return data;
}

// One of the control panel's actions (jarvis.panel, at /api/panel/ACTION), and what it returns.
export async function panelCall<T = any>(action: string, fields: Record<string, unknown> = {}): Promise<T> {
  return (await postAuthedJson(`/api/panel/${action}`, fields)).result as T;
}
