"""The Spotify part of a task: search, choose, play and verify (moved out of controller.py, 01/11).

A base class of `TaskController`: the methods are unchanged and still use its state.
"""
from __future__ import annotations

from app.core.automatic import SPOTIFY_MUSIC_OPERATIONS, allows_local_activation, allows_spotify_effect, allows_spotify_effects, allows_spotify_search, spotify_tool_mutates
from typing import Any, Awaitable, Callable
import asyncio
import threading
import uuid

from app.core.result import TaskResult


class SpotifyFlow:
    # State owned and set by TaskController.__init__; declared here only for type checking.
    spotify: Any
    session_id: str
    confirm: Callable[[str], Awaitable[bool]] | None
    choose: Callable[[str, list[dict[str, Any]]], Awaitable[int | None]] | None
    _spotify_refs: dict[str, dict[str, str]]
    _spotify_epoch: tuple[int | None, int | None] | None
    _spotify_session: str
    _spotify_search_id: str

    @staticmethod
    def _spotify_installer(spotify: Any) -> Any:
        return getattr(spotify, "installer", None) or spotify

    @staticmethod
    def _spotify_broker(spotify: Any) -> Any:
        return getattr(spotify, "broker", None) or spotify

    def _spotify_identity(self, spotify: Any) -> tuple[int | None, int | None]:
        installer = self._spotify_installer(spotify)
        epoch = getattr(installer, "epoch", None)
        if callable(epoch):
            epoch = epoch()
        if epoch is None:
            try:
                epoch = installer.state()["epoch"]
            except Exception:
                epoch = None
        broker_epoch = getattr(self._spotify_broker(spotify), "epoch", None)
        if callable(broker_epoch):
            broker_epoch = broker_epoch()
        return (int(epoch) if isinstance(epoch, int) else None,
                int(broker_epoch) if isinstance(broker_epoch, int) else None)

    @staticmethod
    def _spotify_clean_text(value: Any, limit: int = 300) -> str:
        """Texto público de uma faixa: uma linha, sem control chars, tamanho limitado."""
        return " ".join(str(value or "").split())[:limit]

    def _spotify_track_text(self, state: dict[str, Any] | None) -> str:
        if not isinstance(state, dict) or not state.get("title"):
            return "nada em reprodução"
        title = self._spotify_clean_text(state.get("title"))
        artist = self._spotify_clean_text(state.get("artist"), 200)
        return f"«{title}»" + (f" — {artist}" if artist else "")

    @staticmethod
    def _spotify_track_uri(value: Any) -> bool:
        """O backend só aceita o esquema de faixa; nunca URL/path/URI do modelo."""
        return bool(isinstance(value, str) and value.startswith("spotify:track:")
                    and len(value) <= 200
                    and not any(ord(char) < 32 or ord(char) == 127 for char in value))

    @staticmethod
    def _spotify_verified(result: Any) -> bool:
        """ACK/file presence is never proof; only an observed live result is."""
        status = result.get("status") if isinstance(result, dict) else getattr(result, "status", "")
        evidence = result.get("evidence") if isinstance(result, dict) else getattr(result, "evidence", None)
        return bool(status == "SUCCESS_VERIFIED" and isinstance(evidence, dict)
                    and evidence.get("observed") is True)

    async def _run_spotify_tool(self, operation: dict[str, Any], instruction: str,
                                result: TaskResult,
                                operation_event: Callable[[str, dict[str, Any]], None]) -> TaskResult:
        """Closed Spotify tool: refs, readiness, intent and one-shot activation.

        This path never falls through to UIA. A missing connector, an unknown/remote
        device or a timeout is reported honestly and the command is not repeated.
        """
        args = dict(operation.get("args") or {})
        tool_name = str(operation.get("name") or "")
        name = str(args.get("operation") or
                   (tool_name.split(".", 1)[1] if tool_name.startswith("spotify.") else ""))
        mutation = spotify_tool_mutates(name)
        op = {"operation_id": "op-" + uuid.uuid4().hex,
              "tool": "spotify." + name,
              "category": "mutation" if mutation else "read_only"}
        result.scope = "end_to_end"
        spotify = self.spotify
        installer = self._spotify_installer(spotify)
        broker = self._spotify_broker(spotify)
        cancel_event = threading.Event()
        notified = False

        def refuse(message: str, *, status: str = "FAILED", kind: str = "AUTHORIZATION_REFUSED") -> TaskResult:
            # Uma recusa antes do dispatch não anuncia uma operação iniciada.
            if notified:
                operation_event("operation.finished", {**op, "state": "failed_no_effect",
                                                      "verified": False, "failure_reason": "tool_error"})
            result.status = status
            result.failure_kind = kind
            result.message = message
            return result

        if name not in SPOTIFY_MUSIC_OPERATIONS:
            return refuse("Operação Spotify inválida; nada foi executado.")
        extras = set(args) - {"operation"}
        if name == "search":
            query = args.get("query")
            if extras != {"query"} or not isinstance(query, str) or not 1 <= len(query) <= 200 \
                    or not query.strip() or any(ord(char) < 32 or ord(char) == 127 for char in query):
                return refuse("Pesquisa Spotify inválida; nada foi executado.")
        elif name == "play":
            ref, query = args.get("ref"), args.get("query")
            if extras - {"ref", "query"} or bool(query) == bool(ref):
                return refuse("Pedido de reprodução inválido; nada foi executado.")
            value = query if query else ref
            if not isinstance(value, str) or not 1 <= len(value) <= 200 \
                    or any(ord(char) < 32 or ord(char) == 127 for char in value):
                return refuse("Pedido de reprodução inválido; nada foi executado.")
        elif name == "volume":
            level = args.get("level")
            if extras != {"level"} or isinstance(level, bool) \
                    or not isinstance(level, (int, float)) or not 0.0 <= float(level) <= 1.0:
                return refuse("Volume Spotify inválido; nada foi executado.")
        elif extras:
            return refuse("Operação Spotify inválida; nada foi executado.")
        if spotify is None or installer is None or broker is None:
            return refuse("O Spotify não está configurado; nenhuma ação foi executada.",
                          kind="CONFIGURATION")
        if getattr(installer, "job_running", False):
            return refuse("A configuração do Spotify está em curso; a tarefa foi adiada sem alterar a configuração.",
                          kind="CONFIGURATION")
        if name in {"play", "pause", "resume", "volume", "next", "previous"} \
                and not allows_spotify_effect(instruction, name):
            return refuse("O pedido não autoriza efeitos musicais; nada foi alterado.")
        if name == "search" and not (allows_spotify_search(instruction)
                                      or allows_spotify_effects(instruction)):
            return refuse("O pedido não autoriza uma pesquisa musical; nada foi executado.")
        if name == "activate_local" and not allows_local_activation(instruction):
            return refuse("Ativar este computador exige um pedido humano explícito; nada foi alterado.")

        result.plan_source = "spotify"
        operation_event("operation.started", {**op, "state": "started", "verified": False})
        notified = True

        def prepare(open_spotify: bool) -> None:
            prep = getattr(installer, "prepare_music", None)
            if callable(prep):
                prep(open_spotify=open_spotify, timeout=12.0, cancelled=cancel_event)
                return
            start = getattr(broker, "start", None)
            if callable(start):
                start()

        try:
            await asyncio.to_thread(prepare, name in {"search", "play"})
        except asyncio.CancelledError:
            cancel_event.set()
            result.status = "CANCELLED"
            result.failure_kind = "CANCELLED"
            result.message = "Pedido Spotify cancelado antes de qualquer efeito."
            return result
        except Exception as exc:
            code = str(getattr(exc, "code", "unavailable"))
            if code == "job_cancelled":
                return refuse("Pedido Spotify cancelado antes de qualquer efeito.",
                              status="CANCELLED", kind="CANCELLED")
            if code == "not_configured":
                return refuse("O Spotify não está configurado. Configura em Apps > Spotify; nada foi executado.",
                              kind="CONFIGURATION")
            return refuse("O Spotify não está pronto; o comando não foi repetido.",
                          status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")

        # A readiness pode arrancar o host e incrementar o epoch; a identidade é lida
        # depois dela para que a pesquisa recém-feita continue válida no passo seguinte.
        identity = self._spotify_identity(spotify)
        if identity != self._spotify_epoch or self._spotify_session != self.session_id:
            self._spotify_refs.clear()
            self._spotify_search_id = ""
            self._spotify_epoch = identity
        self._spotify_session = self.session_id
        epoch, broker_epoch = identity

        def stays_current() -> bool:
            """Readiness/confirmação podem mudar epoch; refs e grants não sobrevivem."""
            return self._spotify_identity(spotify) == identity

        try:
            if name in {"play", "pause", "resume", "volume", "next", "previous",
                        "activate_local"} and not stays_current():
                return refuse("A ligação Spotify mudou entretanto; nada foi alterado.",
                              status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
            if name == "state":
                action = await asyncio.to_thread(broker.player_state)
            elif name == "search":
                action = await asyncio.to_thread(broker.search_tracks, str(args["query"]), 10)
            elif name == "volume":
                action = await asyncio.to_thread(broker.set_volume, float(args["level"]))
            elif name == "pause":
                action = await asyncio.to_thread(broker.pause)
            elif name == "resume":
                action = await asyncio.to_thread(broker.play)
            elif name == "next":
                action = await asyncio.to_thread(broker.next_track)
            elif name == "previous":
                action = await asyncio.to_thread(broker.previous_track)
            elif name == "play":
                chosen = self._spotify_resolve_play(args)
                if chosen is None and args.get("ref"):
                    return refuse("A referência Spotify não existe ou expirou; pesquisa de novo. "
                                  "Nada foi reproduzido.")
                if chosen is None:
                    return await self._spotify_choose_track(args, broker, result, op, operation_event,
                                                           refuse, identity, stays_current)
                if not stays_current():
                    return refuse("A ligação Spotify mudou entretanto; nada foi reproduzido.",
                                  status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
                action = await asyncio.to_thread(broker.play_uri, chosen["uri"])
            elif name == "activate_local":
                grant_method = getattr(broker, "grant_local_activation", None)
                if not callable(grant_method):
                    return refuse("A ativação local não está disponível; nada foi alterado.",
                                  status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
                if self.confirm is None:
                    return refuse("A confirmação manual não está disponível; nada foi alterado.",
                                  status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
                allowed = await self.confirm(
                    "Usar este computador pode transferir a reprodução de outro dispositivo "
                    "para aqui. Continuar?")
                if not allowed:
                    return refuse("Ativação local não autorizada; nada foi alterado.",
                                  status="CANCELLED", kind="CANCELLED")
                if not stays_current():
                    return refuse("A ligação Spotify mudou durante a confirmação; nada foi alterado.",
                                  status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
                from app.integrations.spotify import random_request_token
                grant = grant_method(request_id=random_request_token(), task_id=result.task_id,
                                     epoch=(broker_epoch if broker_epoch is not None else (epoch or 0)))
                consume = getattr(grant, "consume", None)
                if callable(consume) and not consume():
                    return refuse("A autorização de ativação expirou; nada foi alterado.",
                                  status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
                action = await asyncio.to_thread(broker.activate_local, grant)
            else:
                return refuse("Operação Spotify inválida; nada foi executado.")
        except asyncio.CancelledError:
            cancel_event.set()
            if mutation:
                # O comando pode já ter saído: cancelar não o repete nem o declara
                # concluído. Só uma leitura interrompida é cancelamento sem efeito.
                operation_event("operation.finished", {**op, "state": "uncertain",
                                                      "verified": False,
                                                      "failure_reason": "dispatch_uncertain"})
                result.status = "NOT_VERIFIED"
                result.failure_kind = "VERIFICATION_FAILURE"
                result.message = ("Pedido Spotify cancelado durante o envio; o resultado é "
                                  "incerto e não repeti o comando.")
            else:
                result.status = "CANCELLED"
                result.failure_kind = "CANCELLED"
                result.message = "Pedido Spotify cancelado; não foi repetido."
            return result
        except Exception as exc:
            code = str(getattr(exc, "code", "unavailable"))
            uncertain = code in {"interrupted", "unavailable"}
            operation_event("operation.finished", {**op,
                "state": "uncertain" if uncertain else "failed_no_effect",
                "verified": False,
                "failure_reason": "dispatch_uncertain" if uncertain else "tool_error"})
            result.status = "NOT_VERIFIED" if uncertain else "FAILED"
            result.failure_kind = "VERIFICATION_FAILURE" if uncertain else "ACTION_FAILURE"
            result.message = ("O resultado do Spotify é incerto. Não repeti o comando."
                              if uncertain else
                              "O Spotify não concluiu o pedido; nada foi repetido.")
            return result

        if name == "search":
            tracks = (action.data or {}).get("tracks") if isinstance(action.data, dict) else None
            if isinstance(tracks, list):
                tracks = tracks[:10]
            if not stays_current():
                return refuse("A ligação Spotify mudou durante a pesquisa; nada foi guardado.",
                              status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
            if not self._spotify_verified(action) or not isinstance(tracks, list):
                operation_event("operation.finished", {**op, "state": "completed_unverified",
                                                      "verified": False})
                result.status = "NOT_VERIFIED"
                result.failure_kind = "VERIFICATION_FAILURE"
                result.message = "O Spotify não devolveu uma pesquisa observada; nada foi alterado."
                return result
            self._spotify_refs = {}
            self._spotify_search_id = "srch-" + uuid.uuid4().hex[:16]
            self._spotify_session = self.session_id
            for track in tracks:
                if not isinstance(track, dict) or not self._spotify_track_uri(track.get("uri")):
                    continue
                ref = "sref-" + uuid.uuid4().hex[:16]
                self._spotify_refs[ref] = {"uri": track["uri"],
                                           "title": self._spotify_clean_text(track.get("title")),
                                           "artist": self._spotify_clean_text(track.get("artist"), 200),
                                           "album": self._spotify_clean_text(track.get("album"), 200)}
            if not self._spotify_refs:
                if tracks:
                    result.status = "NOT_VERIFIED"
                    result.failure_kind = "VERIFICATION_FAILURE"
                    result.message = "A pesquisa não devolveu faixas verificadas; nada foi alterado."
                    return result
                # Uma pesquisa vazia é um resultado observado, não uma falha de prova;
                # e nunca prova ausência no catálogo.
                result.status = "SUCCESS"
                result.message = "Nenhuma faixa encontrada no Spotify."
                result.evidence = [{"operation": "spotify.search", "verified": True,
                                    "read_only": True, "accepted": False}]
                operation_event("operation.finished", {**op, "state": "completed_verified",
                                                      "verified": True})
                return result
            lines = [f"{index + 1}. {track['title']} — {track['artist']} [{ref}]"
                     for index, (ref, track) in enumerate(self._spotify_refs.items())]
            result.status = "SUCCESS"
            result.message = "Resultados de pesquisa Spotify:\n" + "\n".join(lines)
            result.evidence = [{"operation": "spotify.search", "verified": True,
                                "read_only": True, "accepted": False}]
            operation_event("operation.finished", {**op, "state": "completed_verified", "verified": True})
            return result

        verified = self._spotify_verified(action)
        data = (action.get("data") if isinstance(action, dict) else action.data)
        data = data if isinstance(data, dict) else (action if isinstance(action, dict) else {})
        if not verified:
            operation_event("operation.finished", {**op, "state": "completed_unverified",
                                                  "verified": False})
            result.status = "NOT_VERIFIED"
            result.failure_kind = "VERIFICATION_FAILURE"
            result.message = "O Spotify não confirmou o resultado observado; o comando não foi repetido."
            return result
        if name == "state":
            playing = "a tocar" if data.get("is_playing") else "em pausa"
            volume = data.get("volume")
            volume_text = f" Volume: {round(float(volume) * 100)}%." if isinstance(volume, (int, float)) else ""
            result.message = f"Spotify {playing}: {self._spotify_track_text(data)}.{volume_text}"
        elif name == "activate_local":
            result.message = "Reprodução neste computador confirmada pelo Spotify."
        elif name == "volume":
            level = data.get("volume")
            result.message = (f"Volume Spotify: {round(float(level) * 100)}%."
                              if isinstance(level, (int, float)) else "Volume Spotify alterado.")
        elif name == "pause":
            result.message = f"Spotify em pausa: {self._spotify_track_text(data)}."
        elif name in {"play", "resume", "next", "previous"}:
            result.message = f"Spotify a tocar: {self._spotify_track_text(data)}."
        else:
            result.message = "Pedido Spotify concluído."
        result.status = "SUCCESS"
        if mutation:
            result.actions += 1
            result.actions_executed += 1
        result.evidence = [{"operation": "spotify." + name, "verified": True,
                            "read_only": not mutation, "accepted": mutation}]
        operation_event("operation.finished", {**op, "state": "completed_verified", "verified": True})
        return result

    def _spotify_resolve_play(self, args: dict[str, Any]) -> dict[str, str] | None:
        ref = args.get("ref")
        if isinstance(ref, str) and ref:
            return self._spotify_refs.get(ref)
        return None

    async def _spotify_choose_track(self, args: dict[str, Any], broker: Any, result: TaskResult,
                                    op: dict[str, Any],
                                    operation_event: Callable[[str, dict[str, Any]], None],
                                    refuse: Callable[..., TaskResult],
                                    identity: tuple[int | None, int | None],
                                    stays_current: Callable[[], bool]) -> TaskResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return refuse("O pedido de reprodução não indicou uma faixa; nada foi alterado.")
        try:
            found = await asyncio.to_thread(broker.search_tracks, query, 10)
        except Exception:
            return refuse("A pesquisa Spotify falhou; nada foi reproduzido.",
                          status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
        tracks = (found.data or {}).get("tracks") if isinstance(found.data, dict) else None
        if isinstance(tracks, list):
            tracks = tracks[:10]
        if not self._spotify_verified(found) or not isinstance(tracks, list) or not tracks:
            return refuse("Nenhuma faixa verificada foi encontrada; nada foi reproduzido.",
                          status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
        self._spotify_refs = {}
        self._spotify_search_id = "srch-" + uuid.uuid4().hex[:16]
        self._spotify_session = self.session_id
        candidates: list[dict[str, Any]] = []
        for track in tracks:
            if not isinstance(track, dict) or not self._spotify_track_uri(track.get("uri")):
                continue
            ref = "sref-" + uuid.uuid4().hex[:16]
            title = self._spotify_clean_text(track.get("title"))
            artist = self._spotify_clean_text(track.get("artist"), 200)
            album = self._spotify_clean_text(track.get("album"), 200)
            self._spotify_refs[ref] = {"uri": track["uri"], "title": title,
                                       "artist": artist, "album": album}
            detail = " — ".join(part for part in (artist, album) if part)
            candidates.append({"name": title, "publisher": detail, "detail": detail,
                               "ref": ref})
        if not candidates:
            return refuse("A pesquisa não devolveu faixas utilizáveis; nada foi reproduzido.",
                          status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
        chosen = candidates[0]
        if len(candidates) > 1:
            if self.choose is None:
                return refuse("Há várias faixas correspondentes; especifica qual queres ouvir.",
                              status="CANCELLED", kind="CANCELLED")
            position = await self.choose("Escolha a faixa do Spotify", [
                {key: item[key] for key in ("name", "publisher", "detail")}
                for item in candidates])
            try:
                index = int(position) if position is not None else -1
            except (TypeError, ValueError):
                index = -1
            if not 0 <= index < len(candidates):
                return refuse("Nenhuma faixa escolhida; nada foi reproduzido.",
                              status="CANCELLED", kind="CANCELLED")
            chosen = candidates[index]
        if not stays_current():
            return refuse("A ligação Spotify mudou durante a escolha; nada foi reproduzido.",
                          status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
        selected = self._spotify_refs.get(str(chosen.get("ref")))
        if selected is None:
            return refuse("A seleção expirou; pesquisa de novo. Nada foi reproduzido.")
        try:
            action = await asyncio.to_thread(broker.play_uri, selected["uri"])
        except Exception:
            return refuse("O Spotify não confirmou a reprodução; o comando não foi repetido.",
                          status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
        if not self._spotify_verified(action):
            return refuse("O Spotify não confirmou a reprodução observada; nada foi repetido.",
                          status="NOT_VERIFIED", kind="VERIFICATION_FAILURE")
        data = action.data if isinstance(action.data, dict) else selected
        result.status = "SUCCESS"
        result.message = f"Spotify a tocar: {self._spotify_track_text(data)}."
        result.actions += 1
        result.actions_executed += 1
        result.evidence = [{"operation": "spotify.play", "verified": True,
                            "read_only": False, "accepted": True}]
        operation_event("operation.finished", {**op, "state": "completed_verified", "verified": True})
        return result
