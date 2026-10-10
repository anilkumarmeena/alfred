// Panel Markdown: sanitised rendering (lib/markdown.ts) and an editor with preview.
import { useMemo } from "preact/hooks";
import { useSignal } from "@preact/signals";
import { markdown, markdownInline } from "../../lib/markdown";
import { Empty } from "./bits";

// Block document, or with `inline` a single row line. `links` false inside clickable elements.
export function Markdown({ text, inline = false, links = true, class: cls = "" }: {
  text: string; inline?: boolean; links?: boolean; class?: string;
}) {
  const html = useMemo(() => (inline ? markdownInline(text, links) : markdown(text)), [text, inline, links]);
  const Tag = inline ? "span" : "div";
  return <Tag class={`md${inline ? " line" : ""}${cls ? ` ${cls}` : ""}`} dangerouslySetInnerHTML={{ __html: html }} />;
}

// Textarea with Write/Preview tabs
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
