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
``POST /api/sip/tx_audio/{entry_id}`` accepts short audio blobs (WebM/Opus,
Ogg/Opus, or raw s16le PCM) from the browser's MediaRecorder, converts them to
8 kHz s16le PCM via ffmpeg, and injects each 20 ms frame into the active RTP
session via :meth:`~.sip_client.sip_client.SipClient.push_tx_audio`.

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


class SipTxAudioView(HomeAssistantView):
    """POST /api/sip/tx_audio/{entry_id} — injects browser mic audio into the call.

    Accepts short audio blobs (WebM/Opus or Ogg/Opus produced by MediaRecorder,
    or raw s16le PCM) as the request body, converts them to 8 kHz s16le PCM via
    ffmpeg, and feeds each 20 ms frame to the active SIP call's TX RTP path.

    The endpoint returns immediately after queueing the audio so the browser can
    POST the next chunk right away.  Audio errors are logged but never surface as
    error responses — a bad chunk simply gets skipped.
    """

    url = "/api/sip/tx_audio/{entry_id}"
    name = "api:sip:tx_audio"
    requires_auth = True

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        """Handle POST request — convert and inject browser mic audio."""
        hass: HomeAssistant = request.app["hass"]
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
        if entry_data is None:
            return web.Response(status=404, text="SIP entry not found")

        client: SipClient | None = entry_data.get("client")
        if client is None or not client.in_call:
            return web.Response(status=503, text="Not in call")

        # Read browser audio blob (limit to ~64 KB to avoid abuse)
        try:
            audio_data = await request.read()
        except Exception as err:
            _LOGGER.debug("TX audio: failed to read request body: %s", err)
            return web.Response(status=400)

        if not audio_data:
            return web.Response(status=400, text="Empty body")

        # Detect format and convert with ffmpeg
        fmt = _detect_audio_format(audio_data)
        ffmpeg_bin = get_ffmpeg_bin(hass)
        cmd = _tx_ffmpeg_cmd(ffmpeg_bin, input_format=fmt)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(audio_data), timeout=5.0
            )
        except asyncio.TimeoutError:
            _LOGGER.debug("TX audio: ffmpeg conversion timed out")
            return web.Response(status=204)
        except Exception as err:
            _LOGGER.debug("TX audio: ffmpeg error: %s", err)
            return web.Response(status=204)

        if not stdout:
            _LOGGER.debug("TX audio: ffmpeg produced no output (fmt=%s, len=%d)", fmt, len(audio_data))
            return web.Response(status=204)

        # Inject 20 ms frames into the RTP TX path
        injected = 0
        failed = 0
        for offset in range(0, len(stdout), FRAME_BYTES):
            frame = stdout[offset : offset + FRAME_BYTES]
            if len(frame) == FRAME_BYTES and client.in_call:
                try:
                    client.push_tx_audio(frame)
                    injected += 1
                except Exception as err:  # noqa: BLE001
                    failed += 1
                    _LOGGER.debug(
                        "TX audio: frame %d push error: %s",
                        offset // FRAME_BYTES, err,
                    )

        if failed:
            _LOGGER.debug(
                "TX audio: injected %d frames, %d failed for entry %s",
                injected, failed, entry_id,
            )
        else:
            _LOGGER.debug(
                "TX audio: injected %d frames (%d bytes PCM) for entry %s",
                injected, len(stdout), entry_id,
            )
        return web.Response(status=204)
