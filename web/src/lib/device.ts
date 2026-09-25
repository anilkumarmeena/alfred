// What the page is running on, read once at load.
const ua = navigator.userAgent;

export const iOS = /iPhone|iPad|iPod/.test(ua);
export const DEVICE = iOS ? "iphone" : "browser";  // sent with the token and push requests, and the trace

// Phones and tablets (iPadOS says it's a Mac) get earphone controls; desktops keep their media keys for music.
export const mobile = iOS || /Android/i.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);

// Opened from the Home Screen icon rather than a Safari tab: iOS only allows push there.
export const standalone = matchMedia("(display-mode: standalone)").matches
  || (navigator as Navigator & { standalone?: boolean }).standalone === true;
