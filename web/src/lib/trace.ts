// Earphone trace: what the page made of each press, sent to logs/web.log on the Mac (/api/log).
import { postAuthed } from "./api";
import { DEVICE, mobile } from "./device";

const traced: string[] = [];
let timer: ReturnType<typeof setTimeout> | undefined;

export function trace(line: string): void {
  if (!mobile) return;
  traced.push(`${new Date().toISOString().slice(11, 23)} ${line}`);
  if (traced.length > 50) traced.shift();
  clearTimeout(timer);
  timer = setTimeout(() => postAuthed("/api/log", { device: DEVICE, lines: traced.splice(0) }).catch(() => {}), 1500);
}
