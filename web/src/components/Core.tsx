// The core: a canvas reactor inside a button. A tap connects, mutes or unmutes, or brings the mic back.
import { useEffect, useRef } from "preact/hooks";
import { mountReactor } from "../lib/reactor";
import { activate, reactorInput } from "../state/session";
import { coreLabel } from "../state/store";

export function Core() {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => mountReactor(canvas.current!, reactorInput), []);

  return (
    <button id="core" aria-label={coreLabel} onClick={activate}>
      <canvas id="reactor" ref={canvas} />
    </button>
  );
}
