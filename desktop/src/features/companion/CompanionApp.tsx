/**
 * Raiz da janela companheira (`desktop/src/features/companion/CompanionApp.tsx`).
 *
 * `index.html?window=companion` monta ISTO, nunca o `App`/`useSession` completos. A janela é o
 * envelope fixo do LUNMI NODE e o main é a autoridade da superfície: o renderer desenha o objeto
 * contínuo (`NodeSurface`) dentro do rect que o main dá e pede apenas ações fechadas. O modo
 * legado (`hidden`/`orb`/`mini`) deixou de decidir o que se vê — a UI nova usa `node.surface` e
 * `node.phase`, o que o main publica.
 *
 * O tema continua a ser aplicado (a janela acompanha a principal), mas o Node é preto
 * constante: a pele não usa tokens de tema.
 */
import React, { useEffect } from "react";
import { usePresenceTheme } from "../../shared/ipc/presence";
import { applyTheme } from "../../shared/themeDom";
import { NodeSurface } from "./NodeSurface";
import "./companion.css";

export function CompanionApp(): React.ReactElement | null {
  const theme = usePresenceTheme();
  useEffect(() => {
    if (theme) applyTheme(theme);
  }, [theme]);
  return <NodeSurface />;
}
