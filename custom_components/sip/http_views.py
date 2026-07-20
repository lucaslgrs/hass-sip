"""HTTP views for live browser audio I/O during SIP calls.

RX path (caller → browser)
---------------------------
``GET /api/sip/rx_stream/{entry_id}`` streams the caller's incoming audio as
Opus/WebM (or raw 8 kHz s16le PCM as a fallback when libopus is unavailable).
Each browser connection gets its own private PCM queue fed by the active
:class:`~.sip_client.audio.HttpStreamSink`; a per-connection ffmpeg process
transcodes the PCM on the fly so the stream is browser-playable.

TX path (browser mic → caller)
-------------------------------
``POST /api/sip/tx_audio/{entry_id}?action=start`` starts a persistent ffmpeg
decode session.  The request body must be the **first** MediaRecorder blob
(which contains the full container header) so that the audio format can be
detected and the ffmpeg process can be seeded with the container/codec info.

``POST /api/sip/tx_audio/{entry_id}`` streams subsequent audio blobs into the
same persistent process.

``POST /api/sip/tx_audio/{entry_id}?action=stop`` (or ``DELETE``) stops and
tears down the persistent process.

Both endpoints require a valid HA authentication token.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER
from .helpers import get_ffmpeg_bin
from .sip_client.audio import HttpStreamSink
from .sip_client.rtp_session import FRAME_BYTES
from .sip_client.sip_client import SipClient

_LOGGER = logging.getLogger(__name__)

# ffmpeg args for PCM → Opus/WebM (low-latency settings)
_OPUS_CMD_TAIL = [
    "-c:a", "libopus",
    "-ar", "48000",
    "-ac", "1",
    "-b:a", "32k",
    "-application", "voip",
    "-frame_duration", "20",
    "-vbr", "on",
    "-f", "webm",
    # Keep WebM clusters small so the browser gets audio quickly
    "-cluster_size_limit", "10000",
    "-cluster_time_limit", "500",
    "pipe:1",
]

# Fallback: raw 8 kHz s16le PCM — no codec needed, but not natively playable
# in browsers; kept as a diagnostic fallback.
_RAW_CMD_TAIL = [
    "-f", "s16le",
    "pipe:1",
]


def _rx_ffmpeg_cmd(ffmpeg_bin: str, *, use_opus: bool = True) -> list[str]:
    """Build the ffmpeg command to transcode 8 kHz PCM to Opus/WebM."""
    base = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel", "error",
        "-f", "s16le", "-ar", "8000", "-ac", "1",
        "-i", "pipe:0",
    ]
    return base + (_OPUS_CMD_TAIL if use_opus else _RAW_CMD_TAIL)


def _tx_ffmpeg_cmd(ffmpeg_bin: str, input_format: str = "webm") -> list[str]:
    """Build the ffmpeg command to convert browser audio → 8 kHz s16le PCM."""
    # Accept 'webm', 'ogg', or 's16le' as input_format
    if input_format == "s16le":
        in_args = ["-f", "s16le", "-ar", "16000", "-ac", "1"]
    else:
        in_args = ["-f", input_format]
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel", "error",
        *in_args,
        "-i", "pipe:0",
        "-ac", "1",
        "-ar", "8000",
        "-f", "s16le",
        "pipe:1",
    ]


def _detect_audio_format(data: bytes) -> str:
    """Detect browser audio format from magic bytes (best-effort)."""
    if data[:4] == b"\x1aE\xdf\xa3":  # EBML header → WebM/MKV
        return "webm"
    if data[:4] == b"OggS":  # Ogg container
        return "ogg"
    # Assume raw 16 kHz s16le PCM as fallback
    return "s16le"


class SipRxStreamView(HomeAssistantView):
    """GET /api/sip/rx_stream/{entry_id} — streams caller audio to browser.

    Streams the active call's incoming (RX) audio as Opus-in-WebM.  Each
    concurrent browser connection gets independent buffering so they don't
    interfere with each other.
    """

    url = "/api/sip/rx_stream/{entry_id}"
    name = "api:sip:rx_stream"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.StreamResponse:
        """Handle GET request — start streaming caller audio."""
        hass: HomeAssistant = request.app["hass"]
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
        if entry_data is None:
            return web.Response(status=404, text="SIP entry not found")

        http_sink: HttpStreamSink | None = entry_data.get("http_sink")
        if http_sink is None or not http_sink._is_active:
            return web.Response(status=503, text="No active call audio stream")

        ffmpeg_bin = get_ffmpeg_bin(hass)

        response = web.StreamResponse()
        response.content_type = "audio/webm;codecs=opus"
        response.headers["Cache-Control"] = "no-cache, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Instruct nginx/Ingress to NOT buffer this stream
        response.headers["X-Accel-Buffering"] = "no"

        try:
            await response.prepare(request)
        except Exception as err:
            _LOGGER.debug("RX stream: failed to prepare response: %s", err)
            return response

        pcm_queue = http_sink.subscribe()
        cmd = _rx_ffmpeg_cmd(ffmpeg_bin, use_opus=True)

        proc = None
        feed_task = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            _LOGGER.debug(
                "RX stream ffmpeg started (PID %s) for entry %s",
                proc.pid, entry_id,
            )

            async def _feed_pcm() -> None:
                """Feed PCM chunks from the sink queue into ffmpeg stdin."""
                assert proc is not None
                try:
                    while http_sink._is_active:
                        try:
                            chunk = await asyncio.wait_for(pcm_queue.get(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        if chunk is None:
                            break
                        if proc.stdin is None or proc.stdin.is_closing():
                            break
                        try:
                            proc.stdin.write(chunk)
                            await proc.stdin.drain()
                        except Exception:
                            break
                except asyncio.CancelledError:
                    pass
                finally:
                    if proc.stdin and not proc.stdin.is_closing():
                        try:
                            proc.stdin.close()
                        except Exception:
                            pass

            feed_task = asyncio.create_task(_feed_pcm())

            # Read encoded chunks and write them to the HTTP response
            assert proc.stdout is not None
            while True:
                encoded = await proc.stdout.read(4096)
                if not encoded:
                    break
                try:
                    await response.write(encoded)
                except Exception as err:
                    _LOGGER.debug("RX stream: client disconnected: %s", err)
                    break

        except asyncio.CancelledError:
            pass
        except Exception as err:
            _LOGGER.debug("RX stream error for entry %s: %s", entry_id, err)
        finally:
            if feed_task is not None:
                feed_task.cancel()
            http_sink.unsubscribe(pcm_queue)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            _LOGGER.debug("RX stream closed for entry %s", entry_id)

        return response


class TxSession:
    """Persistent per-mic-session ffmpeg decode pipeline.

    A single ffmpeg process is kept alive for the entire duration of a mic
    session (from "Mic On" to "Mic Off" / call end).  Every MediaRecorder
    blob chunk is written to the process's stdin so the full container stream
    (including the header present only in the first chunk) is fed as one
    continuous pipe rather than many isolated invocations.

    A background reader task drains stdout, re-chunks the decoded raw PCM into
    ``FRAME_BYTES``-sized frames, and calls ``client.push_tx_audio`` for each
    complete frame.  Any partial trailing bytes are retained in ``_buf`` until
    enough data arrives to form a complete frame.
    """

    def __init__(self, client: SipClient, ffmpeg_bin: str, fmt: str) -> None:
        self._client = client
        self._ffmpeg_bin = ffmpeg_bin
        self._fmt = fmt
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._buf = b""

    async def start(self) -> None:
        """Spawn the ffmpeg process and begin draining its stdout."""
        cmd = _tx_ffmpeg_cmd(self._ffmpeg_bin, input_format=self._fmt)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _LOGGER.debug("TX session: ffmpeg started (PID %s, fmt=%s)", self._proc.pid, self._fmt)
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Drain ffmpeg stdout and push complete FRAME_BYTES frames to the RTP TX path."""
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            while True:
                data = await self._proc.stdout.read(4096)
                if not data:
                    break
                self._buf += data
                while len(self._buf) >= FRAME_BYTES:
                    frame = self._buf[:FRAME_BYTES]
                    self._buf = self._buf[FRAME_BYTES:]
                    try:
                        if self._client.in_call:
                            self._client.push_tx_audio(frame)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("TX session: frame push error: %s", err)
        except asyncio.CancelledError:
            pass
        except Exception as err:
            _LOGGER.debug("TX session: reader loop error: %s", err)

    async def write(self, data: bytes) -> None:
        """Write a raw chunk to ffmpeg stdin (non-blocking write + drain)."""
        if self._proc is None:
            return
        stdin = self._proc.stdin
        if stdin is None or stdin.is_closing():
            return
        try:
            stdin.write(data)
            await stdin.drain()
        except Exception as err:
            _LOGGER.debug("TX session: stdin write error: %s", err)

    async def stop(self) -> None:
        """Close stdin, cancel the reader task, and wait for the process to exit."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._proc is not None:
            stdin = self._proc.stdin
            if stdin is not None and not stdin.is_closing():
                try:
                    stdin.close()
                except Exception:
                    pass
            if self._proc.returncode is None:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await self._proc.wait()
                except Exception:
                    pass
            self._proc = None
        _LOGGER.debug("TX session: stopped")


class SipTxAudioView(HomeAssistantView):
    """POST /api/sip/tx_audio/{entry_id} — injects browser mic audio into the call.

    Three actions are recognised via the ``action`` query parameter:

    ``action=start``
        Start a new persistent ffmpeg decode session.  The request body **must**
        be the first MediaRecorder blob (which contains the full container /
        codec header) so that the audio format can be auto-detected and ffmpeg
        is seeded with the header data.

    ``action=stop`` (or no body)
        Stop and clean up the persistent ffmpeg process for this entry.

    *(no action)*
        Write the request body as the next chunk to the already-running session.

    The endpoint always returns 204 on success.  Audio errors are logged but
    never surface as error responses so the browser keeps streaming.
    """

    url = "/api/sip/tx_audio/{entry_id}"
    name = "api:sip:tx_audio"
    requires_auth = True

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        """Handle POST request — manage or feed the persistent TX ffmpeg session."""
        hass: HomeAssistant = request.app["hass"]
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
        if entry_data is None:
            return web.Response(status=404, text="SIP entry not found")

        action = request.query.get("action", "")

        if action == "stop":
            await _stop_tx_session(entry_data)
            return web.Response(status=204)

        client: SipClient | None = entry_data.get("client")
        if client is None or not client.in_call:
            return web.Response(status=503, text="Not in call")

        if action == "start":
            # The first MediaRecorder blob (with container header) is in the body.
            try:
                audio_data = await request.read()
            except Exception as err:
                _LOGGER.debug("TX audio start: failed to read body: %s", err)
                return web.Response(status=400)

            if not audio_data:
                return web.Response(status=400, text="Empty body for start action")

            # Stop any previously running session for this entry
            await _stop_tx_session(entry_data)

            fmt = _detect_audio_format(audio_data)
            ffmpeg_bin = get_ffmpeg_bin(hass)
            session = TxSession(client, ffmpeg_bin, fmt)
            try:
                await session.start()
            except Exception as err:
                _LOGGER.debug("TX audio: failed to start session: %s", err)
                return web.Response(status=204)

            entry_data["tx_session"] = session

            # Write the first chunk (container header + first audio cluster)
            await session.write(audio_data)
            _LOGGER.debug("TX audio: session started (fmt=%s) for entry %s", fmt, entry_id)
            return web.Response(status=204)

        # Default: stream chunk to the existing session
        session: TxSession | None = entry_data.get("tx_session")
        if session is None:
            # Session not started yet — silently discard; the client will send
            # ?action=start before the next real chunk.
            _LOGGER.debug("TX audio: chunk received but no active session for entry %s", entry_id)
            return web.Response(status=204)

        try:
            audio_data = await request.read()
        except Exception as err:
            _LOGGER.debug("TX audio: failed to read chunk body: %s", err)
            return web.Response(status=400)

        if audio_data:
            await session.write(audio_data)
        return web.Response(status=204)


async def _stop_tx_session(entry_data: dict) -> None:
    """Stop and remove the active TxSession for an entry (if any)."""
    session: TxSession | None = entry_data.pop("tx_session", None)
    if session is not None:
        await session.stop()
