"""Pluggable audio sources / sinks for the RTP stream.

This is the extension seam. The SIP/RTP core only knows about two PCM ports:

* :class:`AudioSource` feeds TX audio into ``RtpSession.push_tx_audio``.
* :class:`AudioSink` receives RX audio from ``RtpSession.on_audio``.

Today's implementations cover "play a file / TTS to the far end" (TX) and
"discard / record" (RX). A microphone source or a media_player sink can be added
later by implementing the same tiny interfaces, without touching the SIP core.
"""
from __future__ import annotations

import asyncio
import logging
import wave
from abc import ABC, abstractmethod
from typing import Callable

from .rtp_session import FRAME_BYTES

_LOGGER = logging.getLogger(__name__)

PushFn = Callable[[bytes], None]
ActiveFn = Callable[[], bool]


class AudioSource(ABC):
    """Produces 8 kHz / s16le / mono PCM and pushes it into the RTP TX path."""

    @abstractmethod
    async def run(self, push: PushFn, is_active: ActiveFn) -> None:
        """Stream until exhausted or ``is_active()`` returns False."""


class AudioSink(ABC):
    """Consumes 8 kHz / s16le / mono PCM coming off the RTP RX path."""

    @abstractmethod
    def write(self, pcm_le: bytes) -> None:
        ...

    def close(self) -> None:  # optional
        ...


class NullSink(AudioSink):
    """Default sink: keeps the stream alive but discards audio.

    Tracks bytes received so the bidirectional stream can be verified.
    """

    def __init__(self) -> None:
        self.bytes_received = 0

    def write(self, pcm_le: bytes) -> None:
        self.bytes_received += len(pcm_le)


