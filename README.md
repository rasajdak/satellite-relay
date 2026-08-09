# SatGPT — text ChatGPT from off-grid, over satellite

Text ChatGPT from anywhere, even with no cell or wifi, using your iPhone's
**Messages via satellite**. An always-on Mac catches the message, asks OpenAI,
and texts the answer back. It runs two ways: on a Mac signed into your **own
Apple ID** (text yourself a note starting with a keyword), or on one with a
**dedicated Apple ID** (text that address). See *Two ways to run it* below.

```
iPhone --(satellite iMessage)--> Mac [satrelay] --(OpenAI)--> reply --(satellite)--> iPhone
```

## How it works

`satrelay.py` polls the Messages database (`~/Library/Messages/chat.db`) for
new messages (optionally gated by a **trigger keyword** and/or an
**allow-list**), runs them through a small command dispatcher, and replies over
iMessage via AppleScript. Zero third-party dependencies — just Python 3 (stdlib).

Because satellite bandwidth is tiny, replies are capped and split into short
chunks, and the system prompt tells GPT to be terse.

## Two ways to run it

**A) Single account — simplest, no extra Apple ID (recommended).**
Run the relay on a Mac signed into your **own** Apple ID (the same one as your
iPhone). Set a `trigger_keyword` (e.g. `"satchat"`) in the config, then text
**yourself** — a "Note to Self" — starting with that word:

```
satchat what's a bowline good for?
```

The relay reads both directions of the thread, so it sees your own message,
answers it, and tags every reply with a 🛰 marker so it never replies to
itself. The keyword is what stops it from answering ordinary notes-to-self. No
second account, no sign-in gymnastics.

**B) Dedicated Apple ID.**
Run the relay on a Mac signed into a *separate* Apple ID and text **that**
address from your phone. No keyword needed — `allowed_handles` is the gate.
Cleaner separation, but you have to create and keep a second account signed in
(and brand-new Apple IDs can be fussy about staying logged into iMessage).

You need **at least one gate**: a `trigger_keyword`, an `allowed_handles` list,
or both. Otherwise the relay refuses to start (so it's never wide open).

## Field commands

Text these to the bot (the leading word is the command, case-insensitive):

| You send | What happens |
|---|---|
| `help` | Lists the commands. |
| `w: Denver CO` | Live weather. Also `wx:` / `weather:`. `w:` alone uses `default_location`. State/country disambiguates (`Buffalo NY` ≠ `Buffalo MN`); works worldwide. No API key. |
| `loc: 43.39, -74.71 at the ridge` | Logs a location ping to the camp map's breadcrumb trail. Also `gps:` / `here:`. Trailing text becomes the note. See "Auto-logging location" below. |
| `to dad: running late` | Relays an iMessage to a saved contact. Also `msg` / `tell` / `relay`. Only names in `relay_contacts` can be reached. |
| `reset` | Clears the conversation memory. |
| anything else | Goes to ChatGPT (GPT-5.6), which remembers the last few turns (`memory_turns`) and **searches the web** when a question needs current info (`web_search`). |

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

### 1. Sign in (pick a mode)
**Single-account mode (recommended):** the Mac just needs to be signed into
your normal Apple ID in Messages — the same one as your iPhone. Nothing else to
set up here; you'll set a `trigger_keyword` in step 3, and test by texting
yourself.

**Dedicated-account mode:** create a separate Apple Account at account.apple.com,
sign the *Mac* into it in **Messages ▸ Settings ▸ iMessage**, and keep your
iPhone on your normal account. Your phone iMessages that address; the bot
replies to your phone. Send a test from your phone and confirm a **blue bubble**
lands on the Mac. (Heads-up: brand-new email Apple IDs can be stubborn about
staying signed into iMessage — if it keeps logging out, single-account mode
avoids the whole issue.)

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
- `trigger_keyword`: **single-account mode** — set a word like `"satchat"`; only
  messages starting with it are answered (leave `""` for dedicated-account mode).
- `allowed_handles`: **dedicated-account mode** — your iPhone's phone number
  and/or Apple ID email (the sender the bot will answer). Phone numbers match on
  the last 10 digits. Can be `[]` if you're using a `trigger_keyword`.
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
1. Off-grid on your iPhone, open the right thread: your **Note to Self** thread
   in single-account mode (and prefix each message with your keyword, e.g.
   `satchat …`), or the thread with the **Mac's dedicated address** otherwise.
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
- The relay only acts on messages that pass a gate: the `trigger_keyword`
  and/or the `allowed_handles` allow-list. It refuses to start with neither set.
- Every reply is tagged with a 🛰 marker and skipped on the way back in, so the
  relay never answers its own messages (loop protection for single-account mode).
- Your OpenAI key lives in `~/.satrelay/config.json` on your Mac only.

## Troubleshooting
- **"unable to open database file"** → Full Disk Access not granted (step 2),
  or granted to the wrong app. Quit/reopen Terminal.
- **No reply / AppleScript error** → grant Automation access (step 4), and
  confirm Messages is open and signed in on the Mac.
- **Empty message text** → some messages store text in `attributedBody`; the
  script decodes it best-effort. If you see blanks, send plain text.
- **Wrong Python in launchd** → set the interpreter path in the `.plist` to
  the output of `which python3`.
