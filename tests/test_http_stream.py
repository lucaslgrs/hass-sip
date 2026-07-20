"""Unit tests for HttpStreamSink (no Home Assistant required).

Run with:
    python -m pytest tests/test_http_stream.py

The sip_client modules use relative package imports that can't be resolved
without an installed package.  This test file mirrors the approach used in
``test_pure.py``: it adds the ``sip_client/`` directory to sys.path and
patches relative imports before executing the module source.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

# ── bootstrap: make sip_client importable standalone ──────────────────────

_SIP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "sip", "sip_client")
)

# Mock rtp_session so `from rtp_session import FRAME_BYTES` works
_mock_rtp = types.ModuleType("rtp_session")
_mock_rtp.FRAME_BYTES = 320
sys.modules["rtp_session"] = _mock_rtp

# Load audio.py with the relative import replaced by an absolute one
_AUDIO_SRC = os.path.join(_SIP_DIR, "audio.py")
with open(_AUDIO_SRC) as _f:
    _audio_src = _f.read().replace(
        "from .rtp_session import FRAME_BYTES",
        "from rtp_session import FRAME_BYTES",
    )

_audio_mod = types.ModuleType("sip_audio_standalone")
exec(compile(_audio_src, _AUDIO_SRC, "exec"), _audio_mod.__dict__)

HttpStreamSink = _audio_mod.HttpStreamSink
NullSink = _audio_mod.NullSink

# ── helpers ────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── tests ──────────────────────────────────────────────────────────────────

def test_initial_state():
    sink = HttpStreamSink()
    assert sink._is_active is True
    assert sink.bytes_received == 0
    assert sink._subscribers == []


def test_write_counts_bytes():
    sink = HttpStreamSink()
    sink.write(b"\x00" * 320)
    assert sink.bytes_received == 320
    sink.write(b"\x01" * 160)
    assert sink.bytes_received == 480


def test_subscribe_receives_chunk():
    sink = HttpStreamSink()
    q = sink.subscribe()
    assert len(sink._subscribers) == 1

    sink.write(b"\xaa" * 320)

    async def _get():
        return await asyncio.wait_for(q.get(), timeout=1.0)

    chunk = _run(_get())
    assert chunk == b"\xaa" * 320


def test_multiple_subscribers_all_receive():
    sink = HttpStreamSink()
    q1 = sink.subscribe()
    q2 = sink.subscribe()

    payload = b"\x01" * 160
    sink.write(payload)

    async def _drain(q):
        return await asyncio.wait_for(q.get(), timeout=1.0)

    assert _run(_drain(q1)) == payload
    assert _run(_drain(q2)) == payload


def test_unsubscribe_removes_queue():
    sink = HttpStreamSink()
    q = sink.subscribe()
    assert len(sink._subscribers) == 1

    sink.unsubscribe(q)
    assert q not in sink._subscribers

    # A write after unsubscribing must not raise
    sink.write(b"\x00" * 320)


def test_unsubscribe_sends_eof_sentinel():
    sink = HttpStreamSink()
    q = sink.subscribe()
    sink.unsubscribe(q)

    async def _get():
        return await asyncio.wait_for(q.get(), timeout=1.0)

    sentinel = _run(_get())
    assert sentinel is None


def test_close_notifies_all_subscribers():
    sink = HttpStreamSink()
    q1 = sink.subscribe()
    q2 = sink.subscribe()
    sink.close()

    assert sink._is_active is False
    assert sink._subscribers == []

    async def _get(q):
        return await asyncio.wait_for(q.get(), timeout=1.0)

    assert _run(_get(q1)) is None
    assert _run(_get(q2)) is None


def test_write_after_close_does_not_raise():
    sink = HttpStreamSink()
    sink.close()
    # Must not raise even though there are no subscribers
    sink.write(b"\x00" * 320)


def test_full_queue_drops_oldest_frame():
    """When a subscriber's queue is full the oldest frame is dropped."""
    sink = HttpStreamSink()
    q = sink.subscribe()

    # Fill the queue to capacity (maxsize=50)
    for i in range(50):
        sink.write(bytes([i % 256]) * 320)

    # Writing one more should drop the oldest and insert the newest
    sink.write(b"\xff" * 320)

    # Drain all; last item should be \xff*320
    chunks = []

    async def _drain():
        while True:
            try:
                chunk = q.get_nowait()
                chunks.append(chunk)
            except asyncio.QueueEmpty:
                break

    _run(_drain())

    assert len(chunks) == 50
    assert chunks[-1] == b"\xff" * 320


def test_write_is_synchronous():
    """write() must be non-blocking (synchronous, no await needed)."""
    sink = HttpStreamSink()
    q = sink.subscribe()
    # Call write 100 times without running the event loop
    for _ in range(100):
        sink.write(b"\x00" * 320)
    sink.close()


def test_close_is_idempotent():
    sink = HttpStreamSink()
    sink.close()
    # Second close must not raise
    sink.close()
