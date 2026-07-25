# Telegram Sender

One message, typed once, delivered privately and individually to a list of people.
Replies come back to the sender's normal Telegram, one-to-one. No subscription.

**The client should read [`HOW TO USE.txt`](HOW%20TO%20USE.txt), not this file.**
This file is the technical notes. [`WARNING.md`](WARNING.md) covers the ban risk.

## Running it

| Platform | Do this |
| --- | --- |
| Windows | double-click **`START WINDOWS.bat`** |
| Mac / Linux | double-click **`START MAC or LINUX.command`** |

The launcher checks for Python, creates the virtualenv, installs Telethon and opens
the window. First run takes ~1 minute, after that a few seconds. If Python is missing
it opens the download page and explains the "Add Python to PATH" tickbox.

Linux also needs `python3-tk` (`sudo apt install python3-tk`) — the launcher detects
this and says so.

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | The tkinter window — the only thing the client touches |
| `sender.py` | Engine: connection, planning, pacing, safety. No UI, no prints |
| `broadcast.py` | Command-line front end for the same engine (servers, testing) |
| `config.json` | Credentials + all pacing/safety numbers |
| `message.txt` / `recipients.txt` | Autosaved from the window's two text boxes |
| `state.json` | Who already received it. Archived, not wiped, by "New campaign" |
| `sent_log.csv` | Audit trail: timestamp, target, status, reason |
| `main_account.session` | Telethon session = a live login. **Never share or commit.** |

`sender.py` reports everything through an `emit(kind, **data)` callback, so the GUI and
CLI share identical logic. Event kinds: `log`, `progress`, `waiting`, `blocked`,
`finished`.

## Anti-ban design

Built from how Telegram's spam system actually behaves — non-contacts and burst
velocity are what get flagged, not volume alone.

| Measure | Detail |
| --- | --- |
| Human gaps | 45–180s random between messages |
| Rest breaks | 8–20 min after every 12–20 messages |
| Typing simulation | Read-pause, then a real typing indicator sized to the text |
| Night silence | Sends only 09:00–21:30 local; sleeps otherwise |
| Text variation | Multiple variants + `{a\|b\|c}` spintax, resolved per recipient |
| Warm-up ramp | 20 → 25 → 35 → 45 → 60 → 75 → 90 → 100/day over the first week |
| Contact priority | Saved contacts + open chats first; strangers last, ≤12/day |
| FloodWait | Waits the stated time **+20% buffer**, never bypasses or switches session |
| PeerFlood | Hard stop, whole run aborted — this restriction lasts days |
| Pre-flight | @SpamBot queried before every run; refuses to start if limited |
| Error brake | Stops after 5 consecutive failures |
| Idempotent | State saved after every message; Stop/crash never double-sends |

**Do not raise `daily_cap` or lower the delays.** They are the protection, not a
throttle to tune.

## Command line

```bash
./venv/bin/python broadcast.py --check   # plan + sample messages, sends nothing
./venv/bin/python broadcast.py           # send
./venv/bin/python broadcast.py --yes     # no confirmation prompt
```

Login must be done once through the window first (`app.py`) — the CLI won't prompt for
a phone code.

## Tested

Verified with a mock Telegram client, no live account needed:

- ✅ Full run to completion, all delivered, every message textually distinct
- ✅ `FloodWaitError` mid-run → waits, retries, completes all
- ✅ `PeerFloodError` → aborts immediately at that message, nothing sent after
- ✅ `UserPrivacyRestrictedError` → skips that person, continues, counts correctly
- ✅ STOP pressed mid-run → halts promptly, progress preserved
- ✅ Resume → already-sent people excluded, no double-sends
- ✅ Warm-up ramp returns 20/25/45/90/100 on days 0/1/3/6/10
- ✅ Recipient parsing: usernames, `+353 87 123 4567` spacing, IDs, comments, dupes
- ✅ GUI builds, both screens render, event pump drives progress/status/log correctly
- ✅ Config round-trip preserves comments; CLI refuses to run unconfigured

Not exercised without real credentials: the live login handshake and real
`send_message` calls. Before the full list, run `--check`, then send to 2–3 of your own
numbers.

## Updates via GitHub

The client's private data never enters git. `config.json`, `*.session`,
`projects.json`, `state.json` and `sent_log.csv` are all ignored.

Config is layered: `DEFAULT_CONFIG` → `config.default.json` (tracked template)
→ `config.json` (his machine only). His `config.json` stores **only** his API
keys plus settings he personally changed, so:

- a `git pull` can never overwrite his keys, phone or choices
- changing a shipped default reaches him automatically — unless he overrode it
- new settings you add appear for him with no manual step

### Your side

```bash
git remote add origin git@github.com:<you>/telegram-sender.git
git push -u origin main          # first time
# later
git add -A && git commit -m "..." && git push
```

**Use a private repo.** Not because of secrets — those are excluded — but the
code is a mass-DM tool and there's no reason to publish it.

### His side

One-time: install Git for Windows (https://git-scm.com/download/win, click Next
on every screen), then clone once into `C:\TelegramSender`.

After that he does nothing: **`START WINDOWS.bat` pulls the latest version
automatically** on every launch and reinstalls requirements if they changed. It
never blocks — no git, no internet, or a failed pull just carries on and opens
the window.

**`UPDATE WINDOWS.bat`** is there for when he wants to force it, or when you tell
him to grab a fix right now. It says plainly that his lists and login aren't
touched.

### If a pull ever conflicts

Only possible if he edited a tracked file by hand. Fix:

```bash
git checkout -- .    # throw away his local edits to tracked files
git pull
```

His message, lists, settings and login are in ignored files, so that command is
safe — it cannot touch them.
