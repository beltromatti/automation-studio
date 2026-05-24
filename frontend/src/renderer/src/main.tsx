import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";
import App from "./App";
import "./index.css";

// Tag the document with the OS so CSS can position the custom title bar around
// the native window controls (mac traffic lights on the left; win/linux controls
// on the right). Defaults to darwin if the preload didn't inject a platform.
document.documentElement.dataset.platform =
  (window as { api?: { platform?: string } }).api?.platform ?? "darwin";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <HashRouter>
    <App />
  </HashRouter>,
);
