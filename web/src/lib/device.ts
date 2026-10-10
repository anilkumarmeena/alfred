// Platform detection, read once at load.
const ua = navigator.userAgent;

export const iOS = /iPhone|iPad|iPod/.test(ua);
export const android = /Android/i.test(ua);
export const DEVICE = iOS ? "iphone" : android ? "android" : "browser";  // sent with token/push requests and traces

// Phones and tablets (iPadOS reports as Mac) get earphone controls; desktops keep media keys for music.
export const mobile = iOS || android || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);

// Launched from the Home Screen: iOS allows push only there.
export const standalone = matchMedia("(display-mode: standalone)").matches
  || (navigator as Navigator & { standalone?: boolean }).standalone === true;
