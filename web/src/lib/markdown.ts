// Markdown to safe HTML for panel text. Input may come from untrusted email or web pages, so:
// - raw HTML is escaped, never parsed; DOMPurify then allows only the tags/attributes below
// - images become links, so nothing remote loads on display
// - links open in a new tab with noopener/noreferrer
import DOMPurify from "dompurify";
import { Marked } from "marked";

const ESCAPES: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escape = (s: string) => s.replace(/[&<>"']/g, (c) => ESCAPES[c] ?? c);

const md = new Marked({
  gfm: true, breaks: true, async: false,  // breaks: keep single newlines
  renderer: {
    html: ({ text }) => escape(text),
    image: ({ href, text }) => `<a href="${escape(href)}">${escape(text || href)}</a>`,
  },
});

const TAGS = [
  "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li",
  "input", "a", "table", "thead", "tbody", "tr", "th", "td",
];
const PURIFY = { ALLOWED_TAGS: TAGS, ALLOWED_ATTR: ["href", "title", "type", "checked", "disabled", "start", "align"] };
// Outbound hrefs only: blocks javascript:, data: and relative links. (DOMPurify's URI check still covers other attrs.)
const LINK = /^(?:https?:|mailto:|#)/i;
const NO_LINKS = { ...PURIFY, ALLOWED_TAGS: TAGS.filter((t) => t !== "a") };  // keeps link text

let hooked = false;

function purify(html: string, links = true): string {
  if (!hooked) {
    DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A" && !LINK.test(node.getAttribute("href") ?? "")) node.removeAttribute("href");
      if (node.tagName === "A" && node.hasAttribute("href")) {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer nofollow");
      }
      if (node.tagName === "INPUT") {  // task-list checkbox, read-only
        if (node.getAttribute("type") !== "checkbox") node.remove();
        else node.setAttribute("disabled", "");
      }
    });
    hooked = true;
  }
  return DOMPurify.sanitize(html, links ? PURIFY : NO_LINKS) as string;
}

// Block Markdown: paragraphs, headings, lists, checkboxes, code, tables
export const markdown = (text: string): string => purify(md.parse(text) as string);

// Inline Markdown for a single row (to-do, memory fact). Drop links when the row itself is clickable.
export const markdownInline = (text: string, links = true): string => purify(md.parseInline(text) as string, links);
