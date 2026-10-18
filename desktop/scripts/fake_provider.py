"""Provedor FAKE de loopback para o aceite do pacote (rotulado; sem inferência real).

Serve o mínimo compatível com OpenAI que a Lunmi usa para um provider `custom`:
`GET /v1/models` para a descoberta e `POST /v1/chat/completions` para uma resposta. Não chama
nenhum serviço externo, não gasta cota e responde sempre com um texto que se identifica como
fake — o aceite nunca deve ser confundido com uma prova de provedor real.

Uso:  python desktop/scripts/fake_provider.py --port 8791
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "fake-alpha"
MARCA = "Resposta do provedor FAKE local — sem inferência real."


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - nome exigido por BaseHTTPRequestHandler
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {
                "object": "list",
                "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "fake"}],
            })
            return
        self._json(200, {"status": "fake", "model": MODEL_ID})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.rstrip("/").endswith("/chat/completions"):
            self._json(200, {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 0,
                "model": MODEL_ID,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": MARCA},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
            return
        self._json(404, {"error": "rota fake inexistente"})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[fake] " + fmt % args + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Provedor fake de loopback para aceite")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[fake] ouvindo em http://127.0.0.1:{args.port}/v1 (modelo {MODEL_ID})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
