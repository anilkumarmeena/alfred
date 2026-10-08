// Making a photo small enough to send JARVIS: at most MAX_EDGE pixels on its long side, as a JPEG, drawn through a
// canvas, which also turns it the right way up. jarvis.state.pictures checks it again on the Mac, and converts what
// this browser can't draw (an iPhone's HEIC, off Safari) itself.

export const MAX_EDGE = 1568;  // what Claude reads well: more costs tokens without it seeing more
const QUALITY = 0.85;

// The size to draw a w x h picture at: scaled down to fit max on its long side, never up.
export function fitSize(w: number, h: number, max = MAX_EDGE): [number, number] {
  const s = Math.min(1, max / Math.max(w, h, 1));
  return [Math.max(1, Math.round(w * s)), Math.max(1, Math.round(h * s))];
}

async function decode(file: Blob): Promise<ImageBitmap | HTMLImageElement> {
  try { return await createImageBitmap(file); } catch {}
  const url = URL.createObjectURL(file);  // older Safari: through an <img>
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } finally { URL.revokeObjectURL(url); }
}

// The photo as a JPEG that fits, or the file as it is if this browser can't draw it.
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

// The blob's bytes as base64, for a JSON body.
export function base64(blob: Blob): Promise<string> {
  return new Promise((done, fail) => {
    const reader = new FileReader();
    reader.onload = () => done(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => fail(reader.error ?? new Error("Couldn't read the photo."));
    reader.readAsDataURL(blob);
  });
}
