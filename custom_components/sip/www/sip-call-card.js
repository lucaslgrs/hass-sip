/**
 * sip-call-card.js — Custom Lovelace card for hass-sip two-way browser audio.
 *
 * Test-mode variant
 * -----------------
 * • Answer triggers mic capture automatically.
 * • "Mic On" and "Listen" buttons are hidden for simplified testing.
 * • Caller field is removed from UI.
 * • Adds in-call duration timer that starts when call enters in_call state.
 */

class SipCallCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._mediaRecorder = null;
    this._micStream = null;
    this._micActive = false;
    this._micFirstChunk = false;
    this._txUrl = null;
    this._rxUrl = null;
    this._listenStarting = false;
    this._boundRxUrl = null;

    this._callStartedAtMs = null;
    this._durationTimer = null;

    this._remoteAudioEl = document.createElement("audio");
    this._remoteAudioEl.controls = true;
    this._remoteAudioEl.playsInline = true;
    this._remoteAudioEl.preload = "none";
    this._remoteAudioEl.style.display = "none";
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
        .duration {
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
        <div class="duration" id="call-duration"></div>
        <div class="controls" id="controls">
          <button class="btn-answer" id="btn-answer" disabled>📞 Answer</button>
          <button class="btn-hangup" id="btn-hangup" disabled>📵 Hang Up</button>
        </div>
        <div id="rx-audio-slot"></div>
        <div class="stream-note" id="stream-note"></div>
      </ha-card>
    `;

    const audioSlot = this.shadowRoot.getElementById("rx-audio-slot");
    if (audioSlot && this._remoteAudioEl.parentNode !== audioSlot) {
      audioSlot.replaceChildren(this._remoteAudioEl);
    }

    this._el = {
      title:        this.shadowRoot.getElementById("card-title"),
      badge:        this.shadowRoot.getElementById("state-badge"),
      duration:     this.shadowRoot.getElementById("call-duration"),
      btnAnswer:    this.shadowRoot.getElementById("btn-answer"),
      btnHangup:    this.shadowRoot.getElementById("btn-hangup"),
      audio:        this._remoteAudioEl,
      note:         this.shadowRoot.getElementById("stream-note"),
    };

    this._el.btnAnswer.addEventListener("click", () => this._answer());
    this._el.btnHangup.addEventListener("click", () => this._hangup());
  }

  _normalizeAudioUrl(url) {
    if (!url) return null;
    try {
      return new URL(url, window.location.href).href;
    } catch (err) {
      console.debug("SIP RX url normalization failed:", err);
      return url;
    }
  }

  _bindRemoteAudio() {
    const nextUrl = this._normalizeAudioUrl(this._rxUrl);
    if (!nextUrl) return false;

    if (this._boundRxUrl === nextUrl) return false;

    this._el.audio.src = nextUrl;
    this._boundRxUrl = nextUrl;
    return true;
  }

  _stopListen() {
    const audio = this._el?.audio || this._remoteAudioEl;
    if (!audio) return;

    if (!audio.paused) audio.pause();
    if (audio.src || audio.getAttribute("src")) {
      audio.removeAttribute("src");
      audio.src = "";
      try { audio.currentTime = 0; } catch (_) {}
    }
    audio.style.display = "none";
    this._boundRxUrl = null;
    this._listenStarting = false;
  }

  _formatDuration(totalSeconds) {
    const sec = Math.max(0, Math.floor(totalSeconds));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(s).padStart(2, "0");
    if (h > 0) return `${h}:${mm}:${ss}`;
    return `${mm}:${ss}`;
  }

  _startDurationTimer() {
    if (!this._callStartedAtMs) this._callStartedAtMs = Date.now();
    if (this._durationTimer) return;

    const tick = () => {
      const elapsedSec = (Date.now() - this._callStartedAtMs) / 1000;
      if (this._el?.duration) this._el.duration.textContent = `Duration: ${this._formatDuration(elapsedSec)}`;
    };

    tick();
    this._durationTimer = window.setInterval(tick, 1000);
  }

  _stopDurationTimer(reset = false) {
    if (this._durationTimer) {
      clearInterval(this._durationTimer);
      this._durationTimer = null;
    }
    if (reset) this._callStartedAtMs = null;
    if (this._el?.duration && reset) this._el.duration.textContent = "";
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
    const rxUrl     = stateObj.attributes.rx_stream_url || null;
    const txUrl     = stateObj.attributes.tx_audio_url  || null;
    const title     = this._config.title || stateObj.attributes.friendly_name || "SIP Phone";

    this._rxUrl = rxUrl;
    this._txUrl = txUrl;

    this._el.title.textContent = title;

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

    const isIncoming = sipState === "incoming";
    const isInCall   = sipState === "in_call";
    const hasCall    = isIncoming || isInCall;

    this._el.btnAnswer.disabled = !isIncoming;
    this._el.btnHangup.disabled = !hasCall;

    if (!isInCall) {
      this._stopMic();
      this._stopListen();
      this._stopDurationTimer(true);
      this._el.note.textContent = "";
    }

    if (isInCall) {
      this._startDurationTimer();
      if (rxUrl) {
        this._bindRemoteAudio();
        this._el.audio.style.display = "block";
        if (this._el.audio.paused && !this._listenStarting) {
          this._listenStarting = true;
          this._el.audio.play().catch(() => {}).finally(() => {
            this._listenStarting = false;
          });
        }
      }
      this._el.note.textContent = this._micActive
        ? "Call active — microphone streaming is enabled."
        : "Call active.";
    }
  }

  async _answer() {
    if (!this._hass) return;
    try {
      await this._hass.callService("sip", "answer", {}, { entity_id: this._config.entity });
      await this._startMic();
      this._el.note.textContent = "Call answered — microphone streaming enabled.";
    } catch (e) {
      console.error("SIP answer error:", e);
    }
  }

  async _hangup() {
    if (!this._hass) return;
    this._stopMic();
    this._stopListen();
    this._stopDurationTimer(true);
    try {
      await this._hass.callService("sip", "hangup", {}, { entity_id: this._config.entity });
    } catch (e) {
      console.error("SIP hangup error:", e);
    }
  }

  async _startMic() {
    if (this._micActive) return;
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
    this._micFirstChunk = true;

    const mimeTypes = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/ogg",
    ];
    const mimeType = mimeTypes.find(m => MediaRecorder.isTypeSupported(m)) || "";

    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : {});
    this._mediaRecorder = recorder;

    const txUrl = this._txUrl;

    recorder.ondataavailable = async (event) => {
      if (!event.data || event.data.size === 0) return;
      if (!this._micActive) return;

      const headers = { "Content-Type": mimeType || "audio/webm" };
      const freshToken = this._hass?.auth?.data?.access_token;
      if (freshToken) headers["Authorization"] = "Bearer " + freshToken;

      try {
        const isFirst = this._micFirstChunk;
        this._micFirstChunk = false;
        const url = isFirst ? txUrl + "?action=start" : txUrl;
        await fetch(url, {
          method: "POST",
          headers,
          credentials: "same-origin",
          body: event.data,
        });
      } catch (err) {
        console.debug("SIP TX audio send error:", err);
      }
    };

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
    this._micFirstChunk = false;

    if (this._txUrl) {
      const token = this._hass?.auth?.data?.access_token;
      const headers = {};
      if (token) headers["Authorization"] = "Bearer " + token;
      fetch(this._txUrl + "?action=stop", { method: "POST", headers, credentials: "same-origin" }).catch(() => {});
    }
  }

  getCardSize() {
    return 3;
  }

  static getConfigElement() {
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

window.customCards = window.customCards || [];
window.customCards.push({
  type: "sip-call-card",
  name: "SIP Call Card",
  description: "Two-way browser audio for hass-sip (test mode).",
  preview: false,
});

console.info(
  "%c SIP-CALL-CARD %c loaded ",
  "color: white; background: #03a9f4; padding: 2px 4px; border-radius: 4px 0 0 4px;",
  "color: #03a9f4; background: #f0f0f0; padding: 2px 4px; border-radius: 0 4px 4px 0;"
);
