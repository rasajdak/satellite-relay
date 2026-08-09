#!/usr/bin/env python3
"""
SatGPT — bridge satellite iMessages to ChatGPT (and tools) and back.

Runs on an always-on Mac signed into a DEDICATED Apple ID. Watches the
Messages database for incoming texts from your allowed handle(s), routes them
through a small command dispatcher (relay / memory / web-aware GPT), and
replies over iMessage. Designed for Apple "Messages via satellite": replies
are kept short and chunked because bandwidth is tiny.

Field commands (case-insensitive; the leading word is the command):
    help                     -> list commands
    to <nick>: <message>     -> relay an iMessage to a saved contact
                               (also: msg / tell / relay <nick>: ...)
    reset                    -> clear conversation memory
    <anything else>          -> ChatGPT, with short-term memory

Zero third-party dependencies — stdlib only.

Requires:
  * Full Disk Access for whatever runs this (Terminal, or the launchd label).
  * Automation permission to control Messages (macOS prompts on first send).
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOME = Path.home()
CONFIG_PATH = Path(os.environ.get("SATRELAY_CONFIG", HOME / ".satrelay" / "config.json"))
STATE_PATH = Path(os.environ.get("SATRELAY_STATE", HOME / ".satrelay" / "state.json"))
CHAT_DB = HOME / "Library" / "Messages" / "chat.db"

DEFAULTS = {
    # The wake word: a message must start with this (case-insensitive) to
    # trigger SatGPT, e.g. "satgpt how do I...". Stripped before dispatch.
    # Empty = no keyword gate (rely on allowed_handles instead).
    "trigger_keyword": "satgpt",
    # Optional allow-list. If non-empty, only these handles are answered — an
    # extra gate on top of the keyword. Empty = anyone with the keyword.
    # Phone numbers match on their last 10 digits; emails are lowercased.
    "allowed_handles": [],
    "openai_api_key": "",
    "openai_model": "gpt-5.6",             # OpenAI frontier model ("Sol"; alias gpt-5.6)
    "reasoning_effort": "low",             # none|minimal|low|medium|high|xhigh|max —
                                           # low = fast & terse; raise for harder Qs.
                                           # "" omits it (for non-reasoning models).
    "max_completion_tokens": 2000,         # caps reasoning + visible output tokens
    "system_prompt": (
        "You are SatGPT, answering someone texting over a low-bandwidth "
        "satellite link who may be off-grid or in an emergency. Reply in plain "
        "text — no markdown, and NEVER include URLs, links, or citations of any "
        "kind. Keep it concise: at most a few short sentences. Lead with the "
        "answer. If a question needs current or real-time information, search "
        "the web and then state the facts plainly (still no links). If a "
        "question is ambiguous, give your single best guess rather than asking "
        "to clarify."
    ),
    "web_search": True,         # let the model look up current info when needed
    # Nickname -> handle map for the `to <nick>:` relay command. Only these
    # contacts can be messaged, so a garbled field command can't spam numbers.
    "relay_contacts": {},
    # Where `loc:` writes location pings. For a Firebase Realtime Database
    # (like the camp site), set type "firebase" + your DB url + node path.
    # Leave database_url empty to disable the loc command.
    "location_sink": {
        "type": "firebase",
        "database_url": "",   # e.g. https://YOUR-PROJECT-default-rtdb.firebaseio.com
        "path": "your-app/your-node/track",
    },
    "memory_turns": 6,          # how many prior user/assistant msgs to keep
    "poll_seconds": 4,
    # Texting yourself (single-account "Note to Self") records a message twice:
    # a sent copy (is_from_me=1) and a self-received copy (is_from_me=0).
    # Collapse identical messages seen within this many seconds so each query is
    # answered once.
    "dedup_seconds": 45,
    "max_reply_chars": 900,     # total; split into chunks below
    "chunk_chars": 300,         # per iMessage segment
    "reply_prefix": "",         # e.g. "GPT: " to mark bot replies
    "openai_timeout": 90,      # web search + reasoning can take a bit
    "web_timeout": 15,
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    if os.environ.get("OPENAI_API_KEY"):
        cfg["openai_api_key"] = os.environ["OPENAI_API_KEY"]
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"last_rowid": 0, "history": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Handle matching
# ---------------------------------------------------------------------------

def norm(handle: str) -> str:
    """Normalize a handle. Phone -> last 10 digits; email lowercased."""
    if not handle:
        return ""
    if "@" in handle:
        return handle.strip().lower()
    digits = re.sub(r"\D", "", handle)
    return digits[-10:] if len(digits) >= 10 else digits


def is_allowed(handle: str, allowed) -> bool:
    n = norm(handle)
    return any(n and n == norm(a) for a in allowed)


REPLY_MARKER = "\U0001F6F0"  # 🛰  prepended to every reply; inbound lines that
                             # start with it are skipped so the relay never
                             # answers its own message (loop protection).


def strip_trigger(text: str, keyword: str):
    """Gate a message on the trigger keyword.

    If `keyword` is set, the message must start with it (case-insensitive, on a
    word boundary); returns the remainder with the keyword and any trailing
    separators stripped, or None if it doesn't match. If no keyword is
    configured, returns the text unchanged (no gate).
    """
    if not keyword:
        return text
    m = re.match(rf"^\s*{re.escape(keyword)}\b[\s:,\-]*(.*)$", text,
                 re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Reading the Messages DB
# ---------------------------------------------------------------------------

def decode_attributed_body(blob: bytes) -> str:
    """Extract the message text from a typedstream NSAttributedString blob.

    Modern Messages stores the body only in `attributedBody`. The text sits in
    a length-prefixed UTF-8 run right after the NSString class marker:

        ... NSString <class bytes> 2b <len> <utf-8 bytes> ...

    where 0x2b ('+') is the typedstream marker for length-prefixed bytes and
    <len> is a single byte, or 0x81 followed by a little-endian uint16 for
    lengths >= 128. Reading the explicit length (rather than regex-scanning a
    printable run) avoids two bugs: grabbing trailing framing bytes as garbage,
    and truncating/dropping very short messages.
    """
    if not blob:
        return ""
    try:
        idx = blob.find(b"NSString")
        if idx == -1:
            return ""
        # The '+' length marker sits a few bytes past the class name; bound the
        # search so we can't accidentally latch onto a '+' inside the message.
        plus = blob.find(b"\x2b", idx, idx + 16)
        if plus == -1:
            return ""
        p = plus + 1
        if p >= len(blob):
            return ""
        length = blob[p]
        p += 1
        if length == 0x81:  # 2-byte little-endian length follows
            if p + 2 > len(blob):
                return ""
            length = int.from_bytes(blob[p:p + 2], "little")
            p += 2
        if length <= 0 or p + length > len(blob):
            return ""
        return blob[p:p + length].decode("utf-8", "replace").strip()
    except Exception:
        return ""


def fetch_new_messages(last_rowid: int):
    """Return [(rowid, handle, is_from_me, text)] for messages newer than last_rowid.

    Reads BOTH directions: inbound (is_from_me=0) for the dedicated-account
    setup, and your own outbound (is_from_me=1) so the relay also works from a
    SINGLE Apple ID via a "Note to Self" thread. Loop protection is the trigger
    keyword (the relay's own replies never carry it) plus the REPLY_MARKER skip
    in the main loop.
    """
    # Open read-WRITE (not immutable) so SQLite reads the -wal file where
    # Messages keeps recent, un-checkpointed messages. immutable=1 hides them,
    # and a plain ?mode=ro can't open a WAL database. query_only guarantees
    # we never actually write.
    conn = sqlite3.connect(f"file:{CHAT_DB}?mode=rw", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only=1")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT m.ROWID AS rowid, h.id AS handle, m.is_from_me AS fromme,
                   m.text AS text, m.attributedBody AS body
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID > ?
            ORDER BY m.ROWID ASC
            """,
            (last_rowid,),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        text = (r["text"] or "").strip()
        if not text and r["body"]:
            text = decode_attributed_body(r["body"])
        out.append((r["rowid"], r["handle"], r["fromme"], text))
    return out


# ---------------------------------------------------------------------------
# Sending via Messages / AppleScript
# ---------------------------------------------------------------------------

def applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send_imessage(handle: str, text: str):
    """Send an iMessage to `handle` via the Messages app."""
    script = f'''
    on run
        set targetHandle to "{applescript_escape(handle)}"
        set msg to "{applescript_escape(text)}"
        tell application "Messages"
            try
                set svc to 1st account whose service type = iMessage
                set theBuddy to participant targetHandle of svc
                send msg to theBuddy
            on error
                send msg to buddy targetHandle of (1st service whose service type is iMessage)
            end try
        end tell
    end run
    '''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)


