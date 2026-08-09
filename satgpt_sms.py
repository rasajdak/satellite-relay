#!/usr/bin/env python3
"""
SatGPT (SMS) — a text-message front-end for SatGPT, no Mac required.

A tiny stdlib HTTP server that receives inbound SMS from a provider webhook
(Twilio by default), runs the SHARED SatGPT dispatcher (ChatGPT with web search +
memory, `loc:` logging, `to:` relay), and sends the reply back through the
provider's API. Unlike the iMessage version there's no chat.db, AppleScript,
Full Disk Access, or Apple ID — this runs on any server Twilio can reach.

It imports satrelay.py so there's one brain; only the transport differs.

Config lives in the same ~/.satrelay/config.json, plus these keys:
    "twilio_account_sid": "AC...",
    "twilio_auth_token":  "...",      # you set this — never share it
    "twilio_from_number": "+1...",    # your Twilio number
    "sms_port": 8080,
    "public_url": "https://<host>/sms",   # your exact webhook URL
    "verify_twilio_signature": false      # flip true once public_url is set
Security still uses `allowed_handles` (your phone #) and/or `trigger_keyword`.

Run:   python3 satgpt_sms.py
Twilio number → "A message comes in" → Webhook (HTTP POST) → https://<host>/sms
Local testing: expose the port with e.g.  `ngrok http 8080`.
"""

import base64
import hashlib
import hmac
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import satrelay as sr   # reuse the dispatcher brain


# ---------------------------------------------------------------------------
# Twilio transport
# ---------------------------------------------------------------------------

def send_sms(cfg, to: str, body: str):
    """Send an SMS via Twilio's REST API (HTTP Basic auth).

    Prefers a Messaging Service (recommended for US A2P deliverability) when
    twilio_messaging_service_sid is set; otherwise sends from twilio_from_number.
    """
    acct = cfg.get("twilio_account_sid")            # AC… — identifies the account (URL path)
    user = cfg.get("twilio_api_key_sid") or acct    # API Key SID if set, else account SID
    pw = cfg.get("twilio_api_key_secret") or cfg.get("twilio_auth_token")
    frm = cfg.get("twilio_from_number")
    msvc = cfg.get("twilio_messaging_service_sid")
    if not (acct and user and pw and (msvc or frm)):
        sr.log("SMS not configured — need twilio_account_sid (AC…), auth "
               "(API key secret or auth_token), and a messaging_service_sid or from_number")
        return
    fields = {"To": to, "Body": body[:1500]}
    if msvc:
        fields["MessagingServiceSid"] = msvc
    else:
        fields["From"] = frm
    url = f"https://api.twilio.com/2010-04-01/Accounts/{acct}/Messages.json"
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except Exception as e:
        sr.log("Twilio send error:", repr(e))


def valid_signature(cfg, params: dict, header_sig: str) -> bool:
    """Validate Twilio's X-Twilio-Signature over the exact public_url + params."""
    tok = cfg.get("twilio_auth_token", "")
    url = cfg.get("public_url", "")
    if not (tok and url and header_sig):
        return False
    s = url + "".join(k + params[k] for k in sorted(params))
    mac = hmac.new(tok.encode(), s.encode("utf-8"), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), header_sig)


# ---------------------------------------------------------------------------
# Dispatch (reuses satrelay's commands; relay goes out over SMS)
# ---------------------------------------------------------------------------

def handle_sms(cfg, state, sender: str, text: str) -> str:
    if sr.RE_HELP.match(text):
        return sr.HELP_TEXT
    if sr.RE_RESET.match(text):
        state.get("history", {}).pop(sr.norm(sender), None)
        return "Memory cleared."

    m = sr.RE_LOC.match(text)
    if m:
        parsed = sr.parse_coords(m.group(1))
        if not parsed:
            return "Send coords like: loc: 43.39, -74.71 note"
        return sr.log_location(cfg, *parsed)

    m = sr.RE_RELAY.match(text)
    if m:
        nick = m.group(1).strip()
        contacts = {k.lower(): v for k, v in cfg.get("relay_contacts", {}).items()}
        target = contacts.get(nick.lower())
        if not target:
            known = ", ".join(cfg.get("relay_contacts", {}).keys()) or "(none saved)"
            return f"No contact '{nick}'. Known: {known}"
        send_sms(cfg, target, m.group(2).strip())
        return f"Sent to {nick}."

    # Default: ChatGPT (with web search) + short-term memory, keyed by sender.
    key = sr.norm(sender)
    history = state.setdefault("history", {}).setdefault(key, [])
    reply = sr.ask_openai(cfg, history, text)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    limit = max(0, cfg["memory_turns"]) * 2
    if len(history) > limit:
        del history[:-limit]
    return reply


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SatGPT SMS server is running. POST your provider webhook to /sms.")

    def do_POST(self):
        if self.path.split("?")[0] != "/sms":
            self.send_response(404); self.end_headers(); return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        params = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        sig = self.headers.get("X-Twilio-Signature", "")
        # Reply to Twilio immediately with empty TwiML so it doesn't wait on GPT.
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(b"<Response></Response>")
        threading.Thread(target=self._process, args=(params, sig), daemon=True).start()

    def _process(self, params, sig):
        cfg = sr.load_config()
        sender = params.get("From", "")
        text = (params.get("Body", "") or "").strip()

        if cfg.get("verify_twilio_signature") and not valid_signature(cfg, params, sig):
            sr.log("SMS rejected: bad Twilio signature")
            return
        if cfg.get("allowed_handles") and not sr.is_allowed(sender, cfg["allowed_handles"]):
            sr.log(f"SMS ignored (not allow-listed): {sender}")
            return
        payload = sr.strip_trigger(text, cfg.get("trigger_keyword", ""))
        if payload is None:          # keyword gate not matched
            return
        if not payload:
            payload = "help"

        sr.log(f"SMS IN <{sender}>: {text!r}")
        state = sr.load_state()
        try:
            reply = handle_sms(cfg, state, sender, payload)
        except Exception as e:
            reply = f"[error] {e}"
            sr.log("handler error:", repr(e))
        sr.save_state(state)
        if reply and reply.strip():
            sr.log(f"SMS OUT <{sender}>: {reply!r}")
            send_sms(cfg, sender, reply)

    def log_message(self, *a):       # keep the console quiet
        pass


def main():
    cfg = sr.load_config()
    port = int(cfg.get("sms_port", 8080))
    if not cfg.get("verify_twilio_signature"):
        sr.log("WARNING: Twilio signature verification OFF — set public_url + "
               "verify_twilio_signature=true before exposing this publicly.")
    sr.log(f"SatGPT SMS server listening on :{port} (POST /sms)")
    ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
