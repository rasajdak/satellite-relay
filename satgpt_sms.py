#!/usr/bin/env python3
"""
SatGPT (SMS) — a text-message front-end for SatGPT, no Mac required.

A tiny stdlib HTTP server that receives inbound SMS from a provider webhook
(Twilio by default), runs the SHARED SatGPT dispatcher (ChatGPT with web search +
memory, `loc:` logging, `to:` relay), and sends the reply back through the
provider's API. Unlike the iMessage version there's no chat.db, AppleScript,
Full Disk Access, or Apple ID — this runs on any server Twilio can reach.

It imports satrelay.py so there's one brain; only the transport differs.

Two interchangeable transports; pick with "sms_provider" ("twilio" | "sendblue"):

  Twilio (SMS, green bubbles):
    "twilio_account_sid": "AC...",
    "twilio_auth_token":  "...",      # you set this — never share it
    "twilio_from_number": "+1...",    # your Twilio number
    "public_url": "https://<host>/sms",   # your exact webhook URL
    "verify_twilio_signature": false      # flip true once public_url is set

  Sendblue (iMessage, blue bubbles, NO Mac / chat.db / Apple ID):
    "sendblue_api_key_id": "...",
    "sendblue_api_secret": "...",     # you set this — never share it
    "sendblue_from_number": "",       # leave blank on a shared/trial line
    "sendblue_webhook_secret": "",    # optional; set verify_sendblue_secret true to enforce
  Sendblue dashboard → Webhooks → "receive" → https://<host>/sendblue

Shared to both: "sms_port": 8080. Security uses `allowed_handles` (your phone #)
and/or `trigger_keyword`; outbound always follows `sms_provider`.

Run:   python3 satgpt_sms.py
Local testing: expose the port with e.g.  `ngrok http 8080`.
"""

import base64
import hashlib
import hmac
import json
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import satrelay as sr   # reuse the dispatcher brain
import messenger        # Facebook Page <-> SMS relay (Meta transport lives here)
import portal           # web portal: /send form -> SMS to the owner


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


# ---------------------------------------------------------------------------
# Sendblue transport (iMessage — no Mac, chat.db, or Apple ID required)
# ---------------------------------------------------------------------------

