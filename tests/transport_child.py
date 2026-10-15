"""Deterministic stdout shapes for ``tests/test_cli_transport.py``.

Every mode writes raw bytes to stdout and flushes; nothing here knows about the
transport under test, so a failure is the reader's, not the child's.
"""
from __future__ import annotations

import sys
import time

mode = sys.argv[1]
size = int(sys.argv[2]) if len(sys.argv) > 2 else 0

if mode == "line":
    sys.stdout.buffer.write(b"x" * size + b"\n")
elif mode == "fragmented":
    # One logical line, delivered in many chunks: the delimiter only arrives after
    # several reads, which is the ordinary shape of a streaming CLI.
    payload = b"y" * size
    for offset in range(0, len(payload), 1000):
        sys.stdout.buffer.write(payload[offset:offset + 1000])
        sys.stdout.flush()
        time.sleep(0.01)
    sys.stdout.buffer.write(b"\n")
elif mode == "two_lines":
    sys.stdout.buffer.write(b'{"first": 1}\n{"second": 2}\n')
elif mode == "partial":
    # EOF in the middle of a message: no trailing newline at all.
    sys.stdout.buffer.write(b'{"text": "trunc')
elif mode == "silent":
    pass
elif mode == "stall":
    # Never answers: for the timeout and cancellation cases.
    time.sleep(30)
sys.stdout.buffer.flush()
