// Downscale a photo to a JPEG at most MAX_EDGE on the long side (canvas also applies EXIF orientation).
// The server re-checks and converts what the browser can't decode (e.g. HEIC outside Safari).

export const MAX_EDGE = 1568;  // Claude's useful max; larger costs tokens for no gain
const QUALITY = 0.85;

// Fit w x h within max on the long side; never upscale.
export function fitSize(w: number, h: number, max = MAX_EDGE): [number, number] {
  const s = Math.min(1, max / Math.max(w, h, 1));
  return [Math.max(1, Math.round(w * s)), Math.max(1, Math.round(h * s))];
}

async function decode(file: Blob): Promise<ImageBitmap | HTMLImageElement> {
  try { return await createImageBitmap(file); } catch {}
  const url = URL.createObjectURL(file);  // fallback: decode via <img> (older Safari)
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } finally { URL.revokeObjectURL(url); }
}

// JPEG that fits, or the original file if the browser can't decode it.
export async function shrink(file: Blob): Promise<Blob> {
  let img: ImageBitmap | HTMLImageElement;
  try { img = await decode(file); } catch { return file; }
  const [w, h] = img instanceof HTMLImageElement ? [img.naturalWidth, img.naturalHeight] : [img.width, img.height];
  const [cw, ch] = fitSize(w, h);
  const canvas = document.createElement("canvas");
  canvas.width = cw; canvas.height = ch;
  const ctx = canvas.getContext("2d");
  if (!ctx) return file;
  ctx.drawImage(img, 0, 0, cw, ch);
  if (!(img instanceof HTMLImageElement)) img.close();
  const blob = await new Promise<Blob | null>((done) => canvas.toBlob(done, "image/jpeg", QUALITY));
  return blob ?? file;
}

// Blob to base64, for a JSON body.
export function base64(blob: Blob): Promise<string> {
  return new Promise((done, fail) => {
    const reader = new FileReader();
    reader.onload = () => done(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => fail(reader.error ?? new Error("Couldn't read the photo."));
    reader.readAsDataURL(blob);
  });
}
