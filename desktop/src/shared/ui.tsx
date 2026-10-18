/**
 * Primitivas visuais compartilhadas do shell Lunmi (`desktop/src/shared/ui.tsx`, F3).
 *
 * A gramática é única e vive aqui: espaçamento em múltiplos de 4 px, raios (10–12 px em controles,
 * 14–18 px em cartões, cápsula no compositor), estados de hover/foco/pressionado/desabilitado e
 * ícones de traço que nunca substituem um rótulo acessível.
 *
 * Nenhuma cor literal: tudo vem de `shared/theme.ts`, a fonte de verdade da paleta navy sem neon.
 */
import React from "react";
import { MessageCircle, BrainCircuit, Database, Settings2, ChevronRight, ArrowUp, Square, Plus, CornerDownLeft, Monitor, MessageSquareText, Check, Palette, Keyboard, Sparkles, PanelLeftClose, PanelLeftOpen, Paperclip, UserRound, Minus, Copy, X, Plug, Mail } from "lucide-react";
import { TOKENS } from "./theme";
import brandSymbol from "../assets/branding/lunmi-symbol.svg";

/** Escala de espaçamento — múltiplos de 4 px (F3). */
export const SPACE = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24 } as const;

/** Raios da gramática: controles, cartões/balões e cápsula do compositor. */
export const RADIUS = { control: 10, card: 16, capsule: 999 } as const;

/** Tamanho base de conversa (14–15 px, entrelinha 1,55) e de texto secundário. */
export const TEXT = { chat: 14, body: 13, meta: 12 } as const;
export const CHAT_LINE_HEIGHT = 1.55;

export const primaryButtonStyle: React.CSSProperties = {
  padding: "8px 16px",
  borderRadius: `${RADIUS.control}px`,
  border: `1px solid ${TOKENS.accent}`,
  backgroundColor: TOKENS.accent,
  color: TOKENS.onAccent,
  fontSize: "13px",
  fontWeight: 600,
  cursor: "pointer",
};

export const secondaryButtonStyle: React.CSSProperties = {
  padding: "8px 14px",
  borderRadius: `${RADIUS.control}px`,
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgCardSubtle,
  color: TOKENS.textSecondary,
  fontSize: "13px",
  cursor: "pointer",
};

export const ghostButtonStyle: React.CSSProperties = {
  padding: "8px 12px",
  borderRadius: `${RADIUS.control}px`,
  border: "1px solid transparent",
  backgroundColor: "transparent",
  color: TOKENS.textSecondary,
  fontSize: "13px",
  cursor: "pointer",
};

export const dangerButtonStyle: React.CSSProperties = {
  ...secondaryButtonStyle,
  borderColor: TOKENS.error,
  color: TOKENS.error,
};

export const linkButtonStyle: React.CSSProperties = {
  border: "none",
  background: "transparent",
  color: TOKENS.accent,
  cursor: "pointer",
  fontSize: "13px",
  padding: 0,
};

export const cardStyle: React.CSSProperties = {
  padding: "16px",
  borderRadius: `${RADIUS.card}px`,
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgCard,
};

export const fieldStyle: React.CSSProperties = {
  padding: "8px 10px",
  borderRadius: `${RADIUS.control}px`,
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgInput,
  color: TOKENS.textPrimary,
  fontSize: "13px",
};

export const captionStyle: React.CSSProperties = {
  margin: 0,
  fontSize: `${TEXT.meta}px`,
  lineHeight: 1.5,
  color: TOKENS.textMuted,
};

export function badgeStyle(color: string): React.CSSProperties {
  return {
    flex: "0 0 auto",
    padding: "2px 9px",
    borderRadius: "999px",
    border: `1px solid ${color}`,
    color,
    fontSize: "11.5px",
    whiteSpace: "nowrap",
  };
}

/** Tom do estado de uma fase de tarefa — usado por TaskPanel e pela faixa persistente. */
export function phaseTone(phase: string): string {
  if (phase === "failed") {
    return TOKENS.error;
  }
  if (phase === "finished") {
    return TOKENS.success;
  }
  if (phase === "cancelled") {
    return TOKENS.warning;
  }
  return TOKENS.textSecondary;
}

const ICONS = {
  chat: MessageCircle, brain: BrainCircuit, memory: Database, settings: Settings2,
  chevron: ChevronRight, "arrow-up": ArrowUp, stop: Square, plus: Plus,
  enter: CornerDownLeft, monitor: Monitor, conversation: MessageSquareText, check: Check,
  palette: Palette, keyboard: Keyboard, sparkle: Sparkles, collapse: PanelLeftClose, expand: PanelLeftOpen,
  attach: Paperclip, user: UserRound,
  apps: Plug, mail: Mail,
  "win-min": Minus, "win-max": Square, "win-restore": Copy, "win-close": X,
} as const;
export type IconName = keyof typeof ICONS;

/** Lucide icons are decorative; controls keep their own accessible label. */
export function Icon({ name, size = 18 }: { name: IconName; size?: number }): React.ReactElement {
  const Glyph = ICONS[name];
  return <Glyph size={size} strokeWidth={1.65} aria-hidden="true" focusable="false" />;
}

/** Símbolo aprovado da Lunmi; a versão com lettering fica na apresentação pública. */
export function BrandMark({ size = 22 }: { size?: number }): React.ReactElement {
  return <img src={brandSymbol} width={size} height={size} alt="" aria-hidden="true" draggable={false} style={{ flexShrink: 0 }} />;
}
