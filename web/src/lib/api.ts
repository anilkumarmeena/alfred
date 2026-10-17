import type { PanelAction } from "./panelActions";
import { PASSCODE_KEY, storage } from "./storage";

// POST JSON with the saved passcode (checked by every endpoint except /api/push/key).
export function postAuthed(path: string, fields: Record<string, unknown>): Promise<Response> {
  return fetch(path, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ passcode: storage.get(PASSCODE_KEY), ...fields }),
  });
}

// The server rejected the saved passcode.
export class Unauthorized extends Error {}

// postAuthed and parse JSON. Throws Unauthorized for a bad passcode, else the server's error.
export async function postAuthedJson(path: string, fields: Record<string, unknown>): Promise<Record<string, any>> {
  const res = await postAuthed(path, fields);
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) throw new Unauthorized("Wrong passcode.");
  if (!res.ok) throw new Error(data.error || "Couldn't reach JARVIS.");
  return data;
}

// Call a panel action at /api/panel/ACTION.
export async function panelCall<T = any>(action: PanelAction, fields: Record<string, unknown> = {}): Promise<T> {
  return (await postAuthedJson(`/api/panel/${action}`, fields)).result as T;
}
