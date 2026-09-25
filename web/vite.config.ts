import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import preact from "@preact/preset-vite";

const API = `http://127.0.0.1:${process.env.JARVIS_WEB_PORT || 8787}`;
const SW = fileURLToPath(new URL("./public/sw.js", import.meta.url));

// jarvis.web serves the page at / and its service worker at /sw.js, so the worker's scope is the whole
// app. The dev server does the same for /sw.js; Vite itself redirects / to the base.
function rootServiceWorker(): Plugin {
  return {
    name: "jarvis-root-service-worker",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url !== "/sw.js") return next();
        res.setHeader("Content-Type", "text/javascript");
        res.setHeader("Service-Worker-Allowed", "/");
        res.end(readFileSync(SW));
      });
    },
  };
}

// The build's id, in <meta name="jarvis-build">: a hash of its files' names, which carry their contents' hashes, so it
// changes when anything the page loads does. jarvis.web reads it back from index.html, and an open page that sees a
// different one reloads (src/lib/build.ts).
function buildId(): Plugin {
  return {
    name: "jarvis-build-id",
    apply: "build",
    transformIndexHtml: {
      order: "post",
      handler(_, ctx) {
        const names = Object.keys(ctx.bundle ?? {}).sort().join("\n");
        const id = createHash("sha256").update(names).digest("hex").slice(0, 12);
        return [{ tag: "meta", attrs: { name: "jarvis-build", content: id }, injectTo: "head" }];
      },
    },
  };
}

// Built into web/dist, which jarvis.web serves. Everything but the page lives under /static/, the
// URLs the manifest, the service worker and Home Screen installs already use.
export default defineConfig({
  base: "/static/",
  plugins: [preact(), rootServiceWorker(), buildId()],
  server: {
    host: process.env.HOST || "127.0.0.1",
    port: Number(process.env.WEB_PORT || 5173),
    proxy: { "/api": API },  // the token, push and log endpoints stay on jarvis.web
  },
  build: {
    target: "es2020",  // iOS 16.4+, the oldest Safari that can take push
    modulePreload: { polyfill: false },
    rollupOptions: {
      // LiveKit is most of the bundle and changes far less often than the page: its own file stays cached.
      output: { manualChunks: (id) => (id.includes("livekit-client") ? "livekit" : undefined) },
    },
    chunkSizeWarningLimit: 600,
  },
});
