#!/usr/bin/env python3
"""
Messenger relay — bridge a Facebook Page's inbound messages to your phone (SMS)
and let you reply from your phone straight back into the sender's Messenger thread.

This is the Meta side only. The Twilio side lives in satgpt_sms.py, which imports
this module, runs the shared webhook server, and owns SMS sending. Split this way
there's no circular import: satgpt_sms -> messenger -> satrelay (helpers only).

Flow (two-way):
    FB user --(Messenger)--> Page webhook /messenger --> inbound_to_sms()
        --> satgpt_sms sends you an SMS:  "#a3f Jane Doe: are you open Saturday?"
    You reply by SMS:  "#a3f yes, 9-5!"  --> /sms handler --> route_reply()
        --> Meta Send API --> lands in Jane's Messenger thread.

The "#code" is a short, stable per-sender handle so you can juggle several
threads from one phone. The code<->PSID map is persisted in ~/.satrelay state.

Config keys (added to ~/.satrelay/config.json):
    "fb_page_access_token": "EAAG...",   # Page token with pages_messaging — never share
    "fb_app_secret":        "...",        # verifies inbound webhook signatures
    "fb_verify_token":      "any-string", # must match what you enter in Meta's webhook setup
    "fb_graph_version":     "v21.0",      # optional
    "fb_message_tag":       "",           # optional; set to send outside the 24h window
    "relay_to_number":      "+1...",      # your phone; where Page messages get texted
    "verify_fb_signature":  true,         # flip false only for local testing
    "relay_ack":            true          # text you a small "-> Jane ✓" after a reply
"""

import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import satrelay as sr   # helpers only: log(), load_state()/save_state() shape, norm()

# A reply you text back, e.g. "#a3f yes we're open 9-5". Case-insensitive code.
RE_REPLY = re.compile(r"^\s*#([0-9a-zA-Z]{1,12})\s+(.+)$", re.S)

_GRAPH = "https://graph.facebook.com"


# ---------------------------------------------------------------------------
# Meta transport
# ---------------------------------------------------------------------------

def _ver(cfg):
    return cfg.get("fb_graph_version", "v21.0")


def send_messenger(cfg, psid: str, text: str):
    """Send a message into a Messenger thread. Returns (ok: bool, err: str)."""
    token = cfg.get("fb_page_access_token")
    if not token:
        return False, "no fb_page_access_token"
    url = f"{_GRAPH}/{_ver(cfg)}/me/messages?access_token={urllib.parse.quote(token)}"
    payload = {
        "recipient": {"id": psid},
        "messaging_type": "RESPONSE",
        "message": {"text": text[:2000]},
    }
    tag = cfg.get("fb_message_tag")
    if tag:                       # needed to reply outside Meta's 24h window
        payload["messaging_type"] = "MESSAGE_TAG"
        payload["tag"] = tag
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True, ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return False, f"{e.code} {body[:200]}"
    except Exception as e:
        return False, repr(e)


def get_name(cfg, psid: str) -> str:
    """Best-effort display name for a PSID (needs the right Page permissions)."""
    token = cfg.get("fb_page_access_token")
    if not token:
        return ""
    url = (f"{_GRAPH}/{_ver(cfg)}/{urllib.parse.quote(psid)}"
           f"?fields=name&access_token={urllib.parse.quote(token)}")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return (json.loads(r.read().decode()) or {}).get("name", "") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Webhook: verification handshake + signature check + event parsing
# ---------------------------------------------------------------------------

def verify_webhook(params: dict, cfg):
    """GET handshake. Returns the challenge string to echo, or None to reject."""
    if (params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token")
            and params.get("hub.verify_token") == cfg.get("fb_verify_token")):
        return params.get("hub.challenge", "")
    return None


def valid_signature(cfg, raw: bytes, header: str) -> bool:
    """Verify X-Hub-Signature-256 (HMAC-SHA256 of the raw body with the app secret)."""
    secret = cfg.get("fb_app_secret", "")
    if not (secret and header and header.startswith("sha256=")):
        return False
    mac = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, header)


def parse_events(raw_text: str):
    """Flatten a webhook payload to [(psid, text), ...], skipping noise.

    Skips the Page's own echoes, delivery/read receipts, and empty events;
    renders attachments as a placeholder so you at least know something arrived.
    """
    out = []
    data = json.loads(raw_text)
    for entry in data.get("entry", []):
        for m in entry.get("messaging", []):
            msg = m.get("message")
            sender = (m.get("sender") or {}).get("id")
            if not sender or not msg or msg.get("is_echo"):
                continue
            text = msg.get("text")
            if not text:
                text = "[sent an attachment]" if msg.get("attachments") else None
            if text is None:
                continue
            out.append((sender, text))
    return out


# ---------------------------------------------------------------------------
# Thread codes: short, stable per-sender handles  (state["fb_threads"])
# ---------------------------------------------------------------------------

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(n: int) -> str:
    if n == 0:
        return "0"
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = _B36[r] + s
    return s


def _threads(state):
    return state.setdefault("fb_threads", {})   # {code: {"psid":..., "name":...}}


def code_for(state, psid: str, name: str) -> str:
    """Return this sender's code, minting a stable one (from a hash) if new."""
    threads = _threads(state)
    for c, info in threads.items():
        if info.get("psid") == psid:
            if name and info.get("name") != name:
                info["name"] = name
            return c
    base = int(hashlib.sha1(psid.encode()).hexdigest()[:8], 16)
    stem = _b36(base)
    for n in range(3, 9):                 # widen until we dodge a collision
        cand = stem[:n]
        if cand and cand not in threads:
            threads[cand] = {"psid": psid, "name": name or ""}
            return cand
    i = 0                                 # last-ditch linear fallback
    while f"t{i}" in threads:
        i += 1
    threads[f"t{i}"] = {"psid": psid, "name": name or ""}
    return f"t{i}"


# ---------------------------------------------------------------------------
# The two directions
# ---------------------------------------------------------------------------

def inbound_to_sms(cfg, state, psid: str, text: str):
    """Page message -> (to_number, sms_body). Returns (None, reason) if unsendable."""
    threads = _threads(state)
    existing = next((i for i in threads.values() if i.get("psid") == psid), None)
    name = (existing or {}).get("name") or get_name(cfg, psid)
    code = code_for(state, psid, name)
    to = cfg.get("relay_to_number")
    if not to:
        return None, "no relay_to_number set"
    who = f" {name}" if name else ""      # "#18k Jane: hi"  or  "#18k: hi" when nameless
    return to, f"#{code}{who}: {text}"


def route_reply(cfg, state, code: str, body: str) -> str:
    """Your '#code ...' SMS reply -> Messenger. Returns a short ack for you."""
    info = _threads(state).get(code.lower())
    if not info:
        return f"[relay] unknown thread #{code}"
    ok, err = send_messenger(cfg, info["psid"], body)
    who = info.get("name") or f"#{code}"
    return f"-> {who} ✓" if ok else f"[relay] send failed: {err}"
