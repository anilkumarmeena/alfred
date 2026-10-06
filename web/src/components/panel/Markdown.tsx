// Markdown in the panel: shown rendered wherever it's read (lib/markdown.ts makes it safe), and written with a preview
// wherever it's edited.
import { useMemo } from "preact/hooks";
import { useSignal } from "@preact/signals";
import { markdown, markdownInline } from "../../lib/markdown";
import { Empty } from "./bits";

// A document (a task's summary or question), or with `inline` one line of a row (a to-do, a memory fact). `links`
// false for a line inside something that's clicked itself.
export function Markdown({ text, inline = false, links = true, class: cls = "" }: {
  text: string; inline?: boolean; links?: boolean; class?: string;
}) {
  const html = useMemo(() => (inline ? markdownInline(text, links) : markdown(text)), [text, inline, links]);
  const Tag = inline ? "span" : "div";
  return <Tag class={`md${inline ? " line" : ""}${cls ? ` ${cls}` : ""}`} dangerouslySetInnerHTML={{ __html: html }} />;
}

// A text box with Write and Preview above it
export function MarkdownEditor({ value, onInput, label, placeholder, rows = 7 }: {
  value: string; onInput: (text: string) => void; label: string; placeholder?: string; rows?: number;
}) {
  const preview = useSignal(false);
  return (
    <div class="mdedit">
      <div class="mdtabs">
        <label class="lbl">{label}</label>
        <div class="radios" role="radiogroup" aria-label={`${label}: write or preview`}>
          <button type="button" role="radio" aria-checked={!preview.value} class={preview.value ? "" : "on"}
            onClick={() => { preview.value = false; }}>Write</button>
          <button type="button" role="radio" aria-checked={preview.value} class={preview.value ? "on" : ""}
            onClick={() => { preview.value = true; }}>Preview</button>
        </div>
      </div>
      {preview.value
        ? <div class="field mdpreview">{value.trim() ? <Markdown text={value} /> : <Empty>Nothing to preview yet.</Empty>}</div>
        : <textarea class="field" rows={rows} value={value} placeholder={placeholder} aria-label={label}
            onInput={(e) => onInput(e.currentTarget.value)} />}
    </div>
  );
}
