// A connection's server address, checked as the server checks it (mcp_servers.bad_address), so a typo is said before
// saving. Moving to another host forgets the connection's keys, so the page asks first.

// Why `url` can't be a server address, or "". "" itself is fine: the package's default server.
export function badAddress(url: string): string {
  const u = parsed(url);
  const local = u?.protocol === "http:" && ["localhost", "127.0.0.1"].includes(u.hostname);
  if (url && (!u || /\s/.test(url) || !/^https?:\/\/[^/]/.test(url) || !(u.protocol === "https:" || local)
    || u.username || u.password || u.search || u.hash)) {
    return "Give the server's https:// address, like https://git.example.com/api/v4.";
  }
  return "";
}

function parsed(url: string): URL | null {
  try { return new URL(url); } catch { return null; }
}

// Scheme, host and port: the keys belong to these. "" for the package's default.
export function hostOf(url: string): string {
  const u = parsed(url);
  return u ? `${u.protocol}//${u.host}` : url;
}
