# Read this before you run it

This sends messages from **your own Telegram account**, not from a bot. That is how
it reaches people who never pressed "Start" on anything. It is also why there is risk.

## The honest situation

Automated mass messaging from a user account is against Telegram's Terms of Service.
Telegram's anti-spam system can **limit or ban the account**. Nobody can promise it
won't happen — not this script, not any paid service that claims "anti-ban".

What actually triggers a ban, in order of danger:

1. **Recipients pressing "Report Spam" or blocking you.** This is the #1 cause by far.
   A few reports from strangers is enough.
2. **Messaging people you have never talked to.** Cold DMs are what the filter hunts.
3. **Sending fast.** Bursts of identical messages in seconds.
4. **Identical text** to many people.
5. **A new or unestablished account.**

## What this script does about it

| Risk | Defence in this script |
| --- | --- |
| Sending too fast | 45–180s random gap, long 8–20 min breaks every 12–20 messages |
| Robotic timing | Random "reading" pause + real typing indicator sized to message length |
| Sending at 4am | Only sends between 09:00–21:30 local (configurable) |
| Identical text | Multiple message variants + `{a\|b\|c}` random word choices |
| Cold DMs | Existing chats sent first; never-talked-to people capped at 15/day |
| Volume | No artificial cap for people you already know; the clock is the limit. 300/day if you turn the cap on. First week ramps 25 → 240. |
| Telegram pushing back | Obeys `FloodWait` exactly; **stops the whole run** on `PeerFlood` |
| Already-limited account | Asks @SpamBot before every run and refuses to start if limited |
| Crash / double-send | Progress saved after every single message |

## Rules to actually stay alive

- **Only message people who expect to hear from you.** This script cannot protect you
  from someone who doesn't want your message. One angry stranger reporting you does
  more damage than 500 well-paced messages.
- **Do not raise `daily_cap_new`.** 15 strangers a day is the number that keeps
  accounts alive. `PeerFlood` is triggered by messaging people who have never
  talked to you, not by volume as such. Pushing it to 100 is where accounts die.
- **Do not lower the delays** to "get it done faster". The delays are the protection.
- **Keep the account normal otherwise** — use it by hand too, have a profile photo,
  don't run this from a brand-new account.
- **Check @SpamBot** in Telegram if anything looks wrong. It tells you your status.
- If you get `PEER FLOOD`, the script stops itself. **Do not restart that day.**

## If the account does get limited

Message **@SpamBot** in Telegram, press the buttons, and politely explain you were
messaging your own customers. Limits are often temporary (hours to days) and can
sometimes be lifted. Do not run the script again until @SpamBot says you are clear —
the script itself will refuse to start anyway.

The safe alternative remains a real bot (`t.me/yourbot`), where recipients press Start
once and there is no ban risk at all, ever. If this account gets restricted, that is
the fallback worth revisiting.

## Email

Email is a different world and none of the above applies to it. There is no ban
risk of the Telegram kind — the risk is your address being marked as spam.

| Risk | Defence |
| --- | --- |
| Looking like bulk mail | 20–60s gap between emails, same active-hours window |
| Provider daily limits | Capped at 200/day by default; Gmail allows ~500, Outlook ~300 |
| Recipients seeing each other | "One message everyone sees" puts addresses in the envelope only — never a `Bcc:` header |
| Huge recipient lists | Split into batches of 40; a single 500-address message is refused outright |
| Spam complaints | An opt-out line on every email, plus a `List-Unsubscribe` header |

Credentials live in `config.json`, which is git-ignored. **Never put a real address
or password in `config.default.json`** — that file is tracked.

Gmail, Outlook, Yahoo and iCloud all require an app password rather than the account
password. That is the single most common reason email "does not work".
