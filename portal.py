#!/usr/bin/env python3
"""
Web portal — a small page to send a message to the owner's phone.

Someone opens https://<host>/send, types a message (+ a passphrase gate),
and it's delivered to `relay_to_number`. The transport (Twilio SMS or Sendblue
iMessage, per sms_provider) lives in satgpt_sms.py; this module owns the HTML,
input validation, and rate limiting, and hands back a (to, body) for sending.

Config keys (in ~/.satrelay/sms.json):
    "portal_enabled":    true,
    "portal_passphrase": "<shared word people must enter; '' = open>",
    "portal_title":      "Message Ryan",         # optional heading
    "portal_rate_limit": 6,                        # max sends per hour (global)

Served at /send behind Caddy. Owner phone comes from `relay_to_number`.
"""

import html
import time

import satrelay as sr   # helpers only (log, state shape)

MAX_MSG = 600           # chars accepted from the form
MAX_NAME = 40


def _page(cfg, *, name="", message="", error="", sent=False):
    title = html.escape(cfg.get("portal_title", "Send a message"))
    if sent:
        body = ("<div class='card ok'><h1>Sent ✓</h1>"
                "<p>Your message is on its way to the phone. "
                "Reception may be delayed if they're off-grid on satellite.</p>"
                "<p><a href='/send'>Send another</a></p></div>")
    else:
        need_pass = bool(cfg.get("portal_passphrase"))
        err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
        passfield = (
            "<label>Passphrase<input name='passphrase' type='password' "
            "autocomplete='off' required></label>" if need_pass else "")
        body = f"""
        <form class='card' method='post' action='/send'>
          <h1>{title}</h1>
          <p class='muted'>This sends a short message straight to the phone
          — works over cell, and over satellite when they're off-grid.
          Satellite messages are limited to 250 characters, so keep it short.</p>
          {err}
          <label>Your name (optional)
            <input name='name' maxlength='{MAX_NAME}' value='{html.escape(name)}'
             placeholder='e.g. Mom'></label>
          <label>Message
            <textarea name='message' maxlength='{MAX_MSG}' rows='4' required
             placeholder='Keep it under 250 characters — satellite is low-bandwidth.'>{html.escape(message)}</textarea>
          </label>
          {passfield}
          <button type='submit'>Send</button>
        </form>"""
    return f"""<!doctype html><html lang='en'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{title}</title>
<style>
  :root{{color-scheme:light dark}}
  *{{box-sizing:border-box}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
       margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0f1f17;color:#eaf2ec;padding:20px}}
  .card{{background:#16281f;border:1px solid #2c463a;border-radius:16px;padding:26px 22px;
        width:100%;max-width:420px;box-shadow:0 10px 40px rgba(0,0,0,.35)}}
  h1{{margin:0 0 6px;font-size:1.4rem}}
  .muted{{color:#9db6a8;font-size:.9rem;margin:0 0 16px}}
  .err{{color:#ff9c8a;font-size:.9rem;margin:0 0 12px}}
  label{{display:block;font-size:.85rem;color:#bcd3c6;margin:12px 0 4px}}
  input,textarea{{width:100%;padding:11px 12px;border-radius:10px;border:1px solid #33513f;
        background:#0f1f17;color:#eaf2ec;font-size:1rem;font-family:inherit}}
  textarea{{resize:vertical}}
  button{{margin-top:18px;width:100%;padding:13px;border:0;border-radius:10px;
        background:#3fae6d;color:#04150c;font-size:1.05rem;font-weight:600;cursor:pointer}}
  button:hover{{background:#49c47b}}
  .ok h1{{color:#7be0a3}}
  a{{color:#7be0a3}}
</style></head><body>{body}</body></html>"""


def render(cfg, error="", name="", message=""):
    return _page(cfg, name=name, message=message, error=error)


def render_sent(cfg):
    return _page(cfg, sent=True)


def _rate_ok(cfg, state):
    """Global rate limit so the form can't be used to spam the phone."""
    limit = int(cfg.get("portal_rate_limit", 6))
    now = time.time()
    hits = [t for t in state.get("portal_sends", []) if now - t < 3600]
    if len(hits) >= limit:
        state["portal_sends"] = hits
        return False
    hits.append(now)
    state["portal_sends"] = hits
    return True


def handle(cfg, state, params):
    """Validate a submitted form. Returns (send, error, name, message).

    `send` is (to_number, sms_body) for satgpt_sms to actually send, or None.
    `error` is "" on success or a message to show. The caller decides how to
    render (HTML page or JSON), so this stays transport/format agnostic.
    """
    name = (params.get("name", "") or "").strip()[:MAX_NAME]
    message = (params.get("message", "") or "").strip()[:MAX_MSG]
    passphrase = (params.get("passphrase", "") or "").strip()

    want = cfg.get("portal_passphrase", "")
    if want and passphrase != want:
        return None, "Wrong passphrase.", name, message
    if not message:
        return None, "Message can't be empty.", name, message

    to = cfg.get("relay_to_number")
    if not to:
        return None, "Portal isn't configured (no destination number).", name, message
    if not _rate_ok(cfg, state):
        return None, "Too many messages just now — try again in a bit.", name, message

    who = f" from {name}" if name else ""
    body = f"\U0001f4e1 Portal msg{who}: {message}"
    return (to, body), "", name, message
