"""Unit tests for TxSession — persistent TX ffmpeg decode pipeline.

Tests focus on the frame re-chunking logic in ``TxSession._read_loop``:
verify that arbitrary-sized reads from ffmpeg stdout are correctly split into
``FRAME_BYTES``-sized frames and that any partial trailing bytes are retained
across reads until enough data arrives to form a complete frame.

These tests run without a live Home Assistant instance.  The ``TxSession``
class is extracted from ``http_views.py`` using source manipulation (replacing
HA/aiohttp imports with stubs) so no external dependencies are required,
matching the pattern used in ``test_http_stream.py``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

# ── bootstrap: extract TxSession from http_views.py without HA/aiohttp ───────

_SIP_CC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "sip")
)
_SIP_CLIENT_DIR = os.path.join(_SIP_CC_DIR, "sip_client")

FRAME_BYTES = 320  # 160 samples * 2 bytes, matches rtp_session.FRAME_BYTES

# Stub external modules that http_views.py imports at module level
_aiohttp_web = types.ModuleType("aiohttp.web")


class _MockView:
    pass


_aiohttp = types.ModuleType("aiohttp")
_aiohttp.web = _aiohttp_web
sys.modules.setdefault("aiohttp", _aiohttp)
sys.modules.setdefault("aiohttp.web", _aiohttp_web)

_ha_http = types.ModuleType("homeassistant.components.http")
_ha_http.HomeAssistantView = _MockView
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))
sys.modules.setdefault("homeassistant.components.http", _ha_http)
sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))

# Stub the relative imports that http_views.py uses
_mock_const = types.ModuleType("const")
_mock_const.DOMAIN = "sip"
_mock_const.LOGGER = __import__("logging").getLogger("tx_session_test")

_mock_helpers = types.ModuleType("helpers")
_mock_helpers.get_ffmpeg_bin = lambda hass: "ffmpeg"

_mock_rtp = types.ModuleType("rtp_session")
_mock_rtp.FRAME_BYTES = FRAME_BYTES

_mock_audio = types.ModuleType("audio")


class _HttpStreamSink:
    pass


_mock_audio.HttpStreamSink = _HttpStreamSink

_mock_sip_client = types.ModuleType("sip_client")


class _SipClient:
    pass


_mock_sip_client.SipClient = _SipClient

# Build a module namespace that mirrors what http_views.py expects after its
# from-imports resolve.  We exec the file with these stubs in place.
_SRC = os.path.join(_SIP_CC_DIR, "http_views.py")
with open(_SRC) as _f:
    _src = _f.read()

# Replace relative package imports with stub names
_src = _src.replace("from aiohttp import web", "from aiohttp import web as _web_unused; web = _web_unused")
_src = _src.replace("from homeassistant.components.http import HomeAssistantView", "HomeAssistantView = _HomeAssistantView")
_src = _src.replace("from homeassistant.core import HomeAssistant", "HomeAssistant = object")
_src = _src.replace("from .const import DOMAIN, LOGGER", "DOMAIN = 'sip'; LOGGER = __import__('logging').getLogger('test')")
_src = _src.replace("from .helpers import get_ffmpeg_bin", "get_ffmpeg_bin = lambda hass: 'ffmpeg'")
_src = _src.replace("from .sip_client.audio import HttpStreamSink", "HttpStreamSink = object")
_src = _src.replace("from .sip_client.rtp_session import FRAME_BYTES", f"FRAME_BYTES = {FRAME_BYTES}")
_src = _src.replace("from .sip_client.sip_client import SipClient", "SipClient = object")

_http_views_mod = types.ModuleType("http_views_standalone")
_http_views_mod.__dict__["_HomeAssistantView"] = _MockView
exec(compile(_src, _SRC, "exec"), _http_views_mod.__dict__)  # noqa: S102

TxSession = _http_views_mod.TxSession

# ── helpers ─────────────────────────────────────────────────────────────────


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _MockClient:
    """Minimal SipClient stub that records pushed TX audio frames."""

    def __init__(self, in_call: bool = True) -> None:
        self.in_call = in_call
        self.frames: list[bytes] = []

    def push_tx_audio(self, frame: bytes) -> None:
        self.frames.append(frame)


async def _run_reader_with_bytes(data: bytes, *, in_call: bool = True) -> list[bytes]:
    """Feed *data* to a TxSession._read_loop via a mock StreamReader and collect frames."""
    client = _MockClient(in_call=in_call)
    session = TxSession(client, "ffmpeg", "webm")

    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()

    class _MockProc:
        stdout = reader
        stdin = None

    session._proc = _MockProc()
    await session._read_loop()
    return client.frames


# ── tests ────────────────────────────────────────────────────────────────────

def test_exact_frame_count():
    """Exactly N * FRAME_BYTES bytes → exactly N frames pushed."""
    data = b"\xab" * (FRAME_BYTES * 5)
    frames = _run(_run_reader_with_bytes(data))
    assert len(frames) == 5
    for frame in frames:
        assert len(frame) == FRAME_BYTES


def test_partial_bytes_retained():
    """When total bytes are not a multiple of FRAME_BYTES the remainder is not pushed."""
    data = b"\xcd" * (FRAME_BYTES * 3 + 50)  # 3 complete frames + 50 leftover bytes
    frames = _run(_run_reader_with_bytes(data))
    assert len(frames) == 3
    for frame in frames:
        assert len(frame) == FRAME_BYTES


def test_single_large_read():
    """One large read containing many frames is properly split."""
    n = 20
    data = b"\x42" * (FRAME_BYTES * n)
    frames = _run(_run_reader_with_bytes(data))
    assert len(frames) == n


def test_small_reads_accumulate():
    """Multiple reads smaller than FRAME_BYTES are accumulated until complete."""
    n_frames = 4
    full_data = b"\x01" * (FRAME_BYTES * n_frames)

    # Split into 7-byte chunks to simulate many tiny stdout reads
    chunk_size = 7
    chunks = [full_data[i: i + chunk_size] for i in range(0, len(full_data), chunk_size)]

    async def _run_with_chunks():
        client = _MockClient()
        session = TxSession(client, "ffmpeg", "webm")
        reader = asyncio.StreamReader()
        for chunk in chunks:
            reader.feed_data(chunk)
        reader.feed_eof()

        class _MockProc:
            stdout = reader
            stdin = None

        session._proc = _MockProc()
        await session._read_loop()
        return client.frames

    frames = _run(_run_with_chunks())
    assert len(frames) == n_frames
    for frame in frames:
        assert len(frame) == FRAME_BYTES


def test_no_frames_when_not_in_call():
    """Frames are not pushed when client.in_call is False."""
    data = b"\xff" * (FRAME_BYTES * 3)
    frames = _run(_run_reader_with_bytes(data, in_call=False))
    assert frames == []


def test_empty_data_no_frames():
    """An empty stdout produces no frames and no errors."""
    frames = _run(_run_reader_with_bytes(b""))
    assert frames == []


def test_frame_content_preserved():
    """The frame bytes written match those read from stdout, in order."""
    data = b"\xaa" * FRAME_BYTES + b"\xbb" * FRAME_BYTES + b"\xcc" * FRAME_BYTES
    frames = _run(_run_reader_with_bytes(data))
    assert len(frames) == 3
    assert frames[0] == b"\xaa" * FRAME_BYTES
    assert frames[1] == b"\xbb" * FRAME_BYTES
    assert frames[2] == b"\xcc" * FRAME_BYTES


def test_odd_sized_read_across_multiple_frames():
    """Reads that straddle frame boundaries are correctly re-chunked."""
    # 5 frames of data, fed in 37-byte increments (not a divisor of FRAME_BYTES)
    n_frames = 5
    data = bytes(range(256)) * ((FRAME_BYTES * n_frames) // 256 + 1)
    data = data[: FRAME_BYTES * n_frames]

    async def _run_odd():
        client = _MockClient()
        session = TxSession(client, "ffmpeg", "webm")
        reader = asyncio.StreamReader()
        step = 37
        for i in range(0, len(data), step):
            reader.feed_data(data[i: i + step])
        reader.feed_eof()

        class _MockProc:
            stdout = reader
            stdin = None

        session._proc = _MockProc()
        await session._read_loop()
        return client.frames, data

    frames, expected_data = _run(_run_odd())
    assert len(frames) == n_frames
    reconstructed = b"".join(frames)
    assert reconstructed == expected_data

