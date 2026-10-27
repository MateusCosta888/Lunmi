"""Spotify keeps its closed operation identity without publishing arguments."""
import pytest

from app.core.automatic import SPOTIFY_MUSIC_OPERATIONS
from app.core.controller import _operation_metadata


@pytest.mark.parametrize("operation", sorted(SPOTIFY_MUSIC_OPERATIONS))
def test_public_spotify_operation_keeps_identity_and_discards_private_arguments(operation):
    tool = f"spotify.{operation}"
    assert _operation_metadata({"tool": tool, "state": "started", "query": "private",
                                "uri": "private"}) == {"tool": tool, "state": "started"}


def test_public_spotify_operation_rejects_unknown_tool_name():
    assert _operation_metadata({"tool": "spotify.arbitrary"}) == {}
