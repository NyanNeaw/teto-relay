// Makes the panel installable as an app (Edge/Chrome: "Install Teto Relay").
// Nothing is cached: the panel is only useful while the program is running,
// and a stale copy would show controls for a relay that isn't there.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
