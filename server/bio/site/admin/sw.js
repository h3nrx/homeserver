// Makes the admin installable as an app. Nothing is cached: the panel always
// talks to the live server. If the laptop can't be reached, a small offline
// page is shown instead of the browser's error screen.
const OFFLINE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#06080f"><title>__BRAND__ · offline</title>
<style>
  html, body { height: 100%; margin: 0; }
  body { display: grid; place-items: center; background: #06080f; color: #eef1f7;
    font: 15px/1.5 "Segoe UI", system-ui, sans-serif; text-align: center; padding: 24px; box-sizing: border-box; }
  h1 { font-size: 22px; font-weight: 600; letter-spacing: -.02em; margin: 0 0 6px; }
  p { margin: 0 0 22px; color: rgba(238,241,247,.6); }
  button { border: 0; border-radius: 999px; padding: 11px 22px; background: #eef1f7; color: #0a0c13; font: 600 14px system-ui, sans-serif; cursor: pointer; }
  .moon { font-size: 40px; margin-bottom: 14px; }
</style></head><body><div>
  <div class="moon">☾</div>
  <h1>The laptop can't be reached</h1>
  <p>Check your internet connection, or the server might be asleep.</p>
  <button onclick="location.reload()">Try again</button>
</div><script>addEventListener("online", () => location.reload());</script></body></html>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.mode !== "navigate") return;
  e.respondWith(fetch(e.request).catch(() =>
    new Response(OFFLINE, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } })));
});
