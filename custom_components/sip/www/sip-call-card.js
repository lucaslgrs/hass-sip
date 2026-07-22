/**
 * sip-call-card.js — Custom Lovelace card for hass-sip
 * Feature Set:
 *  - Dynamic UI matching HA dark theme (Incoming / In-Call / Idle)
 *  - Gate Script Integration with Visual Alert Feedback
 *  - Mute/Unmute microphone control
 *  - Call duration timer
 *  - WebRTC-friendly ringtone audio management
 *  - Correct Bearer Token authentication for audio stream
 *  - Compact visual layout with pixel-perfect vertical alignment
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
      title_incoming: "Chamando...",
      title_incall: "Em andamento...",
      ringtone_url: "/local/sounds/ringtone.mp3",
      ...config
    };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._updateState();
  }

  _getAuthToken() {
    return (
      this._hass?.auth?.accessToken ||
      this._hass?.connection?.auth?.accessToken ||
      this._hass?.auth?.data?.access_token ||
      ""
    );
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }

        /* RESET E ALINHAMENTO MATEMÁTICO DOS ÍCONES E TEXTOS */
        ha-icon {
          display: inline-flex !important;
          align-items: center !important;
          justify-content: center !important;
          flex-shrink: 0;
          margin: 0 !important;
          padding: 0 !important;
        }

        .status-title span,
        .mic-subtitle span,
        .timer-badge span,
        .btn span {
          display: inline-block;
          line-height: 1 !important;
        }

        /* CARD COMPACTO */
        ha-card {
          background-color: #18181a;
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 16px;
          padding: 14px 16px;
          box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
          color: #ffffff;
          box-sizing: border-box;
        }

        /* CABEÇALHO DINÂMICO SEM ALTURA MÍNIMA FIXA */
        .card-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 12px;
        }

        .status-container {
          display: flex;
          flex-direction: column;
          justify-content: center;
          gap: 4px;
        }

        .status-title {
          display: flex;
          align-items: center;
          gap: 8px;
          font-size: 0.95rem;
          font-weight: 600;
          color: #ffffff;
          line-height: 1;
        }

        .icon-bell {
          color: #ffca28;
          --mdc-icon-size: 18px;
          width: 18px;
          height: 18px;
        }

        .icon-active {
          color: #e53935;
          --mdc-icon-size: 10px;
          width: 10px;
          height: 10px;
        }

        .icon-idle {
          color: #9e9e9e;
          --mdc-icon-size: 18px;
          width: 18px;
          height: 18px;
        }

        .mic-subtitle {
          display: flex;
          align-items: center;
          gap: 6px;
          font-size: 0.8rem;
          color: #9e9e9e;
          line-height: 1;
        }

        .mic-subtitle ha-icon {
          --mdc-icon-size: 14px;
          width: 14px;
          height: 14px;
        }

        /* CRONÔMETRO COMPACTO */
        .timer-badge {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          background: rgba(46, 125, 50, 0.15);
          border: 1px solid #2e7d32;
          color: #81c784;
          padding: 4px 10px;
          border-radius: 20px;
          font-size: 0.82rem;
          font-weight: 600;
          line-height: 1;
        }

        .timer-badge ha-icon {
          --mdc-icon-size: 14px;
          width: 14px;
          height: 14px;
        }

        /* GRID DE BOTÕES COMPACTO */
        .grid-buttons-incoming, .grid-buttons-incall {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 8px;
        }

        .btn-full { grid-column: span 2; }

        /* ESTILO DOS BOTÕES COMPACTOS */
        .btn {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
          height: 40px;
          border-radius: 10px;
          font-size: 0.88rem;
          font-weight: 600;
          cursor: pointer;
          transition: all 0.15s ease;
          outline: none;
          box-sizing: border-box;
          line-height: 1;
        }

        .btn ha-icon {
          --mdc-icon-size: 16px;
          width: 16px;
          height: 16px;
        }

        .btn:active { transform: scale(0.98); }

        /* CORES ESPECÍFICAS */
        .btn-answer {
          background: rgba(27, 77, 41, 0.25);
          border: 1px solid #2e7d32;
          color: #81c784;
        }

        .btn-reject, .btn-hangup {
          background: rgba(120, 28, 32, 0.25);
          border: 1px solid #d32f2f;
          color: #ef5350;
        }

        .btn-gate {
          background: rgba(13, 71, 120, 0.25);
          border: 1px solid #0288d1;
          color: #64b5f6;
        }

        .btn-mute {
          background: rgba(255, 255, 255, 0.05);
          border: 1px solid rgba(255, 255, 255, 0.12);
          color: #ffffff;
        }

        .btn-mute.active {
          background: rgba(245, 124, 0, 0.2);
          border: 1px solid #f57c00;
          color: #ffb74d;
        }

        .spin {
          animation: spin 1s linear infinite;
        }

        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      </style>

      <ha-card>
        <div class="card-header">
          <div class="status-container">
            <div class="status-title" id="card-title">
              <ha-icon class="icon-idle" icon="mdi:doorbell"></ha-icon>
              <span>Interfone</span>
            </div>
            <div class="mic-subtitle" id="card-subtitle" style="display: none;">
              <ha-icon id="mic-subtitle-icon" icon="mdi:microphone"></ha-icon>
              <span id="mic-subtitle-text">Microfone ativo</span>
            </div>
          </div>

          <div class="timer-badge" id="timer-badge" style="display: none;">
            <ha-icon icon="mdi:timer-outline"></ha-icon>
            <span id="timer-text">00:00</span>
          </div>
        </div>

        <div class="grid-buttons-incoming" id="grid-incoming" style="display: none;">
          <button class="btn btn-answer" id="btn-answer">
            <ha-icon icon="mdi:phone"></ha-icon>
            <span>Atender</span>
          </button>
          <button class="btn btn-reject" id="btn-reject">
            <ha-icon icon="mdi:phone-off"></ha-icon>
            <span>Recusar</span>
          </button>
        </div>

        <div class="grid-buttons-incall" id="grid-incall" style="display: none;">
          <button class="btn btn-gate" id="btn-gate">
            <ha-icon id="gate-icon" icon="mdi:key-variant"></ha-icon>
            <span id="gate-text">Abrir Portão</span>
          </button>
          <button class="btn btn-mute" id="btn-mute">
            <ha-icon id="mute-icon" icon="mdi:microphone"></ha-icon>
            <span id="mute-text">Mudo</span>
          </button>
          <button class="btn btn-hangup btn-full" id="btn-hangup">
            <ha-icon icon="mdi:phone-off"></ha-icon>
            <span>Desligar</span>
          </button>
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
      subtitleIcon: this.shadowRoot.getElementById("mic-subtitle-icon"),
      subtitleText: this.shadowRoot.getElementById("mic-subtitle-text"),
      timerBadge:   this.shadowRoot.getElementById("timer-badge"),
      timerText:    this.shadowRoot.getElementById("timer-text"),
      gridIncoming: this.shadowRoot.getElementById("grid-incoming"),
      gridInCall:   this.shadowRoot.getElementById("grid-incall"),
      btnAnswer:    this.shadowRoot.getElementById("btn-answer"),
      btnReject:    this.shadowRoot.getElementById("btn-reject"),
      btnHangup:    this.shadowRoot.getElementById("btn-hangup"),
      btnMute:      this.shadowRoot.getElementById("btn-mute"),
      muteIcon:     this.shadowRoot.getElementById("mute-icon"),
      muteText:     this.shadowRoot.getElementById("mute-text"),
      btnGate:      this.shadowRoot.getElementById("btn-gate"),
      gateIcon:     this.shadowRoot.getElementById("gate-icon"),
      gateText:     this.shadowRoot.getElementById("gate-text"),
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
      if (this._el?.timerText) {
        this._el.timerText.textContent = this._formatDuration(elapsedSec);
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
    if (!this._el) return;
    if (this._isMuted) {
      this._el.btnMute.className = "btn btn-mute active";
      if (this._el.muteIcon) this._el.muteIcon.setAttribute("icon", "mdi:microphone-off");
      if (this._el.muteText) this._el.muteText.textContent = "Mutado";
      if (this._el.subtitleIcon) this._el.subtitleIcon.setAttribute("icon", "mdi:microphone-off");
      if (this._el.subtitleText) this._el.subtitleText.textContent = "Microfone mutado";
    } else {
      this._el.btnMute.className = "btn btn-mute";
      if (this._el.muteIcon) this._el.muteIcon.setAttribute("icon", "mdi:microphone");
      if (this._el.muteText) this._el.muteText.textContent = "Mudo";
      if (this._el.subtitleIcon) this._el.subtitleIcon.setAttribute("icon", "mdi:microphone");
      if (this._el.subtitleText) this._el.subtitleText.textContent = "Microfone ativo";
    }
  }

  async _openGate() {
    if (!this._hass || !this._config.gate_entity) return;

    const btn = this._el.btnGate;
    if (!btn) return;

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
      if (this._el.gateIcon) {
        this._el.gateIcon.setAttribute("icon", "mdi:loading");
        this._el.gateIcon.classList.add("spin");
      }
      if (this._el.gateText) this._el.gateText.textContent = "Abrindo...";
      btn.style.pointerEvents = "none";

      await this._hass.callService(domain, service, { entity_id: entity });

      if (this._el.gateIcon) {
        this._el.gateIcon.classList.remove("spin");
        this._el.gateIcon.setAttribute("icon", "mdi:check-circle-outline");
      }
      if (this._el.gateText) this._el.gateText.textContent = "Portão Aberto!";
      btn.className = "btn btn-answer";

    } catch (err) {
      console.error("SIP Card: Error opening gate", err);
      if (this._el.gateIcon) {
        this._el.gateIcon.classList.remove("spin");
        this._el.gateIcon.setAttribute("icon", "mdi:close-circle-outline");
      }
      if (this._el.gateText) this._el.gateText.textContent = "Erro ao Abrir";
      btn.className = "btn btn-reject";
    } finally {
      setTimeout(() => {
        if (btn) {
          if (this._el.gateIcon) {
            this._el.gateIcon.classList.remove("spin");
            this._el.gateIcon.setAttribute("icon", "mdi:key-variant");
          }
          if (this._el.gateText) this._el.gateText.textContent = "Abrir Portão";
          btn.className = originalClass;
          btn.style.pointerEvents = "auto";
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
      this._el.title.innerHTML = `<span>Entidade não encontrada</span>`;
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
      const titleText = this._config.title_incoming.replace(/^[\u200B-\u200D\uFEFF]|[\u2700-\u27BF]|[\uE000-\uF8FF]|\uD83C[\uDC00-\uDFFF]|\uD83D[\uDC00-\uDFFF]|[\u2011-\u26FF]|\uD83E[\uDD10-\uDDFF]/g, '').trim();
      this._el.title.innerHTML = `<ha-icon class="icon-bell" icon="mdi:bell-ring"></ha-icon><span>${titleText || "Chamando..."}</span>`;
      this._el.subtitle.style.display = "none";
      this._el.timerBadge.style.display = "none";
    } else if (isInCall) {
      const titleText = this._config.title_incall.replace(/^[\u200B-\u200D\uFEFF]|[\u2700-\u27BF]|[\uE000-\uF8FF]|\uD83C[\uDC00-\uDFFF]|\uD83D[\uDC00-\uDFFF]|[\u2011-\u26FF]|\uD83E[\uDD10-\uDDFF]/g, '').trim();
      this._el.title.innerHTML = `<ha-icon class="icon-active" icon="mdi:circle"></ha-icon><span>${titleText || "Em andamento..."}</span>`;
      this._el.subtitle.style.display = "flex";
      this._el.timerBadge.style.display = "inline-flex";
      this._updateMuteUI();
    } else {
      this._el.title.innerHTML = `<ha-icon class="icon-idle" icon="mdi:doorbell"></ha-icon><span>Interfone</span>`;
      this._el.subtitle.style.display = "none";
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

      const isFirst = this._micFirstChunk;
      this._micFirstChunk = false;
      const url = isFirst ? txUrl + "?action=start" : txUrl;

      const token = this._getAuthToken();
      const headers = { "Content-Type": mimeType || "audio/webm" };
      
      if (token) {
        headers["Authorization"] = "Bearer " + token;
      }

      try {
        await fetch(url, {
          method: "POST",
          headers: headers,
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

    if (this._txUrl && this._hass) {
      const stopUrl = this._txUrl + "?action=stop";
      const token = this._getAuthToken();
      const headers = {};
      
      if (token) {
        headers["Authorization"] = "Bearer " + token;
      }

      fetch(stopUrl, {
        method: "POST",
        headers: headers,
        credentials: "same-origin"
      }).catch(() => {});
    }
  }

  getCardSize() { return 2; }

  static getConfigElement() {
    const el = document.createElement("div");
    el.innerHTML = `
      <style>
        label { display: block; margin-bottom: 4px; font-weight: 500; }
        input { width: 100%; padding: 6px; margin-bottom: 10px; box-sizing: border-box; }
      </style>
      <label>Entidade SIP (media_player):
        <input id="entity" type="text" placeholder="media_player.sip_interfone" />
      </label>
      <label>Entidade do Portão (script, button, lock, switch):
        <input id="gate_entity" type="text" placeholder="script.abrir_portao_do_interfone" />
      </label>
      <label>Caminho do Som da Chamada (Ringtone MP3):
        <input id="ringtone_url" type="text" placeholder="/local/sounds/ringtone.mp3" />
      </label>
    `;
    return el;
  }

  static getStubConfig() {
    return { 
      entity: "media_player.sip_interfone",
      gate_entity: "script.abrir_portao_do_interfone",
      ringtone_url: "/local/sounds/ringtone.mp3"
    };
  }
}

customElements.define("sip-call-card", SipCallCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "sip-call-card",
  name: "SIP Call Card",
  description: "Card moderno de interfone SIP com áudio, campainha, mudo e portão.",
  preview: false,
});
