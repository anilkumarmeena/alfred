// Passcode card, shown until a passcode is saved or after one is refused.
import { useRef } from "preact/hooks";
import { useSignalEffect } from "@preact/signals";
import { unlock } from "../state/session";
import { loginPrompt, loginShown, showLogin } from "../state/store";

export function Login() {
  const form = useRef<HTMLFormElement>(null), input = useRef<HTMLInputElement>(null);

  // On each prompt: shake if there's an error, and focus the field.
  useSignalEffect(() => {
    const { shake } = loginPrompt.value;
    if (!loginShown.peek()) return;
    const f = form.current;
    if (shake && f) { f.classList.remove("shake"); void f.offsetWidth; f.classList.add("shake"); }
    const t = setTimeout(() => input.current?.focus(), 50);
    return () => clearTimeout(t);
  });

  function submit(e: Event) {
    e.preventDefault();
    const v = input.current!.value.trim();
    if (!v) return showLogin("Enter your passcode.");
    input.current!.value = "";
    unlock(v);
  }

  return (
    <form id="login" ref={form} class="card" hidden={!loginShown.value} onSubmit={submit}>
      <h1>Identity check</h1>
      <p>Enter your passcode to bring JARVIS online.</p>
      <input id="passcode" ref={input} type="password" inputMode="numeric" autoComplete="current-password" placeholder="Passcode" aria-label="Passcode" />
      <button type="submit" class="primary">Unlock</button>
    </form>
  );
}
