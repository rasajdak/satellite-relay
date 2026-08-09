#!/usr/bin/env bash
# sat-relay status dashboard — runs ON the host Mac (your-mac).
# Safe to run anytime; read-only.
echo "===== sat-relay status @ $(date '+%Y-%m-%d %H:%M:%S') ====="

if pgrep -f satrelay.py >/dev/null; then
  echo "daemon     : RUNNING (pid $(pgrep -f satrelay.py | tr '\n' ' '))"
else
  echo "daemon     : not running"
fi
launchctl list 2>/dev/null | grep -q satrelay && echo "launchd    : loaded" || echo "launchd    : not loaded"
pgrep -x imagent >/dev/null && echo "imagent    : running" || echo "imagent    : NOT running"

LOG="$HOME/sat-relay/satrelay.log"
[ -f "$LOG" ] && echo "log        : $LOG ($(wc -l < "$LOG" | tr -d ' ') lines)" || echo "log        : (none yet)"

python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.satrelay/config.json")
try:
    c = json.load(open(p))
    k = c.get("openai_api_key", "")
    print("openai key :", "SET" if (k and k != "sk-REPLACE_ME") else "NOT SET")
    print("handles    :", ", ".join(c.get("allowed_handles", [])) or "(none)")
    print("model      :", c.get("openai_model"))
except Exception as e:
    print("config     : ERROR", e)
PY

python3 - <<'PY'
import sqlite3, os, tempfile, shutil, datetime, glob
src = os.path.expanduser("~/Library/Messages/chat.db")
try:
    d = tempfile.mkdtemp()
    for f in glob.glob(src + "*"):
        try: shutil.copy(f, d)
        except Exception: pass
    c = sqlite3.connect(os.path.join(d, "chat.db"))
    rows = c.execute("select m.ROWID,m.is_from_me,h.id,m.text,m.date from message m "
                     "left join handle h on m.handle_id=h.ROWID order by m.ROWID desc limit 3").fetchall()
    print("--- last 3 messages (chat.db, WAL-aware) ---")
    for x in rows:
        try: dt = (datetime.datetime(2001,1,1)+datetime.timedelta(seconds=(x[4] or 0)/1e9)).strftime("%m-%d %H:%M")
        except Exception: dt = "?"
        print("   #%s %s from_me=%s %s %r" % (x[0], dt, x[1], x[2], (x[3] or "")[:40]))
    if not rows: print("   (no messages)")
    shutil.rmtree(d, ignore_errors=True)
except Exception as e:
    print("chat.db    : cannot read (%s) — Full Disk Access not granted yet?" % e)
PY

echo "--- last 15 log lines ---"
[ -f "$LOG" ] && tail -n 15 "$LOG" || echo "(no log yet)"
