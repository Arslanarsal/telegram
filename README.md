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
| `sender.py` | Telegram engine: connection, planning, pacing, safety. No UI, no prints |
| `mailer.py` | Email engine: SMTP, plain-English error translation. Imports `sender.py`, never the reverse |
| `broadcast.py` | Command-line front end for the same engine (servers, testing) |
| `config.default.json` | Tracked template: pacing/safety defaults, no secrets |
| `config.json` | **Git-ignored.** His real api keys + only the settings he changed |
| `projects.json` | **Git-ignored.** Each project's message + recipient list |
| `state.json` | **Git-ignored.** Per-project sent-record + per-day known/new/email counters |
| `sent_log.csv` | **Git-ignored.** Audit trail: time, project, target, status |
| `main_account.session` | **Git-ignored.** A live login. Never share or commit. |
| `message.txt` / `recipients.txt` | Legacy seeds, migrated into a project on first run |
| `tools/selftest_email.py` | Engine + regression tests. stdlib only, no network, no pytest |
| `tools/selftest_ui.py` | Drives the real window with a stand-in Telegram and SMTP |

`sender.py` reports everything through an `emit(kind, **data)` callback, so the GUI and
CLI share identical logic. Event kinds: `log`, `progress`, `waiting`, `blocked`,
`email_blocked`, `finished`. `progress` and `finished` carry `channel` (`"Email"`,
absent for Telegram) so the window can run both engines one after the other.

## Anti-ban design

Built from how Telegram's spam system actually behaves — non-contacts and burst
velocity are what get flagged, not volume alone.

| Measure | Detail |
| --- | --- |
| Human gaps | 45–180s random between messages (the `safest` preset) |
| Rest breaks | 8–20 min after every 12–20 messages |
| Typing simulation | Read-pause, then a real typing indicator sized to the text |
| Night silence | Sends only 09:00–21:30 local; sleeps otherwise |
| Text variation | Multiple variants + `{a\|b\|c}` spintax, resolved per recipient |
| Warm-up ramp | 25 → 40 → 60 → 90 → 130 → 180 → 240 total/day, then no ceiling |
| Known contacts | **No cap.** Saved contacts + open chats; the clock is the only limit |
| Strangers | **Hard cap 15/day**, sent last. `PeerFlood` = "too many non-mutual users" |
| FloodWait | Waits the stated time **+20% buffer**, never bypasses or switches session |
| PeerFlood | Hard stop, whole run aborted — this restriction lasts days |
| Pre-flight | @SpamBot queried before every run; refuses to start if limited |
| Error brake | Stops after 5 consecutive failures |
| Idempotent | State saved after every message; Stop/crash never double-sends |

**Do not raise `daily_cap_new` or lower the delays.** They are the protection, not a
throttle to tune. `unlimited_known` is safe to leave on — those people are not what
Telegram's spam system targets.

## Email

Stdlib `smtplib`/`email` only — deliberately no new dependency, because both START
scripts run `pip install -r requirements.txt` after a `git pull` while swallowing the
exit code, so a failed download would be silent and the app would die at import.

An email address is simply a valid recipient `target`: a line `john@example.com, John`
already parsed correctly before any of this existed, so no stored data changed shape.
`S.split_channels()` separates the two lists at the point of sending, and Telegram is
never handed an address — `_resolve()` would fail on one and five failures in a row
abort the whole run.

Email counts live under a separate `"email"` key in the same per-day history dict.
`sent_today()` reads only `known`/`new`, so the two channels cannot consume each
other's daily allowance in either direction.

| Measure | Detail |
| --- | --- |
| Gaps | 20–60s, same active-hours window as Telegram |
| Daily cap | 200 by default (Gmail allows ~500, Outlook ~300) |
| Privacy | "Everyone sees it" mode puts addresses in the SMTP envelope only, never a `Bcc:` header |
| Batching | 40 recipients per message; providers refuse very long lists |
| Compliance | Opt-out line + `List-Unsubscribe` header, on by default |
| Timeouts | `timeout=30` on every SMTP object, so STOP is always bounded |
| Blocking I/O | `run_in_executor` (3.7+, not `asyncio.to_thread`) — the client's Python version is unknown |

`sent_log.csv` deliberately keeps its original six columns; the header is written once
at file creation, so a seventh would desync every existing user's file. The delivery
report derives the channel from `S.is_email(target)`, never from the `detail` column —
that column already carries `"after wait"` and `"run aborted"` for Telegram rows.

## Tests

```bash
./venv/bin/python tools/selftest_email.py   # 126 checks, no network
./venv/bin/python tools/selftest_ui.py      # 52 checks, needs a screen
```

Both redirect `TELEGRAM_SENDER_DATA` to a temp folder before importing `sender`, and
abort if `S.DATA` is not inside it, so they can never touch real lists or a real login.
`selftest_email.py` includes a regression alarm for the Telegram side: a golden
`build_plan` result, the six-column CSV header, and proof that 200 emails leave
`sent_today()` unchanged.

## Command line

```bash
./venv/bin/python broadcast.py --list                      # show projects
./venv/bin/python broadcast.py --project "Daily" --check    # plan only
./venv/bin/python broadcast.py --project "Daily" --yes       # send
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
- ✅ Warm-up ramp 25/40/60/90/130/180/240 then uncapped, days 0–30
- ✅ 200 known contacts all queued; 15 of 50 strangers queued; 35 held for later
- ✅ Stranger cap exhausted → 0 new queued, known still unlimited
- ✅ known/new day counters tracked independently
- ✅ Projects: create, persist, rename, delete; per-project sent-record isolation
- ✅ Unicode/emoji (`Séamus O'Brien — €50 ✅ 中文 🎉`) through files and CSV — the
  cp1252 crash that would have hit on Windows
- ✅ Recipient parsing: usernames, `+353 87 123 4567` spacing, IDs, comments, dupes
- ✅ GUI: both screens, project switching restores per-project content, settings
  dialog, group picker, break/gap/night countdowns, button state on finish
- ✅ Config layering: keys survive a pull, his overrides win, new defaults reach him

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
