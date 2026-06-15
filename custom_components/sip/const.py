"""Constants for the SIP Client integration."""
import logging

DOMAIN = "sip"
LOGGER = logging.getLogger(__package__)

# Config fields
CONF_SERVER = "server"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DOMAIN = "domain"
CONF_CALLER_ID = "caller_id"
CONF_REGISTER_EXPIRATION = "register_expiration"
CONF_LOCAL_RTP_PORT = "local_rtp_port"

# Defaults
DEFAULT_PORT = 5060
DEFAULT_REGISTER_EXPIRATION = 300
DEFAULT_LOCAL_RTP_PORT = 7078

# Events fired by the integration
EVENT_SIP_STATE_CHANGED = "sip_state_changed"
EVENT_SIP_REGISTERED = "sip_registered"
EVENT_SIP_INCOMING_CALL = "sip_incoming_call"
EVENT_SIP_CALL_CONNECTED = "sip_call_connected"
EVENT_SIP_CALL_ENDED = "sip_call_ended"
EVENT_SIP_DTMF_DIGIT = "sip_dtmf_digit"
EVENT_SIP_PLAYBACK_DONE = "sip_playback_done"
EVENT_SIP_RECORDING_STARTED = "sip_recording_started"
EVENT_SIP_RECORDING_STOPPED = "sip_recording_stopped"

