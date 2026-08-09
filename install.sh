#!/usr/bin/env bash
# satrelay installer — run this ON the always-on Mac that will host the relay.
# It auto-detects python3 and paths, scaffolds the config, and generates a
# launchd plist. The GUI-only steps (Messages sign-in, Full Disk Access,
# Automation) it can't do for you — it prints them at the end.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.example.satrelay"
CFG_DIR="$HOME/.satrelay"
LA_DIR="$HOME/Library/LaunchAgents"
PLIST_OUT="$SCRIPT_DIR/$LABEL.plist"

echo "sat-relay installer"
echo "  project dir : $SCRIPT_DIR"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "ERROR: this only runs on macOS (needs Messages + chat.db + AppleScript)." >&2
  exit 1
fi

PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "ERROR: python3 not found. Install it ('brew install python' or python.org)," >&2
  echo "       then re-run this script." >&2
  exit 1
fi
echo "  python3     : $PY ($("$PY" --version 2>&1))"

# 1. Config scaffold (never clobber an existing config).
mkdir -p "$CFG_DIR"
if [[ -f "$CFG_DIR/config.json" ]]; then
  echo "  config      : $CFG_DIR/config.json (exists — leaving as-is)"
else
  cp "$SCRIPT_DIR/config.example.json" "$CFG_DIR/config.json"
  echo "  config      : created $CFG_DIR/config.json  <-- EDIT: OpenAI key + allowed_handles"
fi

# 2. Generate the launchd plist with paths correct for THIS Mac.
cat > "$PLIST_OUT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$SCRIPT_DIR/satrelay.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$SCRIPT_DIR/satrelay.log</string>
    <key>StandardErrorPath</key><string>$SCRIPT_DIR/satrelay.log</string>
</dict>
</plist>
PLIST
echo "  launchd     : wrote $PLIST_OUT"

cat <<EOF

------------------------------------------------------------------
Remaining steps (GUI — do these by hand on THIS Mac):

  1. Messages ▸ Settings ▸ iMessage — sign in as you@example.com.
     (Only ONE Mac should be signed in + running the relay at a time.)

  2. System Settings ▸ Privacy & Security ▸ Full Disk Access — add:
        $PY
     (⌘⇧G in the file picker, paste that path.) Then quit/reopen Terminal.

  3. Edit your config:
        open -e $CFG_DIR/config.json
     Add your OpenAI key and your iPhone's phone/email to allowed_handles.

  4. Test in the foreground first:
        "$PY" "$SCRIPT_DIR/satrelay.py"
     Text this Mac from your phone; click OK on the Automation prompt.

  5. Run it forever:
        mkdir -p "$LA_DIR"
        cp "$PLIST_OUT" "$LA_DIR/"
        launchctl load -w "$LA_DIR/$LABEL.plist"
     Stop with:
        launchctl unload "$LA_DIR/$LABEL.plist"

  6. Keep it awake: System Settings ▸ Battery/Displays — prevent sleep,
     or launch under 'caffeinate'.
------------------------------------------------------------------
EOF
