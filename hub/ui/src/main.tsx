import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "@site/styles.css";
import "./hub.css";
import { installLiveTelemetry } from "./liveTelemetry";

// live tiles report their time to first frame (Servers cards: "Live start")
installLiveTelemetry();

// Web push for alerts (Account → Notifications). Scoped to the hub's own pages only.
if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {}));
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
