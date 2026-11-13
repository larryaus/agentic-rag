/**
 * Registers the app-shell service worker (public/sw.js). Production builds only: in
 * `vite dev` a worker would serve stale modules over the dev server's own.
 */
export function registerServiceWorker(): void {
  if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return;
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((cause: unknown) => {
      console.warn('Service worker registration failed', cause);
    });
  });
}
