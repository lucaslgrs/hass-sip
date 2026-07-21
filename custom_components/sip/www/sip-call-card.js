/**
 * sip-call-card.js — Custom Lovelace card for hass-sip
 * Feature Set:
 *  - Dynamic UI (Incoming / In-Call / Idle)
 *  - Gate Script Integration with Visual Alert Feedback
 *  - Mute/Unmute microphone control
 *  - Call duration timer
 *  - WebRTC-friendly ringtone audio management
 */

class SipCallCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._mediaRecorder = null;
    this._micStream = null;
    this._micActive = false;
    this._micFirstChunk = false;
    this._isMuted = false;
    this._txUrl = null;
    this._rxUrl = null;
    this._listenStarting = false;
    this._boundRxUrl = null;
    this._lastSipState = null;

    this._callStartedAtMs = null;
    this._durationTimer = null;

    this._remoteAudioEl = document.createElement("audio");
    this._remoteAudioEl.controls = false;
    this._remoteAudioEl.playsInline = true;
    this._remoteAudioEl.preload = "none";
    this._remoteAudioEl.style.display = "none";

    this._ringtoneEl = new Audio();
    this._ringtoneEl.loop = true;
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error("sip-call-card: 'entity' is required");
    }
    this._config = {
      title_incoming: "🔔 Chamando...",
      title_incall: "🔴 Em andamento...",
      ringtone_url: "/local/sounds/ringtone.mp3",
      ...config
    };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._updateState();
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          --card-bg: #1c1c1e;
          --card-border: rgba(255, 255, 255, 0.08);
          --text-primary: #f3f4f6;
          --text-secondary: #9ca3af;
          
          --btn-success-bg: rgba(46, 125, 50, 0.22);
          --btn-success-border: rgba(76, 175, 80, 0.35);
          --btn-success-text: #81c784;

          --btn-danger-bg: rgba(211, 47, 47, 0.2);
          --btn-danger-border: rgba(239, 83, 80, 0.3);
          --btn-danger-text: #e57373;

          --btn-action-bg: rgba(2, 136, 209, 0.2);
          --btn-action-border: rgba(41, 182, 246, 0.3);
          --btn-action-text: #64b5f6;

          --btn-neutral-bg: rgba(255, 255, 255, 0.05);
          --btn-neutral-border: rgba(255, 255, 255, 0.1);
          --btn-neutral-text: #e5e7eb;

          --btn-muted-bg: rgba(245, 124, 0, 0.2);
          --btn-muted-border: rgba(255, 167, 38, 0.35);
          --btn-muted-text: #ffb74d;

          font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }

        ha-card {
          background: var(--card-bg);
          border: 1px solid var(--card-border);
          border-radius: 18px;
          padding: 18px;
          box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
          color: var(--text-primary);
        }

        .card-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 14px;
          min-height: 32px;
        }

        .card-title {
          font-weight: 600;
          font-size: 1.05rem;
          display: flex;
          align-items: center;
          gap: 8px;
          color: var(--text-primary);
        }

        .subtitle {
          font-size: 0.78rem;
          color: var(--text-secondary);
          margin-top: 2px;
        }

        .timer-badge {
          font-size: 0.78rem;
          font-weight: 600;
          padding: 4px 10px;
          border-radius: 20px;
          background: var(--btn-success-bg);
          color: var(--btn-success-text);
          border: 1px solid var(--btn-success-border);
        }

        .grid-buttons-incoming, .grid-buttons-incall {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 10px;
        }

        .btn-full { grid-column: span 2; }

        .btn {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
          padding: 12px 14px;
          border-radius: 12px;
          font-size: 0.88rem;
          font-weight: 600;
          cursor: pointer;
          transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
          border: none;
          outline: none;
        }

        .btn:hover { filter: brightness(1.2); }
        .btn:active { transform: scale(0.98); }

        .btn-success { background: var(--btn-success-bg); border: 1px solid var(--btn-success-border); color: var(--btn-success-text); }
        .btn-danger  { background: var(--btn-danger-bg); border: 1px solid var(--btn-danger-border); color: var(--btn-danger-text); }
        .btn-action  { background: var(--btn-action-bg); border: 1px solid var(--btn-action-border); color: var(--btn-action-text); }
        .btn-neutral { background: var(--btn-neutral-bg); border: 1px solid var(--btn-neutral-border); color: var(--btn-neutral-text); }
        .btn-muted   { background: var(--btn-muted-bg); border: 1px solid var(--btn-muted-border); color: var(--btn-muted-text); }
      </style>

      <ha-card>
        <div class="card-header">
          <div>
            <div class="card-title" id="card-title">🔔 Chamando...</div>
            <div class="subtitle" id="card-subtitle"></div>
          </div>
          <div class="timer-badge" id="timer-badge" style="display: none;">⏱️ 00:00</div>
        </div>

        <div class="grid-buttons-incoming" id="grid-incoming" style="display: none;">
          <button class="btn btn-success" id="btn-answer">📞 Atender</button>
          <button class="btn btn-danger" id="btn-reject">📵 Recusar</button>
        </div>

        <div class="grid-buttons-incall" id="grid-incall" style="display: none;">
          <button class="btn btn-action" id="btn-gate">🔑 Abrir Portão</button>
          <button class="btn btn-neutral" id="btn-mute">🎙️ Mudo</button>
          <button class="btn btn-danger btn-full" id="btn-hangup">📵 Desligar</button>
        </div>

        <div id="rx-audio-slot"></div>
      </ha-card>
    `;

    const audioSlot = this.shadowRoot.getElementById("rx-audio-slot");
    if (audioSlot && this._remoteAudioEl.parentNode !== audioSlot) {
      audioSlot.replaceChildren(this._remoteAudioEl);
    }

    this._el = {
      title:        this.shadowRoot.getElementById("card-title"),
      subtitle:     this.shadowRoot.getElementById("card-subtitle"),
      timerBadge:   this.shadowRoot.getElementById("timer-badge"),
      gridIncoming: this.shadowRoot.getElementById("grid-incoming"),
      gridInCall:   this.shadowRoot.getElementById("grid-incall"),
      btnAnswer:    this.shadowRoot.getElementById("btn-answer"),
      btnReject:    this.shadowRoot.getElementById("btn-reject"),
      btnHangup:    this.shadowRoot.getElementById("btn-hangup"),
      btnMute:      this.shadowRoot.getElementById("btn-mute"),
      btnGate:      this.shadowRoot.getElementById("btn-gate"),
      audio:        this._remoteAudioEl,
    };

    this._el.btnAnswer.addEventListener("click", () => this._answer());
    this._el.btnReject.addEventListener("click", () => this._hangup());
    this._el.btnHangup.addEventListener("click", () => this._hangup());
    this._el.btnMute.addEventListener("click", () => this._toggleMute());
    this._el.btnGate.addEventListener("click", () => this._openGate());
  }

  _startRingtone() {
    if (!this._config.ringtone_url) return;
    try {
      const targetUrl = new URL(this._config.ringtone_url, window.location.href).href;
      this._ringtoneEl.src = targetUrl;
      this._ringtoneEl.play().catch(err => {
        console.debug("SIP Card: Autoplay bloqueado pelo navegador:", err);
      });
    } catch (e) {
      console.error("SIP Card: Erro ao iniciar ringtone:", e);
    }
  }

  _stopRingtone() {
    if (this._ringtoneEl) {
      this._ringtoneEl.pause();
      this._ringtoneEl.currentTime = 0;
      this._ringtoneEl.removeAttribute("src");
      this._ringtoneEl.load();
    }
  }

  _normalizeAudioUrl(url) {
    if (!url) return null;
    try {
      return new URL(url, window.location.href).href;
    } catch (err) {
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
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  _startDurationTimer() {
    if (!this._callStartedAtMs) this._callStartedAtMs = Date.now();
    if (this._durationTimer) return;

    const tick = () => {
      const elapsedSec = (Date.now() - this._callStartedAtMs) / 1000;
      if (this._el?.timerBadge) {
        this._el.timerBadge.textContent = `⏱️ ${this._formatDuration(elapsedSec)}`;
      }
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
  }

  _toggleMute() {
    this._isMuted = !this._isMuted;
    if (this._micStream) {
      this._micStream.getAudioTracks().forEach(track => {
        track.enabled = !this._isMuted;
      });
    }
    this._updateMuteUI();
  }

  _updateMuteUI() {
    if (this._isMuted) {
      this._el.btnMute.className = "btn btn-muted";
      this._el.btnMute.textContent = "🔇 Mutado";
      this._el.subtitle.textContent = "";
    } else {
      this._el.btnMute.className = "btn btn-neutral";
      this._el.btnMute.textContent = "🎙️ Mudo";
      this._el.subtitle.textContent = "🎙️ Microfone ativo";
    }
  }

  async _openGate() {
    if (!this._hass || !this._config.gate_entity) return;

    const btn = this._el.btnGate;
    if (!btn) return;

    const originalText = "🔑 Abrir Portão";
    const originalClass = btn.className;

    const entity = this._config.gate_entity;
    let domain = entity.split(".")[0];
    let service = "turn_on";

    if (domain === "button") service = "press";
    if (domain === "lock") service = "unlock";
    if (domain === "cover") service = "open_cover";

    if (this._config.gate_service) {
      const parts = this._config.gate_service.split(".");
      domain = parts[0];
      service = parts[1];
    }

    try {
      btn.textContent = "🔓 Abrindo...";
      btn.style.pointerEvents = "none";
      btn.style.opacity = "0.8";

      await this._hass.callService(domain, service, { entity_id: entity });

      btn.textContent = "✅ Portão Aberto!";
      btn.className = "btn btn-success";
      btn.style.opacity = "1";

    } catch (err) {
      console.error("SIP Card: Error opening gate", err);
      btn.textContent = "❌ Erro ao Abrir";
      btn.className = "btn btn-danger";
      btn.style.opacity = "1";
    } finally {
      setTimeout(() => {
        if (btn) {
          btn.textContent = originalText;
          btn.className = originalClass;
          btn.style.pointerEvents = "auto";
          btn.style.opacity = "1";
        }
      }, 2500);
    }
  }

  async _sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  _updateState() {
    if (!this._hass || !this._config) return;
    const stateObj = this._hass.states[this._config.entity];
    if (!stateObj) {
      this._el.title.textContent = "Entidade não encontrada";
      return;
    }

    const sipState = (stateObj.attributes.sip_state || "").toLowerCase();
    const rxUrl    = stateObj.attributes.rx_stream_url || null;
    const txUrl    = stateObj.attributes.tx_audio_url  || null;

    this._rxUrl = rxUrl;
    this._txUrl = txUrl;

    const isIncoming = sipState === "incoming";
    const isInCall   = sipState === "in_call";

    if (this._lastSipState !== sipState) {
      if (isIncoming) {
        this._startRingtone();
      } else {
        this._stopRingtone();
      }
      this._lastSipState = sipState;
    }

    if (isIncoming) {
      this._el.title.textContent = this._config.title_incoming;
      this._el.subtitle.textContent = "";
      this._el.timerBadge.style.display = "none";
    } else if (isInCall) {
      this._el.title.textContent = this._config.title_incall;
      this._el.timerBadge.style.display = "block";
      this._updateMuteUI();
    } else {
      this._el.title.textContent = "🔒 Interfone";
      this._el.subtitle.textContent = "";
      this._el.timerBadge.style.display = "none";
    }

    this._el.gridIncoming.style.display = isIncoming ? "grid" : "none";
    this._el.gridInCall.style.display   = isInCall ? "grid" : "none";

    if (!isInCall) {
      this._stopMic();
      this._stopListen();
      this._stopDurationTimer(true);
      this._isMuted = false;
    }

    if (isInCall) {
      this._startDurationTimer();
      if (rxUrl) {
        this._bindRemoteAudio();
        this._el.audio.style.display = "none";
        if (this._el.audio.paused && !this._listenStarting) {
          this._listenStarting = true;
          this._el.audio.play().catch(() => {}).finally(() => {
            this._listenStarting = false;
          });
        }
      }
    }
  }

  async _answer() {
    if (!this._hass) return;
    this._stopRingtone();
    try {
      await this._hass.callService("sip", "answer", {}, { entity_id: this._config.entity });
      await this._sleep(150);

      for (let i = 0; i < 5; i++) {
        const st = this._hass?.states?.[this._config.entity];
        this._txUrl = st?.attributes?.tx_audio_url || this._txUrl;

        const started = await this._startMic();
        if (started || this._micActive) break;
        await this._sleep(250);
      }
    } catch (e) {
      console.error("SIP answer error:", e);
    }
  }

  async _hangup() {
    if (!this._hass) return;
    this._stopRingtone();
    this._stopMic();
    this._stopListen();
    this._stopDurationTimer(true);
    this._isMuted = false;
    try {
      await this._hass.callService("sip", "hangup", {}, { entity_id: this._config.entity });
    } catch (e) {
      console.error("SIP hangup error:", e);
    }
  }

  async _startMic() {
    if (this._micActive) return true;
    if (!this._txUrl) return false;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    } catch (err) {
      console.error("Microphone access denied:", err);
      return false;
    }

    this._micStream = stream;
    this._micActive = true;
    this._micFirstChunk = true;

    this._micStream.getAudioTracks().forEach(track => {
      track.enabled = !this._isMuted;
    });

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
    return true;
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

  getCardSize() { return 2; }
}

customElements.define("sip-call-card", SipCallCard);
