import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import preact from "@preact/preset-vite";

const API = `http://127.0.0.1:${process.env.JARVIS_WEB_PORT || 8787}`;
const SW = fileURLToPath(new URL("./public/sw.js", import.meta.url));

// Serve /sw.js at the root in dev, as jarvis.web does, so the worker's scope is the whole app.
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

// Build id in <meta name="jarvis-build">: hash of the content-hashed file names, so any change alters it.
// jarvis.web reads it from index.html; open pages that see a different one reload (src/lib/build.ts).
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

// Output to web/dist, served by jarvis.web. Assets live under /static/ (URLs the manifest, service worker and
// Home Screen installs depend on).
export default defineConfig({
  base: "/static/",
  plugins: [preact(), rootServiceWorker(), buildId()],
  server: {
    host: process.env.HOST || "127.0.0.1",
    port: Number(process.env.WEB_PORT || 5173),
    proxy: { "/api": API },
  },
  build: {
    target: "es2020",  // iOS 16.4+, the oldest Safari with Web Push
    modulePreload: { polyfill: false },
    rollupOptions: {
      // LiveKit is most of the bundle and rarely changes: separate chunk stays cached.
      output: { manualChunks: (id) => (id.includes("livekit-client") ? "livekit" : undefined) },
    },
    chunkSizeWarningLimit: 600,
  },
});
