/**
 * Capturador de atalho (`desktop/src/features/voice/KeyRecorder.tsx`, V2 §4.2).
 *
 * Sem campo de texto: «Alterar» pausa o detector global (`voiceInputShortcut.pause()`), captura
 * `keydown`/`keyup` do documento e apresenta as teclas na ORDEM FÍSICA premida; aplica ao largar
 * tudo, enviando ao `set()` o acelerador canónico (Ctrl, Alt, Shift, Super, tecla final) — a
 * deteção compara o conjunto, a apresentação mantém a ordem. Escape abandona; blur do documento
 * abandona e só rearma quando nenhuma tecla estiver fisicamente em baixo; o `resume()` do detector
 * acontece apenas com as teclas livres. Válidos: 2+ modificadores sozinhos ou 1+ modificador +
 * tecla final (A–Z, 0–9, F1–F24, Space, Tab, Enter, setas). AltGr (`AltRight`/`AltGraph`) e o seu
 * `Ctrl` sintético são ambíguos e rejeitam a captura; duplicados rejeitam; `hold_unavailable` do
 * pause/resume é reportado como está. O valor mostrado após reabrir vem do `get()` (o main
 * preserva a ordem visual normalizada).
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { getBridgeApi } from "../../shared/ipc/client";
import type { VoiceInputShortcutApi } from "../../shared/ipc/client";

/** Subconjeto da API fechada §4.3 que este componente usa (o `onHold` é do controlador). */
export type ShortcutApi = Pick<VoiceInputShortcutApi, "get" | "set" | "pause" | "resume">;

const MOD_LABEL: Record<string, string> = { Control: "Ctrl", Alt: "Alt", Shift: "Shift", Meta: "Super" };

const canonicalOfCode = (code: string): { kind: "mod" | "final"; name: string } | null => {
  if (code === "ControlLeft" || code === "ControlRight") return { kind: "mod", name: "Control" };
  if (code === "ShiftLeft" || code === "ShiftRight") return { kind: "mod", name: "Shift" };
  if (code === "AltLeft") return { kind: "mod", name: "Alt" };
  if (code === "MetaLeft" || code === "MetaRight") return { kind: "mod", name: "Meta" };
  if (/^Key[A-Z]$/.test(code)) return { kind: "final", name: code.slice(3) };
  if (/^Digit[0-9]$/.test(code)) return { kind: "final", name: code.slice(5) };
  if (/^F([1-9]|[12][0-9]|3[0-9]|4[0-4])$/.test(code)) return { kind: "final", name: code };
  if (code === "Space") return { kind: "final", name: "Space" };
  if (code === "Tab") return { kind: "final", name: "Tab" };
  if (code === "Enter") return { kind: "final", name: "Enter" };
  if (code === "ArrowLeft") return { kind: "final", name: "Left" };
  if (code === "ArrowRight") return { kind: "final", name: "Right" };
  if (code === "ArrowUp") return { kind: "final", name: "Up" };
  if (code === "ArrowDown") return { kind: "final", name: "Down" };
  return null;
};

export const shortcutErrorMessage = (error: string): string => {
  if (error === "invalid_shortcut") return "Esse atalho é inválido; o atalho anterior conserva-se.";
  if (error === "shortcut_conflict") return "Esse atalho já está a ser usado; o atalho anterior conserva-se.";
  if (error === "io_error") return "Não foi possível guardar o atalho; o atalho anterior mantém-se.";
  if (error === "hold_unavailable") return "O observador de teclas não está pronto; tente novamente mais tarde.";
  return "";
};

export function useVoiceShortcutApi(): ShortcutApi | undefined {
  const api = getBridgeApi()?.voiceInputShortcut as ShortcutApi | undefined;
  return typeof api?.get === "function" && typeof api?.set === "function" ? api : undefined;
}

export interface KeyRecorderProps {
  api: ShortcutApi;
  disabled?: boolean;
  onApplied: (accelerator: string | null) => void;
  onError: (message: string) => void;
}

