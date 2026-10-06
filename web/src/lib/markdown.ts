// Markdown to safe HTML, for the panel's task summaries and questions, memory, to-dos and "about you". The text can
// come from an email or a web page a task read, so it gets two walls: raw HTML in it is shown as text, never parsed,
// and DOMPurify then keeps only the tags and attributes below. Images become links, so nothing loads from elsewhere
// just by being shown, and links open in a new tab without telling the page they lead to where they came from.
import DOMPurify from "dompurify";
import { Marked } from "marked";

const ESCAPES: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escape = (s: string) => s.replace(/[&<>"']/g, (c) => ESCAPES[c] ?? c);

const md = new Marked({
  gfm: true, breaks: true, async: false,  // breaks: "one point per line" text keeps its lines
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
// Links out only: no javascript:, data: or links into the panel's own server. (DOMPurify's own URI check is kept as it
// is, since it also judges every other attribute's value.)
const LINK = /^(?:https?:|mailto:|#)/i;
const NO_LINKS = { ...PURIFY, ALLOWED_TAGS: TAGS.filter((t) => t !== "a") };  // a link's text stays, as text

let hooked = false;

function purify(html: string, links = true): string {
  if (!hooked) {
    DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A" && !LINK.test(node.getAttribute("href") ?? "")) node.removeAttribute("href");
      if (node.tagName === "A" && node.hasAttribute("href")) {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer nofollow");
      }
      if (node.tagName === "INPUT") {  // only a task list's tick box, which can't be changed here
        if (node.getAttribute("type") !== "checkbox") node.remove();
        else node.setAttribute("disabled", "");
      }
    });
    hooked = true;
  }
  return DOMPurify.sanitize(html, links ? PURIFY : NO_LINKS) as string;
}

// A whole document: paragraphs, headings, lists and tick boxes, code, tables
export const markdown = (text: string): string => purify(md.parse(text) as string);

// One line inside a row (a to-do, a memory fact): bold, italics, code and links, no blocks. Without links inside
// something that's itself clicked, like a task's card.
export const markdownInline = (text: string, links = true): string => purify(md.parseInline(text) as string, links);
