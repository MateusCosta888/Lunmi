/**
 * Entrada do renderer (`desktop/src/main.tsx`).
 * O shell vive em `App.tsx`; aqui só montamos o React no documento.
 * BACKGROUND V1: `index.html?window=companion` monta o companheiro (orb/mini) sem App/useSession.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { CompanionApp } from "./features/companion/CompanionApp";
import { applyTheme } from "./shared/themeDom";
import "./styles.css";
import "./shared/motion.css";
import "./shared/soft-ui.css";

applyTheme("celestial");

const isCompanion = typeof window !== "undefined"
  && new URLSearchParams(window.location.search).get("window") === "companion";
// A janela companheira é transparente: o fundo opaco global só se aplica à janela principal.
if (isCompanion) document.documentElement.dataset.companion = "companion";

const container = document.getElementById("root");
if (container) {
  createRoot(container).render(
    <React.StrictMode>
      {isCompanion ? <CompanionApp /> : <App />}
    </React.StrictMode>
  );
}