export function KeyRecorder({ api, disabled = false, onApplied, onError }: KeyRecorderProps): React.ReactElement {
  const [capturing, setCapturing] = useState(false);
  const [keys, setKeys] = useState<string[]>([]); // nomes lógicos na ordem física premida
  const pressedRef = useRef<Set<string>>(new Set()); // códigos em baixo agora (blur honesto)
  const sequenceRef = useRef<{ code: string; name: string; kind: "mod" | "final" }[]>([]);
  const ambiguousRef = useRef(false);
  const deferredKeyupRef = useRef<((event: KeyboardEvent) => void) | null>(null);
  const capturingRef = useRef(false);
  capturingRef.current = capturing;
  const pausedRef = useRef(false); // detector pausado e ainda sem retoma emitida
  const unmountedRef = useRef(false);

  const complete = useCallback(async () => {
    const seq = [...sequenceRef.current];
    setCapturing(false);
    setKeys([]);
    sequenceRef.current = [];
    const ambiguous = ambiguousRef.current;
    ambiguousRef.current = false;
    if (ambiguous) {
      onError("AltGr não pode fazer parte do atalho; a captura foi cancelada.");
      return;
    }
    if (seq.length === 0) return;
    const mods = seq.filter(entry => entry.kind === "mod").map(entry => entry.name);
    const finals = seq.filter(entry => entry.kind === "final").map(entry => entry.name);
    if (new Set(mods).size !== mods.length || new Set(finals).size !== finals.length) {
      onError("Há teclas repetidas na combinação; nada foi alterado.");
      return;
    }
    // §4.2 (precisão do Root): a GUARDA é a ordem visual premida (ex.: `Alt+Ctrl`, `V+Ctrl`);
    // `set` recebe essa ordem normalizada e `get` devolve-a. A ordem canónica estável
    // (Ctrl, Alt, Shift, Super, final) é interna ao main/helper/parser — não da UI.
    let accelerator = "";
    const okGrammar =
      (finals.length === 0 && mods.length >= 2) ||
      (finals.length === 1 && mods.length >= 1 && finals.length + mods.length <= 5);
    if (!okGrammar) {
      onError(finals.length > 1
        ? "Só uma tecla final é permitida; nada foi alterado."
        : "É preciso pelo menos um modificador com uma tecla final, ou dois modificadores; nada foi alterado.");
      return;
    }
    accelerator = seq.map(entry => MOD_LABEL[entry.name] ?? entry.name).join("+");
    try {
      const result = await api.set(accelerator);
      if (result.error) onError(shortcutErrorMessage(result.error) || "O atalho não pôde ser registado; o anterior conserva-se.");
      onApplied(result.accelerator);
    } catch {
      onError("Não foi possível comunicar com o observador de atalhos; o atalho anterior mantém-se.");
    }
  }, [api, onApplied, onError]);

  const abandon = useCallback(() => {
    setCapturing(false);
    setKeys([]);
    sequenceRef.current = [];
    ambiguousRef.current = false;
  }, []);

  const resumeWhenFree = useCallback(async () => {
    if (pressedRef.current.size > 0) return false;
    pausedRef.current = false; // a retoma vai ser emitida agora (ou o API não tem pause nativo)
    if (!api.resume) return true;
    try {
      const result = await api.resume();
      if (!result.ok && result.error) onError(shortcutErrorMessage(result.error) || "O detector não retomou.");
      return result.ok;
    } catch {
      onError("O detector de atalho não respondeu; nada foi retomado.");
      return false;
    }
  }, [api, onError]);

  // Escape/blur/troca abandonam, mas o `resume()` do detector só pode acontecer com as teclas
  // fisicamente livres: enquanto alguma estiver em baixo, guarda-se um escuta-keyup única vez.
  const resumeWhenKeysFree = useCallback(() => {
    if (pressedRef.current.size === 0) { void resumeWhenFree(); return; }
    if (deferredKeyupRef.current) return; // uma escuta por vez
    const onKeyup = (event: KeyboardEvent) => {
      pressedRef.current.delete(event.code);
      if (pressedRef.current.size === 0) {
        document.removeEventListener("keyup", onKeyup);
        deferredKeyupRef.current = null;
        void resumeWhenFree();
      }
    };
    deferredKeyupRef.current = onKeyup;
    document.addEventListener("keyup", onKeyup);
  }, [resumeWhenFree]);

  // Desmontar devolve SEMPRE o controlo ao observador: o helper nativo garante esperar todas
  // as teclas ficarem livres e nunca sintetiza pressed/released. Sem isto, cancelar com uma
  // tecla em baixo e mudar de vista deixava o detector pausado até um próximo picker.
  // Nenhum listener órfão sobrevive ao componente; nenhum polling é introduzido.
  useEffect(() => () => {
    unmountedRef.current = true;
    if (deferredKeyupRef.current) {
      document.removeEventListener("keyup", deferredKeyupRef.current);
      deferredKeyupRef.current = null;
    }
    if (pausedRef.current) {
      pausedRef.current = false;
      void api.resume?.().catch(() => undefined);
    }
  }, [api]);

  useEffect(() => {
    if (!capturing) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!capturingRef.current) return;
      event.preventDefault();
      if (event.key === "Escape") { abandon(); resumeWhenKeysFree(); return; }
      if (event.repeat) return; // auto-repeat não cria turnos nem duplica teclas
      if (event.code === "AltRight" || event.key === "AltGraph") { ambiguousRef.current = true; return; }
      const mapped = canonicalOfCode(event.code);
      if (!mapped) { ambiguousRef.current = true; onError("Essa tecla não pode fazer parte do atalho."); abandon(); resumeWhenKeysFree(); return; }
      pressedRef.current.add(event.code);
      if (!sequenceRef.current.some(entry => entry.code === event.code)) {
        sequenceRef.current = [...sequenceRef.current, { code: event.code, ...mapped }];
        setKeys(sequenceRef.current.map(entry => MOD_LABEL[entry.name] ?? entry.name));
      }
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (!capturingRef.current) return;
      pressedRef.current.delete(event.code);
      if (pressedRef.current.size === 0 && sequenceRef.current.length > 0) {
        void complete().then(() => { void resumeWhenFree(); });
      }
    };
    const onBlurOrVisibility = () => {
      // Blur abandona e NÃO rearma enquanto uma tecla estiver fisicamente premida.
      if (!capturingRef.current) return;
      abandon();
      resumeWhenKeysFree();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlurOrVisibility);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlurOrVisibility);
    };
  }, [abandon, complete, onError, resumeWhenFree, resumeWhenKeysFree, capturing]);

  const begin = async () => {
    if (capturing || disabled) return;
    if (api.pause) {
      try {
        const result = await api.pause();
        if (unmountedRef.current) {
          // A promessa do pause resolveu depois de desmontar: devolver o controlo
          // imediatamente — o pause não pode ficar preso.
          if (result.ok) void api.resume?.().catch(() => undefined);
          return;
        }
        if (!result.ok) {
          onError(shortcutErrorMessage(result.error) || "O detector não pausou; a captura não começou.");
          return;
        }
      } catch {
        onError("O detector de atalho não respondeu; a captura não começou.");
        return;
      }
      pausedRef.current = true;
    }
    pressedRef.current = new Set();
    setCapturing(true);
  };

  const cancel = () => {
    // Fechamento único (igual a Escape/blur): larga as teclas primeiro; o detector só retoma
    // quando `pressedRef` fica vazio — nunca fica pausado com uma tecla fisicamente em baixo.
    abandon();
    resumeWhenKeysFree();
  };

  return (
    <span className="lunmi-keyrecorder" data-testid="key-recorder" data-capturing={capturing ? "true" : "false"}>
      {capturing ? (
        <span className="lunmi-keyrecorder-live" data-testid="key-recorder-keys" role="status" aria-live="polite">
          {keys.length ? keys.join(" + ") : "Premindo as teclas…"}
        </span>
      ) : (
        <button type="button" data-testid="key-recorder-change" className="lunmi-voice-button" disabled={disabled} onClick={() => void begin()}>
          Alterar
        </button>
      )}
      {capturing && (
        <button type="button" data-testid="key-recorder-cancel" className="lunmi-voice-button" onClick={cancel}>
          Cancelar
        </button>
      )}
    </span>
  );
}