class WavRecorderSink(AudioSink):
    """Records received audio to a WAV file (handy for verifying the RX path)."""

    def __init__(self, path: str) -> None:
        self._wav = wave.open(path, "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(8000)

    def write(self, pcm_le: bytes) -> None:
        self._wav.writeframes(pcm_le)

    def close(self) -> None:
        try:
            self._wav.close()
        except Exception:  # noqa: BLE001
            pass


class SpeakerSink(AudioSink):
    """Routes received 8kHz RTP audio to system speaker via ffmpeg.
    
    This sink buffers incoming call audio and streams it in real-time to the
    system's default audio output device using PulseAudio, enabling incoming 
    callers to be heard through a local speaker.
    """

    def __init__(self, ffmpeg_bin: str = "ffmpeg") -> None:
        """Initialize the speaker sink.
        
        Args:
            ffmpeg_bin: Path to ffmpeg binary (default: 'ffmpeg' in PATH)
        """
        self._bin = ffmpeg_bin
        self._proc = None
        self._proc_task = None
        self._buffer_queue: asyncio.Queue[bytes] | None = None
        self._is_active = False
        self.bytes_written = 0

    def _start_ffmpeg(self) -> None:
        """Start ffmpeg process on first write."""
        if self._is_active:
            return
        self._is_active = True

        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            _LOGGER.warning("No event loop available for SpeakerSink")
            return

        self._buffer_queue = asyncio.Queue()
        self._proc_task = self._loop.create_task(self._run_ffmpeg())

    async def _run_ffmpeg(self) -> None:
        """Run ffmpeg process to stream audio to speakers."""
        try:
            # Use PulseAudio (most common on Linux/HA)
            cmd = [
                self._bin,
                "-hide_banner",
                "-loglevel", "error",
                "-f", "s16le",
                "-ar", "8000",
                "-ac", "1",
                "-i", "pipe:0",
                "-f", "pulse",
                "-",
            ]

            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            
            _LOGGER.debug("SpeakerSink: ffmpeg process started (PID %s)", self._proc.pid)
            
            # Feed queued audio to ffmpeg stdin
            while self._is_active and self._proc is not None:
                try:
                    chunk = await asyncio.wait_for(self._buffer_queue.get(), timeout=2.0)
                    if self._proc.stdin:
                        self._proc.stdin.write(chunk)
                        await self._proc.stdin.drain()
                except asyncio.TimeoutError:
                    # Keep-alive: drain to detect broken pipe
                    if self._proc.stdin:
                        try:
                            await self._proc.stdin.drain()
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    break
                except Exception as err:
                    _LOGGER.debug("SpeakerSink ffmpeg write error: %s", err)
                    break
        except Exception as err:
            _LOGGER.debug("SpeakerSink ffmpeg error: %s", err)
        finally:
            if self._proc is not None and self._proc.stdin is not None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
            if self._proc is not None:
                try:
                    await self._proc.wait()
                except Exception:
                    pass
            _LOGGER.debug("SpeakerSink: ffmpeg process stopped")

    def write(self, pcm_le: bytes) -> None:
        """Queue incoming PCM audio for playback."""
        if not self._is_active:
            self._start_ffmpeg()

        self.bytes_written += len(pcm_le)

        if self._buffer_queue is not None and self._is_active:
            try:
                # Non-blocking put; discard oldest if queue is full (prioritize fresh audio)
                self._buffer_queue.put_nowait(pcm_le)
            except asyncio.QueueFull:
                try:
                    self._buffer_queue.get_nowait()  # drop oldest
                    self._buffer_queue.put_nowait(pcm_le)
                except Exception:
                    pass

    def close(self) -> None:
        """Shut down the speaker sink and ffmpeg process."""
        self._is_active = False
        if self._proc_task is not None:
            self._proc_task.cancel()
            self._proc_task = None
        if self._proc is not None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            self._proc = None


class HttpStreamSink(AudioSink):
    """Broadcasts incoming 8 kHz s16le PCM to subscribed HTTP consumers.

    Each browser connection subscribes via :meth:`subscribe`, receives a private
    asyncio.Queue of raw PCM chunks, and is responsible for encoding/streaming
    that data.  Call :meth:`unsubscribe` when the connection closes.

    The sink is designed to be safe from the synchronous ``write()`` call path:
    all queue operations are non-blocking (``put_nowait``), and the oldest frame
    is silently dropped when a subscriber's buffer is full to keep latency low.
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[bytes | None]] = []
        self._is_active = True
        self.bytes_received = 0

    def write(self, pcm_le: bytes) -> None:
        """Deliver a PCM frame to all active subscribers."""
        self.bytes_received += len(pcm_le)
        for q in list(self._subscribers):
            try:
                q.put_nowait(pcm_le)
            except asyncio.QueueFull:
                # Drop the oldest frame to keep latency low, then insert new one
                try:
                    q.get_nowait()
                    q.put_nowait(pcm_le)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("HttpStreamSink queue operation failed: %s", exc)

    def subscribe(self) -> "asyncio.Queue[bytes | None]":
        """Register a new consumer; returns a queue that receives PCM chunks.

        A ``None`` sentinel in the queue signals that the stream has ended.
        """
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=50)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[bytes | None]") -> None:
        """Deregister a consumer queue."""
        if q in self._subscribers:
            self._subscribers.remove(q)
        # Best-effort EOF signal so the reader unblocks
        try:
            q.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def close(self) -> None:
        """Shut down the sink and notify all subscribers with an EOF sentinel."""
        self._is_active = False
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._subscribers.clear()


class FfmpegAudioSource(AudioSource):
    """Decode any media (file path, URL, or raw bytes) to 8 kHz mono via ffmpeg.

    ffmpeg transparently handles WAV/MP3/etc. and produces the exact format the
    G.711 encoder expects, so this single source covers audio files, HTTP URLs
    and TTS output. It paces itself at ~real time so the RTP TX buffer stays
    small (no dropped audio).
    """

    def __init__(
        self,
        ffmpeg_bin: str = "ffmpeg",
        *,
        url: str | None = None,
        data: bytes | None = None,
    ) -> None:
        if (url is None) == (data is None):
            raise ValueError("Provide exactly one of url/data")
        self._bin = ffmpeg_bin
        self._url = url
        self._data = data

    async def run(self, push: PushFn, is_active: ActiveFn) -> None:
        src = self._url if self._url is not None else "pipe:0"
        proc = await asyncio.create_subprocess_exec(
            self._bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            src,
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE if self._data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        try:
            if self._data is not None and proc.stdin is not None:
                proc.stdin.write(self._data)
                proc.stdin.write_eof()

            # Read ~20 ms at a time and pace to real time so the RTP buffer
            # never overflows and drops audio.
            while is_active():
                chunk = await proc.stdout.read(FRAME_BYTES)
                if not chunk:
                    break
                push(chunk)
                await asyncio.sleep(0.018)
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
