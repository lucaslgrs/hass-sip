"""Unit tests for the framework-agnostic SIP core (no Home Assistant needed).

Run with either:
    py tests/test_pure.py
    py -m pytest tests/test_pure.py
"""
import os
import struct
import sys

# Load the standalone modules directly (they have no HA / relative-package deps).
_SIP = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "sip", "sip_client"
)
sys.path.insert(0, os.path.abspath(_SIP))

import g711  # noqa: E402
import sip_auth  # noqa: E402
import sip_message as sm  # noqa: E402


# ---------------------------------------------------------------- g711
def test_g711_silence_constants():
    assert g711.ulaw_to_linear(g711.linear_to_ulaw(0)) == 0
    assert abs(g711.alaw_to_linear(g711.linear_to_alaw(0))) <= 8


def test_g711_roundtrip_within_quant_error():
    for s in (-32000, -8000, -512, -1, 0, 1, 512, 8000, 32000):
        u = g711.ulaw_to_linear(g711.linear_to_ulaw(s))
        a = g711.alaw_to_linear(g711.linear_to_alaw(s))
        tol = abs(s) // 8 + 256  # companding quantisation error grows with magnitude
        assert abs(u - s) <= tol, (s, u)
        assert abs(a - s) <= tol, (s, a)
        # sign must be preserved for non-trivial samples
        if abs(s) > 512:
            assert (u < 0) == (s < 0)
            assert (a < 0) == (s < 0)


def test_g711_encode_decode_frame_length():
    pcm = struct.pack("<160h", *([1000] * 160))
    enc = g711.encode(pcm, 0)
    assert len(enc) == 160
    dec = g711.decode(enc, 0)
    assert len(dec) == 320  # back to s16le


# ------------------------------------------------------------ sip_message
def test_parse_response():
    raw = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/UDP 1.2.3.4:5060\r\n"
        "CSeq: 2 REGISTER\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    m = sm.parse_sip_message(raw)
    assert not m.is_request
    assert m.status_code == 200
    assert m.reason == "OK"
    assert m.header("cseq") == "2 REGISTER"


def test_parse_request():
    raw = (
        "INVITE sip:100@pbx SIP/2.0\r\n"
        "From: <sip:200@pbx>;tag=abc\r\n"
        "Call-ID: xyz@host\r\n\r\n"
    )
    m = sm.parse_sip_message(raw)
    assert m.is_request
    assert m.method == "INVITE"
    assert m.request_uri == "sip:100@pbx"
    assert m.header("call-id") == "xyz@host"


def test_parse_sdp():
    body = (
        "v=0\r\n"
        "c=IN IP4 192.168.0.5\r\n"
        "m=audio 4002 RTP/AVP 0 8 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
    )
    sdp = sm.parse_sdp(body)
    assert sdp.valid
    assert sdp.connection_ip == "192.168.0.5"
    assert sdp.audio_port == 4002
    assert sdp.pcmu_pt == 0
    assert sdp.pcma_pt == 8
    assert sdp.telephone_event_pt == 101


def test_auth_param():
    h = 'Digest realm="asterisk", nonce="abc123", qop="auth", algorithm=MD5'
    assert sm.auth_param(h, "realm") == "asterisk"
    assert sm.auth_param(h, "nonce") == "abc123"
    assert sm.auth_param(h, "qop") == "auth"
    assert sm.auth_param(h, "algorithm") == "MD5"
    assert sm.auth_param(h, "missing") == ""


# -------------------------------------------------------------- sip_auth
def test_digest_response_rfc2617_vector():
    # Canonical RFC 2617 §3.5 example.
    resp = sip_auth.digest_response(
        "Mufasa",
        "Circle Of Life",
        "testrealm@host.com",
        "GET",
        "/dir/index.html",
        "dcd98b7102dd2f0e8b11d0f600bfb0c093",
        "auth",
        "00000001",
        "0a4f113b",
    )
    assert resp == "6629fae49393a05397450978507c4ef1"


def test_digest_response_legacy_no_qop():
    # HA1:nonce:HA2 form must still compute deterministically.
    r1 = sip_auth.digest_response("u", "p", "r", "REGISTER", "sip:x", "n", "", "", "")
    r2 = sip_auth.digest_response("u", "p", "r", "REGISTER", "sip:x", "n", "", "", "")
    assert r1 == r2 and len(r1) == 32


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
