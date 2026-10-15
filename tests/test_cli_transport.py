"""The Brain↔CLI transport: framing, its explicit bound, and how it fails.

Measured 2026-10-09 (the two reported failures, CASO B and CASO C in the chat): the
sentences "Separator is found, but chunk is longer than limit" and "Separator is not found,
and chunk exceed the limit" were the *reader's* own — asyncio's default stream limit is
64 KiB and one CLI message can be larger — and the raw sentence was reported to the user as
the task's result, with zero actions, classified `CONFIGURATION`. Two properties are held
here: a valid message is delivered whole however it was fragmented, and a message past the
transport's bound fails *as a transport failure with a name* — never truncated, never
reinterpreted, and never blamed on the application the round was driving.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.brain.cli import process as process_module  # noqa: E402
from app.brain.cli.process import LINE_LIMIT_BYTES, spawn  # noqa: E402
from app.brain.contracts import BrainError, ErrorKind  # noqa: E402

CHILD = str(ROOT / "tests" / "transport_child.py")


def child(mode: str, size: int = 0) -> list[str]:
    return [sys.executable, "-X", "utf8", CHILD, mode, str(size)]


def read(mode: str, size: int = 0, *, limit: int | None = None) -> str:
    """Spawn the child, read one message, always stop the process."""

    async def scenario() -> str:
        if limit is not None:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(process_module, "LINE_LIMIT_BYTES", limit)
                handle = await spawn(child(mode, size))
                try:
                    return await handle.readline(10.0)
                finally:
                    await handle.stop()
        handle = await spawn(child(mode, size))
        try:
            return await handle.readline(10.0)
        finally:
            await handle.stop()

    return asyncio.run(scenario())


def test_the_transport_bound_is_explicit_and_large_enough_for_one_message() -> None:
    """The bound is ours and stated, not asyncio's accidental 64 KiB default."""
    assert LINE_LIMIT_BYTES >= 256 * 1024
    assert LINE_LIMIT_BYTES != 64 * 1024


def test_the_reader_is_created_with_that_bound() -> None:
    async def scenario() -> int:
        handle = await spawn(child("silent"))
        try:
            return handle.process.stdout._limit  # type: ignore[union-attr]
        finally:
            await handle.stop()

    assert asyncio.run(scenario()) == LINE_LIMIT_BYTES


def test_a_small_message_is_returned_whole() -> None:
    assert read("line", 40) == "x" * 40 + "\n"


def test_a_message_above_the_old_64_kib_default_is_delivered_whole() -> None:
    """The reported failure itself: 200 000 bytes in one line used to raise."""
    assert read("line", 200_000) == "x" * 200_000 + "\n"


def test_a_line_delivered_in_many_chunks_arrives_as_one_message() -> None:
    """The delimiter arrives after several reads; that is not a partial message."""
    assert read("fragmented", 50_000) == "y" * 50_000 + "\n"


def test_consecutive_messages_are_read_one_by_one_and_then_eof() -> None:
    async def scenario() -> list[str]:
        handle = await spawn(child("two_lines"))
        try:
            return [await handle.readline(10.0) for _ in range(3)]
        finally:
            await handle.stop()

    assert asyncio.run(scenario()) == ['{"first": 1}\n', '{"second": 2}\n', ""]


def test_eof_in_the_middle_of_a_message_is_that_partial_message_not_an_error() -> None:
    """A last line without a trailing newline is still a message; the parser judges it."""
    from app.brain.cli.headless import extract_json_reply

    assert read("partial") == '{"text": "trunc'
    with pytest.raises(BrainError) as raised:
        extract_json_reply('{"text": "trunc')
    assert raised.value.kind is ErrorKind.MALFORMED_RESPONSE


def test_a_silent_process_that_ends_reports_eof_and_not_a_crash() -> None:
    assert read("silent") == ""


def test_a_message_past_the_bound_fails_as_a_named_transport_failure() -> None:
    async def scenario() -> tuple[BrainError, bool, BrainError]:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(process_module, "LINE_LIMIT_BYTES", 4096)
            handle = await spawn(child("line", 40_000))
            try:
                with pytest.raises(BrainError) as raised:
                    await handle.readline(10.0)
                desynced = handle.stream_desynced
                with pytest.raises(BrainError) as again:
                    await handle.readline(10.0)
                return raised.value, desynced, again.value
            finally:
                await handle.stop()

    failure, desynced, second = asyncio.run(scenario())
    assert failure.kind is ErrorKind.TRANSPORT
    assert "4096 bytes" in failure.message and "transporte" in failure.message
    assert "nada foi truncado" in failure.message.casefold()
    # The reader's own sentence is not what the user reads.
    assert "Separator" not in failure.message and "chunk" not in failure.message
    # The message boundary is unknown afterwards, so nothing more may be read from it.
    assert desynced is True and second.kind is ErrorKind.TRANSPORT


def test_the_same_bound_holds_when_the_oversized_line_was_split_by_the_pipe() -> None:
    """Chunked delivery must not turn an over-limit line into two bounded ones."""
    async def scenario() -> BrainError:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(process_module, "LINE_LIMIT_BYTES", 8192)
            handle = await spawn(child("fragmented", 40_000))
            try:
                with pytest.raises(BrainError) as raised:
                    await handle.readline(10.0)
                return raised.value
            finally:
                await handle.stop()

    assert asyncio.run(scenario()).kind is ErrorKind.TRANSPORT


def test_a_killed_process_does_not_become_an_opaque_reader_error() -> None:
    async def scenario() -> str:
        handle = await spawn(child("stall"))
        try:
            handle.process.kill()
            await handle.process.wait()
            return await handle.readline(10.0)
        finally:
            await handle.stop()

    assert asyncio.run(scenario()) == ""


def test_a_read_cancelled_by_the_caller_raises_cancellation_and_leaves_no_orphan() -> None:
    async def scenario() -> tuple[str, bool]:
        handle = await spawn(child("stall"))
        waiting = asyncio.ensure_future(handle.readline(30.0))
        await asyncio.sleep(0.2)
        waiting.cancel()
        kind = ""
        try:
            await waiting
        except asyncio.CancelledError:
            kind = "cancelled"
        await handle.stop()
        return kind, handle.process.returncode is not None

    assert asyncio.run(scenario()) == ("cancelled", True)


def test_a_broken_pipe_on_write_is_a_named_failure() -> None:
    """A CLI that ends its input early is reported as a process failure, not a traceback."""
    async def scenario() -> BrainError:
        handle = await spawn(child("silent"))
        try:
            await handle.wait(10.0)
            with pytest.raises(BrainError) as raised:
                await handle.write("prompt")
            return raised.value
        finally:
            await handle.stop()

    assert asyncio.run(scenario()).kind is ErrorKind.PROCESS_CRASHED


def test_the_raw_sentence_can_no_longer_escape_through_readline() -> None:
    """Regression guard: nothing in the transport may hand back asyncio's sentence."""
    async def scenario() -> list[str]:
        messages: list[str] = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(process_module, "LINE_LIMIT_BYTES", 2048)
            for mode, size in (("line", 20_000), ("fragmented", 20_000)):
                handle = await spawn(child(mode, size))
                try:
                    try:
                        await handle.readline(10.0)
                    except BrainError as exc:
                        messages.append(exc.message)
                finally:
                    await handle.stop()
        return messages

    for message in asyncio.run(scenario()):
        assert "Separator" not in message and "chunk" not in message
