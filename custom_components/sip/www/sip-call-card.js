/**
 * sip-call-card.js — Custom Lovelace card for hass-sip two-way browser audio.
 *
 * Features
 * --------
 * • Displays the SIP call state (idle / incoming / in-call) from the
 *   `media_player` entity produced by the integration.
 * • When a call is active, plays the caller's audio stream (RX path) via an
 *   HTML5 <audio> element using the `rx_stream_url` attribute exposed by the
 *   entity.  Playback is started on a user gesture (the "Listen" button) so
 *   browsers do not block the autoplay policy.
 * • Provides a "Mic On / Mic Off" toggle that calls getUserMedia() (triggering
 *   the browser's microphone permission prompt) and streams mic audio to the
 *   HA `tx_audio_url` endpoint using the MediaRecorder API.  Audio is sent as
 *   small ~200 ms chunks so latency stays low.
 * • Provides Answer and Hang Up buttons that call the `sip.answer` /
 *   `sip.hangup` services.
 *
 * Usage
 * -----
 * 1. Add `/sip/static/sip-call-card.js` as a Lovelace resource
 *    (Settings → Dashboards → Resources → Add resource, type = JavaScript module).
 * 2. Add a card with type `custom:sip-call-card` and set `entity` to your SIP
 *    media_player entity id, e.g.:
 *
 *      type: custom:sip-call-card
 *      entity: media_player.sip_client_100_phone_line
 *
 * Known limitations
 * -----------------
 * - Mic capture only works in the browser/tab where the user presses "Mic On".
 * - The RX audio stream uses Opus/WebM, so it requires ffmpeg with libopus on
 *   the HA host.  Chrome and Firefox both support this format natively.
 * - Browser autoplay policies may prevent the audio from starting automatically
 *   even after answering; click "Listen" if the audio does not start on its own.
 */

class SipCallCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._mediaRecorder = null;
    this._micStream = null;
    this._micActive = false;
    this._txUrl = null;
    this._rxUrl = null;
    this._entryId = null;
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error("sip-call-card: 'entity' is required");
    }
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._updateState();
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card {
          padding: 16px;
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
        }
        .title {
          font-size: 1.1em;
          font-weight: 500;
          margin-bottom: 8px;
        }
        .state-badge {
          display: inline-block;
          padding: 2px 8px;
          border-radius: 12px;
          font-size: 0.85em;
          font-weight: 600;
          margin-bottom: 8px;
          background: var(--primary-color, #03a9f4);
          color: #fff;
        }
        .state-badge.idle { background: var(--disabled-color, #9e9e9e); }
        .state-badge.incoming { background: var(--warning-color, #ff9800); }
        .state-badge.in_call { background: var(--success-color, #4caf50); }
        .caller {
          font-size: 0.95em;
          color: var(--secondary-text-color, #888);
          margin-bottom: 12px;
          min-height: 1.2em;
        }
        .controls {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin-bottom: 12px;
        }
        button {
          padding: 8px 14px;
          border: none;
          border-radius: 4px;
          cursor: pointer;
          font-size: 0.9em;
          font-weight: 500;
          transition: background 0.15s;
        }
        .btn-answer  { background: var(--success-color, #4caf50); color: #fff; }
        .btn-hangup  { background: var(--error-color, #f44336); color: #fff; }
        .btn-listen  { background: var(--primary-color, #03a9f4); color: #fff; }
        .btn-mic     { background: var(--primary-color, #03a9f4); color: #fff; }
        .btn-mic.active { background: var(--error-color, #f44336); }
        button:disabled { opacity: 0.4; cursor: default; }
        audio { width: 100%; margin-top: 4px; display: none; }
        .stream-note {
          font-size: 0.8em;
          color: var(--secondary-text-color, #888);
          margin-top: 8px;
        }
      </style>
      <ha-card>
        <div class="title" id="card-title">SIP Phone</div>
        <div class="state-badge idle" id="state-badge">Idle</div>
        <div class="caller" id="caller-info"></div>
        <div class="controls" id="controls">
          <button class="btn-answer" id="btn-answer" disabled>📞 Answer</button>
          <button class="btn-hangup" id="btn-hangup" disabled>📵 Hang Up</button>
          <button class="btn-listen" id="btn-listen" disabled>🔊 Listen</button>
          <button class="btn-mic" id="btn-mic" disabled>🎤 Mic On</button>
        </div>
        <audio id="rx-audio" controls></audio>
        <div class="stream-note" id="stream-note"></div>
      </ha-card>
    `;

    this._el = {
      title:     this.shadowRoot.getElementById("card-title"),
      badge:     this.shadowRoot.getElementById("state-badge"),
      caller:    this.shadowRoot.getElementById("caller-info"),
      btnAnswer: this.shadowRoot.getElementById("btn-answer"),
      btnHangup: this.shadowRoot.getElementById("btn-hangup"),
      btnListen: this.shadowRoot.getElementById("btn-listen"),
      btnMic:    this.shadowRoot.getElementById("btn-mic"),
      audio:     this.shadowRoot.getElementById("rx-audio"),
      note:      this.shadowRoot.getElementById("stream-note"),
    };

    this._el.btnAnswer.addEventListener("click", () => this._answer());
    this._el.btnHangup.addEventListener("click", () => this._hangup());
    this._el.btnListen.addEventListener("click", () => this._startListen());
    this._el.btnMic.addEventListener("click", () => this._toggleMic());
  }

  _updateState() {
    if (!this._hass || !this._config) return;
    const stateObj = this._hass.states[this._config.entity];
    if (!stateObj) {
      this._el.badge.textContent = "Entity not found";
      return;
    }

    const sipState  = (stateObj.attributes.sip_state || "").toLowerCase();
    const haState   = stateObj.state;
    const caller    = stateObj.attributes.remote_name || stateObj.attributes.remote_party || "";
    const rxUrl     = stateObj.attributes.rx_stream_url || null;
    const txUrl     = stateObj.attributes.tx_audio_url  || null;
    const title     = this._config.title || stateObj.attributes.friendly_name || "SIP Phone";

    this._rxUrl = rxUrl;
    this._txUrl = txUrl;

    this._el.title.textContent = title;

    // Update badge
    const badge = this._el.badge;
    badge.className = "state-badge";
    if (sipState.includes("idle") || haState === "off") {
      badge.classList.add("idle");
      badge.textContent = "Idle";
    } else if (sipState === "incoming") {
      badge.classList.add("incoming");
      badge.textContent = "Incoming call";
    } else if (sipState === "in_call") {
      badge.classList.add("in_call");
      badge.textContent = "In call";
    } else {
      badge.textContent = haState;
    }

    this._el.caller.textContent = caller ? `Caller: ${caller}` : "";

    const isIncoming = sipState === "incoming";
    const isInCall   = sipState === "in_call";
    const hasCall    = isIncoming || isInCall;

    this._el.btnAnswer.disabled = !isIncoming;
    this._el.btnHangup.disabled = !hasCall;
    this._el.btnListen.disabled = !(isInCall && rxUrl);
    this._el.btnMic.disabled    = !isInCall;

    // If we have an rxUrl and audio isn't playing this stream yet, wire it up
    if (rxUrl && this._el.audio.src !== rxUrl) {
      // Don't auto-switch src if mic is active (race condition risk)
      if (!this._micActive) {
        this._el.audio.src = rxUrl;
      }
    }

    // If the call ended, stop mic and hide audio
    if (!isInCall) {
      this._stopMic();
      this._el.audio.style.display = "none";
      this._el.audio.src = "";
      this._el.note.textContent = "";
    }

    if (isInCall && rxUrl) {
      this._el.note.textContent = "Click 'Listen' to hear the caller, 'Mic On' for two-way audio.";
    }
  }

  async _answer() {
    if (!this._hass) return;
    try {
      await this._hass.callService("sip", "answer", {}, { entity_id: this._config.entity });
    } catch (e) {
      console.error("SIP answer error:", e);
    }
  }

  async _hangup() {
    if (!this._hass) return;
    this._stopMic();
    try {
      await this._hass.callService("sip", "hangup", {}, { entity_id: this._config.entity });
    } catch (e) {
      console.error("SIP hangup error:", e);
    }
  }

  _startListen() {
    const audio = this._el.audio;
    if (!this._rxUrl) return;
    // Use HA auth token in Authorization header via a fetch + MediaSource approach
    // falls back to src= with ?auth_callback for browsers that support it
    const token = this._hass?.auth?.data?.access_token;
    let url = this._rxUrl;
    // Append token as query param (HA accepts it for streaming endpoints)
    if (token) {
      url = url + (url.includes("?") ? "&" : "?") + "auth_callback=1&token=" + encodeURIComponent(token);
    }
    audio.src = url;
    audio.style.display = "block";
    audio.play().catch(err => {
      console.warn("SIP RX autoplay blocked:", err);
      this._el.note.textContent = "Tap the audio player to start listening.";
    });
  }

  async _toggleMic() {
    if (this._micActive) {
      this._stopMic();
    } else {
      await this._startMic();
    }
  }

  async _startMic() {
    if (!this._txUrl) {
      this._el.note.textContent = "No active call for mic input.";
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      this._el.note.textContent = "getUserMedia not supported in this browser.";
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    } catch (err) {
      this._el.note.textContent = `Microphone access denied: ${err.message}`;
      return;
    }

    this._micStream = stream;
    this._micActive = true;
    this._el.btnMic.textContent = "🎤 Mic Off";
    this._el.btnMic.classList.add("active");
    this._el.note.textContent = "Microphone active — your voice is being sent to the caller.";

    // Choose best MIME type supported by this browser
    const mimeTypes = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/ogg",
    ];
    const mimeType = mimeTypes.find(m => MediaRecorder.isTypeSupported(m)) || "";

    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : {});
    this._mediaRecorder = recorder;

    const token = this._hass?.auth?.data?.access_token;
    const txUrl = this._txUrl;

    recorder.ondataavailable = async (event) => {
      if (!event.data || event.data.size === 0) return;
      if (!this._micActive) return;

      const headers = { "Content-Type": mimeType || "audio/webm" };
      if (token) headers["Authorization"] = "Bearer " + token;

      try {
        await fetch(txUrl, {
          method: "POST",
          headers,
          body: event.data,
        });
      } catch (err) {
        console.debug("SIP TX audio send error:", err);
      }
    };

    // Send ~200 ms chunks for low latency
    recorder.start(200);
  }

  _stopMic() {
    if (this._mediaRecorder) {
      try { this._mediaRecorder.stop(); } catch (_) {}
      this._mediaRecorder = null;
    }
    if (this._micStream) {
      this._micStream.getTracks().forEach(t => t.stop());
      this._micStream = null;
    }
    this._micActive = false;
    if (this._el && this._el.btnMic) {
      this._el.btnMic.textContent = "🎤 Mic On";
      this._el.btnMic.classList.remove("active");
    }
  }

  getCardSize() {
    return 3;
  }

  static getConfigElement() {
    // Simple editor — just a text input for the entity id
    const el = document.createElement("div");
    el.innerHTML = `
      <style>
        label { display: block; margin-bottom: 4px; font-weight: 500; }
        input { width: 100%; padding: 6px; box-sizing: border-box; }
      </style>
      <label>Entity (media_player):
        <input id="entity" type="text" placeholder="media_player.sip_client_100_phone_line" />
      </label>
    `;
    return el;
  }

  static getStubConfig() {
    return { entity: "media_player.phone_line" };
  }
}

customElements.define("sip-call-card", SipCallCard);

// Register the card with the Lovelace card picker
window.customCards = window.customCards || [];
window.customCards.push({
  type: "sip-call-card",
  name: "SIP Call Card",
  description: "Two-way browser audio for hass-sip: hear the caller and speak via mic.",
  preview: false,
});

console.info(
  "%c SIP-CALL-CARD %c loaded ",
  "color: white; background: #03a9f4; padding: 2px 4px; border-radius: 4px 0 0 4px;",
  "color: #03a9f4; background: #f0f0f0; padding: 2px 4px; border-radius: 0 4px 4px 0;"
);
