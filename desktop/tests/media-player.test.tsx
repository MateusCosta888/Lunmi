/**
 * Testes do mini leitor do Node (`desktop/tests/media-player.test.tsx`).
 *
 * Nenhum Windows real: `window.lunmiPresence.media` é uma ponte espiã. Provas:
 * - sem API, sem `available` ou sem `session_id` não há leitor nenhum (nada inventado);
 * - o estado mostrado é sempre o do main; um clique não muda o ícone antes da resposta;
 * - os botões respeitam as capacidades publicadas e os comandos usam o `session_id` mostrado;
 * - erro de comando aparece uma vez e NÃO é repetido automaticamente;
 * - o polling é finito e sem sobreposição: só com `active`, uma leitura de cada vez, e para ao
 *   desativar/desmontar;
 * - sem capa/thumbnail/URL: o leitor não contém imagens.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { MediaPlayer } from "../src/features/companion/MediaPlayer";
import { MEDIA_POLL_MS, useNodeMedia } from "../src/features/companion/useNodeMedia";
import type { MediaCommandResultDto, MediaStateDto } from "../src/shared/media";

function mediaState(overrides: Partial<MediaStateDto> = {}): MediaStateDto {
  return {
    v: 1, available: true, session_id: "ms-1", title: "Paper Sky", artist: "Lumen Coast",
    source_app: "Spotify", playback: "playing", can_play: true, can_pause: true,
    can_previous: true, can_next: true, ...overrides,
  };
}

type MediaHarness = {
  get: ReturnType<typeof vi.fn>;
  command: ReturnType<typeof vi.fn>;
};

function installMedia(overrides: { get?: () => Promise<MediaStateDto>; command?: (input: unknown) => Promise<MediaCommandResultDto> } = {}): MediaHarness {
  const harness: MediaHarness = {
    get: vi.fn(overrides.get ?? (async () => mediaState())),
    command: vi.fn(overrides.command ?? (async () => ({ ok: true, state: mediaState({ playback: "paused" }) }))),
  };
  (window as unknown as { lunmiPresence: unknown }).lunmiPresence = { media: harness };
  return harness;
}

async function flush(): Promise<void> {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

let hidden = false;

beforeEach(() => {
  vi.useFakeTimers();
  hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
});

describe("MediaPlayer — lifecycle and concurrency regressions", () => {
  it("uses a synchronous lock for two commands from the same render", async () => {
    const pending = deferred<MediaCommandResultDto>();
    const harness = installMedia({ command: () => pending.promise });
    const hook = renderHook(() => useNodeMedia(true));
    await flush();
    const command = hook.result.current.command;
    act(() => { command("pause"); command("next"); });
    expect(harness.command).toHaveBeenCalledTimes(1);
    pending.resolve({ ok: true, state: mediaState({ playback: "paused" }) });
    await flush();
  });

  it("cannot restore media or an error after the player is deactivated", async () => {
    const pending = deferred<MediaCommandResultDto>();
    installMedia({ command: () => pending.promise });
    const hook = renderHook(({ active }) => useNodeMedia(active), { initialProps: { active: true } });
    await flush();
    act(() => hook.result.current.command("pause"));
    hook.rerender({ active: false });
    pending.resolve({ ok: false, error: "stale_session", state: mediaState({ title: "Old response" }) });
    await flush();
    expect(hook.result.current.state).toBeNull();
    expect(hook.result.current.busy).toBeNull();
    expect(hook.result.current.error).toBeNull();
  });

  it("does not apply a previous activation's command response to a reopened player", async () => {
    const pending = deferred<MediaCommandResultDto>();
    let reads = 0;
    const harness = installMedia({
      get: async () => mediaState(++reads === 1 ? {} : { title: "New session", session_id: "ms-2" }),
      command: () => pending.promise,
    });
    const hook = renderHook(({ active }) => useNodeMedia(active), { initialProps: { active: true } });
    await flush();
    act(() => hook.result.current.command("pause"));
    hook.rerender({ active: false });
    hook.rerender({ active: true });
    await flush();
    pending.resolve({ ok: true, state: mediaState({ title: "Old session" }) });
    await flush();
    expect(hook.result.current.state?.title).toBe("New session");
    expect(hook.result.current.state?.session_id).toBe("ms-2");
    expect(hook.result.current.busy).toBeNull();
    expect(harness.command).toHaveBeenCalledTimes(1);
  });

  it("does not poll while a command is unresolved", async () => {
    const pending = deferred<MediaCommandResultDto>();
    const harness = installMedia({ command: () => pending.promise });
    render(<MediaPlayer active />);
    await flush();
    fireEvent.click(screen.getByTestId("mini-media-next"));
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS * 3); await Promise.resolve(); });
    expect(harness.get).toHaveBeenCalledTimes(1);
    expect(harness.command).toHaveBeenCalledTimes(1);
    pending.resolve({ ok: true, state: mediaState() });
    await flush();
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS); await Promise.resolve(); });
    expect(harness.get).toHaveBeenCalledTimes(2);
  });

  it("does not rearm a hidden-document timer when an outstanding read settles", async () => {
    const pending = deferred<MediaStateDto>();
    const harness = installMedia({ get: () => pending.promise });
    render(<MediaPlayer active />);
    await flush();
    act(() => { hidden = true; document.dispatchEvent(new Event("visibilitychange")); });
    pending.resolve(mediaState());
    await flush();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS * 3); await Promise.resolve(); });
    expect(harness.get).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("removes old actionable state when a later read fails", async () => {
    const harness = installMedia();
    render(<MediaPlayer active />);
    await flush();
    expect(screen.getByTestId("mini-media-title").textContent).toBe("Paper Sky");
    harness.get.mockRejectedValueOnce(new Error("private helper details"));
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS); await Promise.resolve(); await Promise.resolve(); });
    expect(screen.queryByTestId("mini-media")).toBeNull();
    expect(screen.queryByTestId("mini-media-toggle")).toBeNull();
    expect(screen.getByTestId("mini-media-error").textContent).toBe("Não foi possível consultar o leitor.");
  });

  it("applies a failed command's real state and translates its error without exposing raw codes", async () => {
    const harness = installMedia({ command: async () => ({ ok: false, error: "stale_session",
      state: mediaState({ available: false, session_id: null }) }) });
    render(<MediaPlayer active />);
    await flush();
    fireEvent.click(screen.getByTestId("mini-media-next"));
    await flush();
    expect(screen.queryByTestId("mini-media")).toBeNull();
    expect(screen.queryByTestId("mini-media-next")).toBeNull();
    expect(screen.getByTestId("mini-media-error").textContent).toBe("A sessão do leitor mudou.");
    expect(harness.command).toHaveBeenCalledTimes(1);
  });

  it("updates to an available replacement session even when the command was rejected", async () => {
    const harness = installMedia({ command: async () => ({ ok: false, error: "not_supported",
      state: mediaState({ session_id: "ms-2", title: "Replacement session", playback: "paused" }) }) });
    render(<MediaPlayer active />);
    await flush();
    fireEvent.click(screen.getByTestId("mini-media-next"));
    await flush();
    expect(screen.getByTestId("mini-media-title").textContent).toBe("Replacement session");
    expect(screen.getByTestId("mini-media-toggle").getAttribute("aria-label")).toBe("Reproduzir");
    expect(screen.getByTestId("mini-media-error").textContent).toBe("Este controlo não está disponível.");
    expect(harness.command).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("mini-media-toggle"));
    await flush();
    expect(harness.command.mock.calls[1][0]).toEqual({ command: "play", session_id: "ms-2" });
  });

  it("uses a safe Portuguese fallback for unknown or prototype-shaped error codes", async () => {
    const harness = installMedia({ command: async () => ({ ok: false, error: "__proto__", state: mediaState() }) });
    render(<MediaPlayer active />);
    await flush();
    fireEvent.click(screen.getByTestId("mini-media-next"));
    await flush();
    expect(screen.getByTestId("mini-media-error").textContent).toBe("Não foi possível controlar o leitor.");
    expect(harness.command).toHaveBeenCalledTimes(1);
  });

  it("blocks commands during an outstanding polling read and uses its fresh session afterwards", async () => {
    const pending = deferred<MediaStateDto>();
    const harness = installMedia();
    const hook = renderHook(() => useNodeMedia(true));
    await flush();
    harness.get.mockReturnValueOnce(pending.promise);
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS); await Promise.resolve(); });
    act(() => hook.result.current.command("pause"));
    expect(harness.command).not.toHaveBeenCalled();
    expect(hook.result.current.loading).toBe(true);
    pending.resolve(mediaState({ session_id: "ms-2" }));
    await flush();
    act(() => hook.result.current.command("pause"));
    await flush();
    expect(harness.command).toHaveBeenCalledExactlyOnceWith({ command: "pause", session_id: "ms-2" });
  });

  it("does not read or schedule while mounted hidden, and resumes on visibility", async () => {
    hidden = true;
    const harness = installMedia();
    render(<MediaPlayer active />);
    await flush();
    expect(harness.get).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    act(() => { hidden = false; document.dispatchEvent(new Event("visibilitychange")); });
    await flush();
    expect(harness.get).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("mini-media-title").textContent).toBe("Paper Sky");
  });

  it("invalidates command responses across document hiding and reshowing", async () => {
    const pending = deferred<MediaCommandResultDto>();
    let reads = 0;
    installMedia({ get: async () => mediaState(++reads === 1 ? {} : { title: "Visible session", session_id: "ms-2" }),
      command: () => pending.promise });
    const hook = renderHook(() => useNodeMedia(true));
    await flush();
    act(() => hook.result.current.command("pause"));
    act(() => { hidden = true; document.dispatchEvent(new Event("visibilitychange")); });
    act(() => { hidden = false; document.dispatchEvent(new Event("visibilitychange")); });
    pending.resolve({ ok: false, error: "stale_session", state: mediaState({ title: "Old command" }) });
    await flush();
    expect(hook.result.current.state?.title).toBe("Visible session");
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.busy).toBeNull();
  });

  it("waits for an old read to settle without applying it or overlapping a new activation's read", async () => {
    const pending = deferred<MediaStateDto>();
    const harness = installMedia();
    harness.get.mockReturnValueOnce(pending.promise);
    const hook = renderHook(({ active }) => useNodeMedia(active), { initialProps: { active: true } });
    await flush();
    hook.rerender({ active: false });
    hook.rerender({ active: true });
    await flush();
    expect(harness.get).toHaveBeenCalledTimes(1);
    expect(hook.result.current.state).toBeNull();
    pending.resolve(mediaState({ title: "Old read" }));
    await flush();
    expect(harness.get).toHaveBeenCalledTimes(2);
    expect(hook.result.current.state?.title).toBe("Paper Sky");
  });

  it("does not poll or apply a command result after unmounting", async () => {
    const pending = deferred<MediaCommandResultDto>();
    const harness = installMedia({ command: () => pending.promise });
    const hook = renderHook(() => useNodeMedia(true));
    await flush();
    act(() => hook.result.current.command("pause"));
    hook.unmount();
    pending.resolve({ ok: true, state: mediaState({ playback: "paused" }) });
    await flush();
    expect(vi.getTimerCount()).toBe(0);
    expect(harness.get).toHaveBeenCalledTimes(1);
    expect(harness.command).toHaveBeenCalledTimes(1);
  });

  it("recovers a fresh read after a thrown command error without retrying that command", async () => {
    const harness = installMedia({ command: async () => { throw new Error("private transport details"); } });
    render(<MediaPlayer active />);
    await flush();
    fireEvent.click(screen.getByTestId("mini-media-next"));
    await flush();
    expect(screen.queryByTestId("mini-media-toggle")).toBeNull();
    expect(screen.getByTestId("mini-media-error").textContent).toBe("O leitor não respondeu.");
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS); await Promise.resolve(); await Promise.resolve(); });
    expect(screen.queryByTestId("mini-media-error")).toBeNull();
    expect(screen.getByTestId("mini-media-toggle")).toBeTruthy();
    expect(harness.command).toHaveBeenCalledTimes(1);
    expect(harness.get).toHaveBeenCalledTimes(2);
  });
});

afterEach(() => {
  vi.useRealTimers();
  delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
});

describe("MediaPlayer — estado real do sistema", () => {
  it("sem API de media: nada é renderizado nem pedido", async () => {
    render(<MediaPlayer active />);
    await flush();
    expect(screen.queryByTestId("mini-media")).toBeNull();
  });

  it("available=false ou session_id nulo: sem leitor (nada inventado)", async () => {
    installMedia({ get: async () => mediaState({ available: false }) });
    const view = render(<MediaPlayer active />);
    await flush();
    expect(view.queryByTestId("mini-media")).toBeNull();
    view.unmount();

    installMedia({ get: async () => mediaState({ session_id: null }) });
    const second = render(<MediaPlayer active />);
    await flush();
    expect(second.queryByTestId("mini-media")).toBeNull();
  });

  it("mostra metadados reais e respeita as capacidades; nenhuma imagem/URL", async () => {
    installMedia({ get: async () => mediaState({ can_previous: false, can_next: false, can_pause: false }) });
    const view = render(<MediaPlayer active />);
    await flush();
    expect(screen.getByTestId("mini-media-title").textContent).toBe("Paper Sky");
    expect(screen.getByTestId("mini-media-sub").textContent).toContain("Lumen Coast · Spotify · Em reprodução");
    expect((screen.getByTestId("mini-media-prev") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("mini-media-next") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("mini-media-toggle") as HTMLButtonElement).disabled).toBe(true); // can_pause=false
    expect(view.container.querySelectorAll("img")).toHaveLength(0);
  });

  it("play/pause usa o session_id mostrado e o ícone só muda com a resposta do main", async () => {
    let resolve!: (value: MediaCommandResultDto) => void;
    const harness = installMedia({ command: () => new Promise<MediaCommandResultDto>(res => { resolve = res; }) });
    render(<MediaPlayer active />);
    await flush();
    const toggle = screen.getByTestId("mini-media-toggle") as HTMLButtonElement;
    expect(toggle.getAttribute("aria-label")).toBe("Pausar");
    fireEvent.click(toggle);
    await flush();
    expect(harness.command).toHaveBeenCalledTimes(1);
    expect(harness.command.mock.calls[0][0]).toEqual({ command: "pause", session_id: "ms-1" });
    expect(screen.getByTestId("mini-media-toggle").getAttribute("aria-label")).toBe("Pausar"); // sem otimismo
    expect((screen.getByTestId("mini-media-toggle") as HTMLButtonElement).disabled).toBe(true); // em voo
    resolve({ ok: true, state: mediaState({ playback: "paused" }) });
    await flush();
    expect(screen.getByTestId("mini-media-toggle").getAttribute("aria-label")).toBe("Reproduzir");
    expect((screen.getByTestId("mini-media-toggle") as HTMLButtonElement).disabled).toBe(false);
  });

  it("erro do comando aparece uma vez e não é repetido automaticamente", async () => {
    const harness = installMedia({
      command: async () => ({ ok: false, error: "stale_session", state: mediaState({ session_id: null, available: false }) }),
    });
    render(<MediaPlayer active />);
    await flush();
    fireEvent.click(screen.getByTestId("mini-media-next"));
    await flush();
    expect(harness.command).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("mini-media-error").textContent).toBe("A sessão do leitor mudou.");
    // A resposta real invalidou a sessão: o erro fica visível sem os controlos antigos.
    expect(screen.queryByTestId("mini-media-title")).toBeNull();
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS * 2); await Promise.resolve(); });
    expect(harness.command).toHaveBeenCalledTimes(1); // nunca repetido sozinho
  });

  it("polling finito e sem sobreposição: só com active, uma leitura de cada vez", async () => {
    let resolveFirst!: (value: MediaStateDto) => void;
    const get = vi.fn()
      .mockImplementationOnce(() => new Promise<MediaStateDto>(res => { resolveFirst = res; }))
      .mockImplementation(async () => mediaState());
    installMedia({ get });
    const view = render(<MediaPlayer active />);
    await flush();
    expect(get).toHaveBeenCalledTimes(1);

    // A primeira leitura ainda está em voo: o timer não pode lançar uma segunda.
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS); await Promise.resolve(); });
    expect(get).toHaveBeenCalledTimes(1);

    resolveFirst(mediaState());
    await flush();
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS); await Promise.resolve(); await Promise.resolve(); });
    expect(get).toHaveBeenCalledTimes(2);

    view.unmount();
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS * 3); await Promise.resolve(); });
    expect(get).toHaveBeenCalledTimes(2); // desmontado: nada continua
  });

  it("inativo não consulta nada; reativar volta a ler", async () => {
    const harness = installMedia();
    const view = render(<MediaPlayer active={false} />);
    await act(async () => { vi.advanceTimersByTime(MEDIA_POLL_MS); await Promise.resolve(); });
    expect(harness.get).toHaveBeenCalledTimes(0);
    expect(view.queryByTestId("mini-media")).toBeNull();

    view.rerender(<MediaPlayer active />);
    await flush();
    expect(harness.get).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("mini-media")).toBeTruthy();
  });
});
