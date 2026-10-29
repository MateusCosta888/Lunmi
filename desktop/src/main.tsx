/**
 * Entrada do renderer (`desktop/src/main.tsx`).
 * O shell vive em `App.tsx`; aqui só montamos o React no documento.
 * BACKGROUND V1: `index.html?window=companion` monta o companheiro (orb/mini) sem App/useSession.
 */
import React from "react";
import { createRoot } from "react-dom/client";
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
  const root = createRoot(container);
  // The Node never needs to load the main window's settings, onboarding or account panels.
  const load = isCompanion
    ? import("./features/companion/CompanionApp").then(module => module.CompanionApp)
    : import("./App").then(module => module.App);
  void load.then(Component => {
    root.render(<React.StrictMode><Component /></React.StrictMode>);
  }).catch(() => {
    root.render(<p role="alert">Não foi possível abrir a interface. Reinicia a Lunmi.</p>);
  });
}
