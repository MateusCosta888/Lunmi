/**
 * Entrada do renderer (`desktop/src/main.tsx`).
 * O shell vive em `App.tsx`; aqui só montamos o React no documento.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { applyTheme } from "./shared/themeDom";
import "./styles.css";
import "./shared/motion.css";
import "./shared/soft-ui.css";

applyTheme("celestial");

const container = document.getElementById("root");
if (container) {
  createRoot(container).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}