def chunk(text: str, size: int):
    text = (text or "").strip()
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


# ---------------------------------------------------------------------------
# Tools: location logging (Firebase Realtime Database REST write)
# ---------------------------------------------------------------------------

COORD_RE = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)")


def parse_coords(s: str):
    """Pull (lat, lon, note) out of free text; None if no valid pair found."""
    m = COORD_RE.search(s or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    note = (s[:m.start()] + " " + s[m.end():]).strip(" ,")
    return lat, lon, note


def log_location(cfg, lat: float, lon: float, note: str) -> str:
    sink = cfg.get("location_sink") or {}
    if sink.get("type") != "firebase" or not sink.get("database_url"):
        return "Location logging isn't configured."
    url = sink["database_url"].rstrip("/") + "/" + sink["path"].strip("/") + ".json"
    payload = {
        "lat": lat, "lon": lon, "note": note,
        "t": int(time.time() * 1000), "src": "sat",
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg["web_timeout"]) as resp:
        resp.read()
    tail = f" ({note})" if note else ""
    return f"Logged {lat:.5f},{lon:.5f} to camp map.{tail}"


# ---------------------------------------------------------------------------
# OpenAI (with short-term memory)
# ---------------------------------------------------------------------------

def ask_openai(cfg, history, prompt: str) -> str:
    # Uses the Responses API so GPT-5 reasoning models can call the built-in
    # web_search tool when a question needs current info (the model decides).
    # System prompt -> `instructions`; conversation memory -> the `input` list.
    payload = {
        "model": cfg["openai_model"],
        "instructions": cfg["system_prompt"],
        "input": list(history) + [{"role": "user", "content": prompt}],
        # Caps reasoning + visible output; leave headroom above the reply length.
        "max_output_tokens": cfg.get("max_completion_tokens", 2000),
    }
    effort = cfg.get("reasoning_effort", "")
    if effort:
        payload["reasoning"] = {"effort": effort}
    if cfg.get("web_search", True):
        payload["tools"] = [{"type": "web_search"}]
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg['openai_api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg["openai_timeout"]) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # The final answer lives in output[] items of type "message" -> content[]
    # items of type "output_text". (No top-level output_text in the raw JSON.)
    parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "satrelay commands:\n"
    "loc: <lat,lon> <note> — log to camp map\n"
    "to <name>: <msg> — relay a text\n"
    "reset — clear memory\n"
    "anything else — ask ChatGPT"
)

RE_LOC = re.compile(r"^\s*(?:loc|gps|pos|ping|here)\s*:\s*(.*)$", re.IGNORECASE)
RE_RELAY = re.compile(r"^\s*(?:to|msg|tell|relay)\s+([^:]+?)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
RE_HELP = re.compile(r"^\s*(?:help|commands|\?)\s*$", re.IGNORECASE)
RE_RESET = re.compile(r"^\s*(?:reset|clear|forget)\s*$", re.IGNORECASE)


def relay_message(cfg, nick: str, message: str) -> str:
    contacts = {k.lower(): v for k, v in cfg.get("relay_contacts", {}).items()}
    target = contacts.get(nick.strip().lower())
    if not target:
        known = ", ".join(cfg.get("relay_contacts", {}).keys()) or "(none saved)"
        return f"No contact '{nick}'. Known: {known}"
    try:
        send_imessage(target, message)
        return f"Sent to {nick}."
    except subprocess.CalledProcessError as e:
        return f"[relay failed] {e.stderr.strip() if e.stderr else e}"


def handle_message(cfg, state, handle: str, text: str) -> str:
    """Route one inbound message to a command or ChatGPT; return the reply."""
    if RE_HELP.match(text):
        return HELP_TEXT

    if RE_RESET.match(text):
        state.get("history", {}).pop(norm(handle), None)
        return "Memory cleared."

    m = RE_LOC.match(text)
    if m:
        parsed = parse_coords(m.group(1))
        if not parsed:
            return "Send coords like: loc: 43.39, -74.71 at the ridge"
        return log_location(cfg, *parsed)

    m = RE_RELAY.match(text)
    if m:
        return relay_message(cfg, m.group(1), m.group(2).strip())

    # Default: ChatGPT with short-term memory, keyed by sender.
    key = norm(handle)
    history = state.setdefault("history", {}).setdefault(key, [])
    reply = ask_openai(cfg, history, text)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    # Keep only the last N messages (memory_turns pairs).
    limit = max(0, cfg["memory_turns"]) * 2
    if len(history) > limit:
        del history[:-limit]
    return reply


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def main():
    cfg = load_config()
    if not cfg["allowed_handles"] and not cfg.get("trigger_keyword"):
        log("ERROR: set a trigger_keyword and/or allowed_handles (need at least",
            "one gate so the relay isn't wide open). Edit", CONFIG_PATH)
        sys.exit(1)
    if not cfg["openai_api_key"]:
        log("ERROR: no openai_api_key configured. Edit", CONFIG_PATH)
        sys.exit(1)
    if not CHAT_DB.exists():
        log("ERROR: cannot find Messages DB at", CHAT_DB,
            "\n  -> Grant Full Disk Access to whatever runs this.")
        sys.exit(1)

    state = load_state()
    state.setdefault("history", {})
    if state.get("last_rowid", 0) == 0:
        try:
            newest = fetch_new_messages(0)
            state["last_rowid"] = newest[-1][0] if newest else 0
            save_state(state)
        except sqlite3.OperationalError as e:
            log("ERROR reading chat.db:", e, "\n  -> Likely missing Full Disk Access.")
            sys.exit(1)

    log("satrelay started. Trigger keyword:",
        repr(cfg.get("trigger_keyword") or "(none)"),
        "| allow-list:", cfg["allowed_handles"] or "(open)")
    log("Model:", cfg["openai_model"], "| relay contacts:",
        list(cfg.get("relay_contacts", {}).keys()) or "none")

    recent = {}  # raw text -> monotonic time, for de-duping self-thread echoes

    while True:
        try:
            msgs = fetch_new_messages(state["last_rowid"])
        except sqlite3.OperationalError as e:
            log("chat.db read error:", e)
            time.sleep(cfg["poll_seconds"])
            continue

        for rowid, handle, fromme, text in msgs:
            state["last_rowid"] = rowid
            # Skip empties and the relay's own replies (loop protection).
            if not text or text.startswith(REPLY_MARKER):
                save_state(state)
                continue
            # Optional allow-list gate (only enforced if configured).
            if cfg["allowed_handles"] and not is_allowed(handle, cfg["allowed_handles"]):
                save_state(state)
                continue
            # Keyword gate: message must start with the trigger keyword.
            payload = strip_trigger(text, cfg.get("trigger_keyword", ""))
            if payload is None:
                save_state(state)
                continue
            if not payload:
                payload = "help"   # bare keyword -> show the command list
            # Collapse the sent + self-received duplicate of a Note-to-Self
            # message so we don't answer (and bill for) the same query twice.
            now = time.monotonic()
            for k, t0 in list(recent.items()):
                if now - t0 > cfg.get("dedup_seconds", 45):
                    recent.pop(k, None)
            if text in recent:
                recent[text] = now
                log(f"SKIP dup <{handle}>: {text!r}")
                save_state(state)
                continue
            recent[text] = now
            log(f"IN  from_me={fromme} <{handle}>: {text!r}")
            try:
                reply = handle_message(cfg, state, handle, payload)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:200]
                reply = f"[relay error {e.code}] {body}"
                log("OpenAI HTTPError:", e.code, body)
            except Exception as e:
                reply = f"[relay error] {e}"
                log("handler error:", repr(e))
            save_state(state)

            reply = (reply or "")[: cfg["max_reply_chars"]]
            if not reply.strip():
                log(f"OUT <{handle}>: (empty reply, nothing sent)")
                continue
            if cfg["reply_prefix"]:
                reply = cfg["reply_prefix"] + reply
            reply = f"{REPLY_MARKER} {reply}"   # tag so we never re-process it
            log(f"OUT <{handle}>: {reply!r}")
            for part in chunk(reply, cfg["chunk_chars"]):
                if not part.strip():
                    continue
                try:
                    send_imessage(handle, part)
                except subprocess.CalledProcessError as e:
                    log("send error:", e.stderr.strip() if e.stderr else e)
                    break
                time.sleep(1)

        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
