import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { BASE } from "./api";
import "./styles.css";

// Installable app (PWA). Browsers only allow service workers on HTTPS or localhost. Through the fleet hub
// (/s/<site>/) the hub has its own service worker; one scoped under a site would shadow the others.
if (!BASE && "serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