def send_imessage(cfg, to: str, body: str):
    """Send an iMessage via Sendblue's REST API.

    On a shared/trial line leave sendblue_from_number blank and Sendblue picks
    the outbound number; on a dedicated line set it to your own number.
    """
    kid = cfg.get("sendblue_api_key_id")
    secret = cfg.get("sendblue_api_secret")
    if not (kid and secret):
        sr.log("Sendblue not configured — need sendblue_api_key_id and sendblue_api_secret")
        return
    payload = {"number": to, "content": body[:1500]}
    frm = cfg.get("sendblue_from_number")
    if frm:                      # omit on a shared line; required on a dedicated line
        payload["from_number"] = frm
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.sendblue.com/api/send-message", data=data, method="POST")
    req.add_header("sb-api-key-id", kid)
    req.add_header("sb-api-secret-key", secret)
    req.add_header("Content-Type", "application/json")
    # Cloudflare fronts the API and 403s the default "Python-urllib" UA (err 1010).
    req.add_header("User-Agent", "satgpt-relay/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except Exception as e:
        sr.log("Sendblue send error:", repr(e))


def send_text(cfg, to: str, body: str):
    """Provider-agnostic outbound. Follows cfg['sms_provider']; Twilio default."""
    if cfg.get("sms_provider") == "sendblue":
        send_imessage(cfg, to, body)
    else:
        send_sms(cfg, to, body)


def valid_sendblue_secret(cfg, headers: dict) -> bool:
    """Verify the shared secret Sendblue echoes in a configured header.

    The header name isn't fixed in Sendblue's docs, so we check the configured
    name (default 'sb-signing-secret', case-insensitively) and fall back to
    scanning all header values for the exact secret.
    """
    want = cfg.get("sendblue_webhook_secret", "")
    if not want:
        return False
    name = (cfg.get("sendblue_secret_header") or "sb-signing-secret").lower()
    lowered = {k.lower(): v for k, v in headers.items()}
    got = lowered.get(name)
    if got and hmac.compare_digest(got, want):
        return True
    return any(hmac.compare_digest(v, want) for v in headers.values())


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
        send_text(cfg, target, m.group(2).strip())
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
        if self.path.split("?")[0] == "/messenger":
            # Meta's webhook verification handshake (echo hub.challenge if the token matches).
            cfg = sr.load_config()
            query = urllib.parse.urlparse(self.path).query
            params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
            challenge = messenger.verify_webhook(params, cfg)
            if challenge is not None:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(challenge.encode())
            else:
                self.send_response(403); self.end_headers()
            return
        if self.path.split("?")[0] == "/send":
            cfg = sr.load_config()
            if not cfg.get("portal_enabled", True):
                self.send_response(404); self.end_headers(); return
            self._html(portal.render(cfg))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SatGPT server is running. POST Twilio to /sms, "
                         b"Sendblue to /sendblue, Meta to /messenger.")

    def _html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if path == "/messenger":
            # Meta posts JSON signed with X-Hub-Signature-256; ack fast, process async.
            sig = self.headers.get("X-Hub-Signature-256", "")
            self.send_response(200); self.end_headers()
            threading.Thread(target=self._process_messenger, args=(raw, sig), daemon=True).start()
            return

        if path == "/sms":
            params = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
            sig = self.headers.get("X-Twilio-Signature", "")
            # Reply to Twilio immediately with empty TwiML so it doesn't wait on GPT.
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.end_headers()
            self.wfile.write(b"<Response></Response>")
            threading.Thread(target=self._process, args=(params, sig), daemon=True).start()
            return

        if path == "/sendblue":
            # Sendblue posts JSON; ack fast, process async like Meta's webhook.
            self.send_response(200); self.end_headers()
            threading.Thread(target=self._process_sendblue,
                             args=(raw, dict(self.headers)), daemon=True).start()
            return

        if path == "/send":
            cfg = sr.load_config()
            if not cfg.get("portal_enabled", True):
                self.send_response(404); self.end_headers(); return
            params = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
            state = sr.load_state()
            send, error, name, message = portal.handle(cfg, state, params)
            sr.save_state(state)
            if params.get("fmt") == "json":          # embedded fetch (e.g. from the camp site)
                self._json({"ok": bool(send), "error": error})
            elif send:
                self._html(portal.render_sent(cfg))
            else:
                self._html(portal.render(cfg, error, name, message))
            if send:
                to, body = send
                sr.log(f"PORTAL -> {to}: {body!r}")
                send_text(cfg, to, body)
            return

        self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        # CORS preflight (harmless for simple requests; present for completeness).
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _cors(self):
        # Reflect any ryansajdak.com origin (apex/www/subdomain) — the camp page
        # is served from both apex and www, and a fixed apex-only value made
        # www loads fail the browser's response read (POST still went through).
        origin = self.headers.get("Origin", "")
        allowed = "https://ryansajdak.com"
        host = urllib.parse.urlparse(origin).hostname or ""
        if host == "ryansajdak.com" or host.endswith(".ryansajdak.com"):
            allowed = origin
        self.send_header("Access-Control-Allow-Origin", allowed)
        self.send_header("Vary", "Origin")

    def _json(self, obj, code=200):
        import json as _json
        data = _json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _process(self, params, sig):
        """Twilio inbound: verify the signature, then run the shared brain."""
        cfg = sr.load_config()
        sender = params.get("From", "")
        text = (params.get("Body", "") or "").strip()
        if cfg.get("verify_twilio_signature") and not valid_signature(cfg, params, sig):
            sr.log("SMS rejected: bad Twilio signature")
            return
        self._handle_inbound(cfg, sender, text)

    def _process_sendblue(self, raw, headers):
        """Sendblue inbound: an iMessage arrived. Same brain, iMessage out.

        Sendblue POSTs JSON here for several event types; act only on inbound
        text (is_outbound falsy, non-empty content) and ignore the delivery
        status callbacks echoing our own replies.
        """
        cfg = sr.load_config()
        if cfg.get("verify_sendblue_secret") and not valid_sendblue_secret(cfg, headers):
            sr.log("Sendblue rejected: bad secret")
            return
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:
            sr.log("Sendblue parse error:", repr(e))
            return
        if data.get("is_outbound"):          # status callback for a message we sent
            return
        sender = data.get("from_number", "")
        text = (data.get("content", "") or "").strip()
        if not (sender and text):            # media-only or a non-message event
            return
        self._handle_inbound(cfg, sender, text)

    def _handle_inbound(self, cfg, sender, text):
        """Provider-neutral core: allow-list, FB-reply route, trigger gate, then
        the SatGPT reply. Outbound goes through send_text() (follows sms_provider)."""
        if cfg.get("allowed_handles") and not sr.is_allowed(sender, cfg["allowed_handles"]):
            sr.log(f"IN ignored (not allow-listed): {sender}")
            return

        # "#code ..." from your phone -> route back into that Messenger thread.
        m = messenger.RE_REPLY.match(text)
        if m:
            state = sr.load_state()
            ack = messenger.route_reply(cfg, state, m.group(1), m.group(2).strip())
            sr.save_state(state)
            sr.log(f"FB reply <{sender}>: {text!r} => {ack}")
            if cfg.get("relay_ack", True) and ack:
                send_text(cfg, sender, ack)
            return

        payload = sr.strip_trigger(text, cfg.get("trigger_keyword", ""))
        if payload is None:          # keyword gate not matched
            return
        if not payload:
            payload = "help"

        sr.log(f"IN <{sender}>: {text!r}")
        state = sr.load_state()
        try:
            reply = handle_sms(cfg, state, sender, payload)
        except Exception as e:
            reply = f"[error] {e}"
            sr.log("handler error:", repr(e))
        sr.save_state(state)
        if reply and reply.strip():
            sr.log(f"OUT <{sender}>: {reply!r}")
            send_text(cfg, sender, reply)

    def _process_messenger(self, raw, sig):
        cfg = sr.load_config()
        if cfg.get("verify_fb_signature", True) and not messenger.valid_signature(cfg, raw, sig):
            sr.log("Messenger rejected: bad X-Hub-Signature-256")
            return
        try:
            events = messenger.parse_events(raw.decode("utf-8", "replace"))
        except Exception as e:
            sr.log("Messenger parse error:", repr(e))
            return
        if not events:
            return
        state = sr.load_state()
        for psid, text in events:
            to, body = messenger.inbound_to_sms(cfg, state, psid, text)
            if not to:
                sr.log(f"Messenger dropped ({body}): {text!r} from {psid}")
                continue
            sr.log(f"FB IN <{psid}>: {text!r} -> SMS {to}")
            send_text(cfg, to, body)
        sr.save_state(state)

    def log_message(self, *a):       # keep the console quiet
        pass


def main():
    cfg = sr.load_config()
    port = int(cfg.get("sms_port", 8080))
    provider = cfg.get("sms_provider", "twilio")
    if provider == "twilio" and not cfg.get("verify_twilio_signature"):
        sr.log("WARNING: Twilio signature verification OFF — set public_url + "
               "verify_twilio_signature=true before exposing this publicly.")
    if provider == "sendblue" and not cfg.get("verify_sendblue_secret"):
        sr.log("NOTE: Sendblue secret verification OFF — allowed_handles is the "
               "security boundary. Set sendblue_webhook_secret + verify_sendblue_secret "
               "true to also verify the webhook.")
    sr.log(f"SatGPT server listening on :{port} (provider={provider}; "
           f"Twilio /sms, Sendblue /sendblue, Meta /messenger)")
    ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
