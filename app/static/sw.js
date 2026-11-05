// Minimal service worker: Android Chrome only shows notifications through one.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: "window" }).then((wins) => (wins[0] ? wins[0].focus() : self.clients.openWindow("/"))),
  );
});
