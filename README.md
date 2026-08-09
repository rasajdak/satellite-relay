# satrelay — satellite iMessage ↔ ChatGPT bridge

Text ChatGPT from anywhere, even with no cell or wifi, using your iPhone's
**Messages via satellite**. An always-on Mac (signed into a *dedicated* Apple
ID) catches the message, asks OpenAI, and texts the answer back.

```
iPhone --(satellite iMessage)--> Mac [satrelay] --(OpenAI)--> reply --(satellite)--> iPhone
```

## How it works

`satrelay.py` polls the Messages database (`~/Library/Messages/chat.db`) for
new inbound texts from your allowed handle(s), runs them through a small
command dispatcher, and replies over iMessage via AppleScript. Zero
third-party dependencies — just Python 3 (stdlib).

Because satellite bandwidth is tiny, replies are capped and split into short
chunks, and the system prompt tells GPT to be terse.

## Field commands

Text these to the bot (the leading word is the command, case-insensitive):

| You send | What happens |
|---|---|
| `help` | Lists the commands. |
| `w: Denver CO` | Live weather. Also `wx:` / `weather:`. `w:` alone uses `default_location`. State/country disambiguates (`Buffalo NY` ≠ `Buffalo MN`); works worldwide. No API key. |
| `loc: 43.39, -74.71 at the ridge` | Logs a location ping to the camp map's breadcrumb trail. Also `gps:` / `here:`. Trailing text becomes the note. See "Auto-logging location" below. |
| `to dad: running late` | Relays an iMessage to a saved contact. Also `msg` / `tell` / `relay`. Only names in `relay_contacts` can be reached. |
| `reset` | Clears the conversation memory. |
| anything else | Goes to ChatGPT, which remembers the last few turns (`memory_turns`). |

Weather and memory need no extra keys; relay just needs the contact saved in
`relay_contacts`. All the internet fetching happens on the Mac — your phone
stays offline.

---

## Setting up on the host Mac (quick path)

Copy this whole folder to the always-on Mac (AirDrop, or `git clone` if you put
it in a repo), then run the installer — it auto-detects `python3` and writes a
launchd plist with the right paths for that machine:

```bash
cd sat-relay
./install.sh
```

It scaffolds `~/.satrelay/config.json` and prints the remaining GUI steps
(Messages sign-in, Full Disk Access, editing the config, loading launchd).
Only **one** Mac should be signed into the relay Apple Account and running the
daemon at a time. The manual walkthrough below covers the same steps in detail.

## One-time setup

### 1. The dedicated Apple Account for the Mac
This relay uses **`you@example.com`** as its own Apple Account (also the
standing technical/dev identity for app building). Create the Apple Account at
account.apple.com with that address if you haven't, then sign the *Mac* into
it — in **Messages ▸ Settings ▸ iMessage only** (do NOT sign it into the Mac's
System Settings / iCloud; leave your personal iCloud login intact). Keep your
iPhone on your normal Apple Account. Your phone iMessages `you@example.com`;
the bot replies to your phone. This avoids the "messaging yourself" reply loop.

Send a test iMessage from your phone to `you@example.com` and confirm a
**blue bubble** arrives in Messages on the Mac.

### 2. Grant Full Disk Access
The bot must read `chat.db`, which macOS protects.

- **System Settings ▸ Privacy & Security ▸ Full Disk Access**
- Add whatever runs the script:
  - Testing from a terminal → add **Terminal** (or iTerm).
  - Running via launchd → add the Python binary: `/opt/homebrew/bin/python3`
    (drag it in from Finder with ⌘⇧G → paste the path).
- Quit and reopen Terminal afterward.

### 3. Configure
```bash
mkdir -p ~/.satrelay
cp config.example.json ~/.satrelay/config.json
```
Edit `~/.satrelay/config.json`:
- `allowed_handles`: **your iPhone's** phone number and/or Apple ID email
  (the sender the bot will answer). Phone numbers match on the last 10 digits.
- `openai_api_key`: your OpenAI key (from platform.openai.com).
- Tune `openai_model`, `system_prompt`, `chunk_chars` as you like.

### 4. Test in the foreground
```bash
python3 /Users/you/Desktop/sites/sat-relay/satrelay.py
```
You should see `satrelay started`. Now, from your iPhone, iMessage the Mac's
dedicated address (over normal wifi first — no need to go off-grid to test).
The first send triggers a macOS **Automation** prompt to let the script
control Messages — click **OK**. You should get a reply within a few seconds.

### 5. Run it forever (launchd)
`install.sh` generates `com.example.satrelay.plist` with the correct paths
for this Mac (it's gitignored since it's machine-specific). If you haven't run
the installer, run `./install.sh` now to produce it, then:
```bash
cp com.example.satrelay.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.example.satrelay.plist
```
Logs stream to `satrelay.log` in this folder. To stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.example.satrelay.plist
```
Make sure the Mac never sleeps: **System Settings ▸ Displays ▸ Advanced ▸
"Prevent automatic sleeping when the display is off"**, or use `caffeinate`.

---

## Using it in the field
1. Off-grid on your iPhone, open the last thread with the Mac's address.
2. When you have no signal, iOS offers **Messages via satellite** — follow the
   on-screen guide to connect (point at the satellite).
3. Send your question. The reply comes back over the same link — keep an eye
   out, and stay connected until it arrives.

Tips: keep questions short. The bot answers tersely by design. Weather,
directions, "what's poisonous," gear questions, relay-a-message-to-someone all
work well if you add that logic later.

---

## Auto-logging location to the camp map

The `loc:` command writes a timestamped point to your camp site's Firebase
Realtime Database (`camp/<SYNC_ID>/track`), and the camp map draws it as a
breadcrumb trail — newest ping highlighted in red. All the network write
happens on the Mac; your phone only sends the coordinates as text.

Configure it in `config.json` under `location_sink` (already pointed at the
camp DB in the example). Set `database_url` to `""` to disable the command.

**Getting coordinates off-grid** — your iPhone's GPS works with no signal:

- *Manual:* open **Compass** or drop a pin in **Maps**; both show your
  decimal lat/lon. Text `loc: 43.3879, -74.7105 note`.
- *One tap (recommended):* build an **iOS Shortcut** —
  `Get Current Location` → `Get Details of Location` (Latitude) →
  (Longitude) → `Text` = `loc: [Latitude], [Longitude]` →
  `Send Message` to the Mac's relay address. Add it to your Home Screen or
  Action Button. Off-grid, running it composes the message; connect to the
  satellite to send. GPS resolves even with no cell/wifi.

Notes: coordinates must be decimal (not the DMS "44°7′12″" form). The trail
persists in Firebase; to clear it, delete the `track` node from the Firebase
console (or the camp site can add a "clear trail" button later).

## Security notes
- The bot only answers handles in `allowed_handles`. Anyone else is ignored.
- Your OpenAI key lives in `~/.satrelay/config.json` on your Mac only.
- The dedicated Apple ID means a stranger who somehow got the Mac's address
  still can't trigger it — they're not on the allow-list.

## Troubleshooting
- **"unable to open database file"** → Full Disk Access not granted (step 2),
  or granted to the wrong app. Quit/reopen Terminal.
- **No reply / AppleScript error** → grant Automation access (step 4), and
  confirm Messages is open and signed in on the Mac.
- **Empty message text** → some messages store text in `attributedBody`; the
  script decodes it best-effort. If you see blanks, send plain text.
- **Wrong Python in launchd** → set the interpreter path in the `.plist` to
  the output of `which python3`.
