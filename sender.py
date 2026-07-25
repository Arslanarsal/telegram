"""
Sending engine. No printing, no input() — it reports through a callback so the
window (app.py) and the command line (broadcast.py) share identical logic.

Safety model, based on how Telegram actually enforces spam:

  Telegram's hard limits apply to NON-CONTACTS. PeerFloodError is specifically
  "contacted too many non-mutual users". So the two groups are treated
  differently:

    * People you already know (saved contacts / open private chats)
        -> effectively unlimited. The only real limit is the clock, because
           each message is paced like a human typing it.
    * People you have never spoken to
        -> hard capped per day. This is the group that gets accounts banned.

  Plus: gradual warm-up for the first week, randomised human pacing, typing
  indicators, night silence, FloodWait obeyed with a 20% buffer, and a full
  stop on PeerFlood.
"""

import asyncio
import csv
import json
import os
import random
import re
from datetime import datetime, time as dtime, timedelta

from telethon import TelegramClient, errors, functions
from telethon.tl.types import User

HERE = os.path.dirname(os.path.abspath(__file__))
# config.default.json is tracked in git (placeholders only).
# config.json is the live file with the real keys — git-ignored, never pushed.
CONFIG_TEMPLATE_PATH = os.path.join(HERE, "config.default.json")
CONFIG_PATH = os.path.join(HERE, "config.json")
PROJECTS_PATH = os.path.join(HERE, "projects.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "sent_log.csv")

# Legacy single-list files, imported once into a project if they exist.
MESSAGE_PATH = os.path.join(HERE, "message.txt")
RECIPIENTS_PATH = os.path.join(HERE, "recipients.txt")

# Max messages allowed on day 1, 2, 3... of using this tool. Telegram trusts
# gradual growth and punishes sudden spikes. After this list: no ceiling.
WARMUP_RAMP = [25, 40, 60, 90, 130, 180, 240]

SPEED_PRESETS = {
    "safest": {"label": "Safest — slowest, strongly recommended",
               "delay_min": 45, "delay_max": 180,
               "break_every_min": 12, "break_every_max": 20,
               "break_min": 480, "break_max": 1200},
    "normal": {"label": "Normal — a bit quicker, still careful",
               "delay_min": 25, "delay_max": 90,
               "break_every_min": 15, "break_every_max": 25,
               "break_min": 300, "break_max": 720},
    "fast":   {"label": "Fast — RISKY, can get the account limited",
               "delay_min": 8, "delay_max": 30,
               "break_every_min": 25, "break_every_max": 40,
               "break_min": 120, "break_max": 300},
}

DEFAULT_CONFIG = {
    "api_id": 0,
    "api_hash": "",
    "session_name": "main_account",
    "speed": "safest",
    "delay_min": 45,
    "delay_max": 180,
    "break_every_min": 12,
    "break_every_max": 20,
    "break_min": 480,
    "break_max": 1200,
    "typing_per_char": 0.08,
    "typing_min": 2.0,
    "typing_max": 14.0,
    "read_pause_min": 1.5,
    "read_pause_max": 5.0,
    "active_hours_start": "09:00",
    "active_hours_end": "21:30",
    # People you already know: no artificial cap by default — the clock is the
    # limit. Set a number here if you want one.
    "unlimited_known": True,
    "daily_cap_known": 300,
    # Strangers: this cap is the thing keeping the account alive. Keep it low.
    "daily_cap_new": 15,
    "stop_after_consecutive_errors": 5,
    "check_spambot_before_run": True,
    "abort_on_peer_flood": True,
    "use_warmup": True,
}


# ---------------------------------------------------------------- io helpers
# NOTE: every open() specifies encoding. Without this, Windows defaults to
# cp1252 and any accented name, €, or emoji crashes the whole app.

def _read(path, default=""):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- config

def load_config():
    """Defaults <- tracked template <- the user's own live config.json.

    Layering it this way means `git pull` can add new settings or change the
    shipped defaults without ever touching the user's keys or their choices.
    """
    cfg = dict(DEFAULT_CONFIG)
    for path in (CONFIG_TEMPLATE_PATH, CONFIG_PATH):
        saved = _read_json(path, {})
        cfg.update({k: v for k, v in saved.items() if not k.startswith("_")})
    return cfg


# Keys that belong to this one machine and must always be kept locally.
ALWAYS_SAVE = ("api_id", "api_hash", "session_name", "last_phone")


def save_config(cfg):
    """Only ever writes config.json — the template stays pristine for git.

    Stores just the values that actually differ from the shipped defaults, so a
    later `git pull` can still change a default the user never touched. If they
    set something back to the default we drop it again for the same reason.
    """
    base = dict(DEFAULT_CONFIG)
    tmpl = _read_json(CONFIG_TEMPLATE_PATH, {})
    base.update({k: v for k, v in tmpl.items() if not k.startswith("_")})

    out = _read_json(CONFIG_PATH, {})
    for k, v in cfg.items():
        if k.startswith("_"):
            continue
        if k in ALWAYS_SAVE or base.get(k) != v:
            out[k] = v
        else:
            out.pop(k, None)
    _write_json(CONFIG_PATH, out)


def apply_speed(cfg, preset):
    if preset in SPEED_PRESETS:
        cfg["speed"] = preset
        for k, v in SPEED_PRESETS[preset].items():
            if k != "label":
                cfg[k] = v
    return cfg


def config_ready(cfg):
    return bool(cfg.get("api_id")) and bool(cfg.get("api_hash")) \
        and "PUT_YOUR" not in str(cfg.get("api_hash"))


# ---------------------------------------------------------------- projects
# A "project" is one saved job: a name, its message, and its list of people.

DEFAULT_MESSAGE = """{Hi|Hello|Hey} {name},

{Just a quick update|Quick update for you} — today's rates are out.
{Let me know if you want the details.|Message me if you need anything.}

{Thanks|Thank you},
"""


def load_projects():
    data = _read_json(PROJECTS_PATH, None)
    if data and isinstance(data, dict) and data.get("projects"):
        return data
    # first run: migrate the old single message.txt / recipients.txt if present
    msg = _read(MESSAGE_PATH, "") or DEFAULT_MESSAGE
    rcpt = _read(RECIPIENTS_PATH, "")
    data = {"projects": {"My first list": {"message": msg, "recipients": rcpt}},
            "current": "My first list"}
    _write_json(PROJECTS_PATH, data)
    return data


def save_projects(data):
    _write_json(PROJECTS_PATH, data)


def project_names(data):
    return sorted(data["projects"].keys())


def get_project(data, name):
    return data["projects"].get(name, {"message": DEFAULT_MESSAGE,
                                       "recipients": ""})


def set_project(data, name, message, recipients):
    data["projects"][name] = {"message": message, "recipients": recipients}
    data["current"] = name
    save_projects(data)


def delete_project(data, name):
    data["projects"].pop(name, None)
    if not data["projects"]:
        data["projects"]["My first list"] = {"message": DEFAULT_MESSAGE,
                                            "recipients": ""}
    if data.get("current") == name:
        data["current"] = project_names(data)[0]
    save_projects(data)
    return data


# ---------------------------------------------------------------- parsing

def parse_message_variants(text):
    lines = [ln.rstrip("\n") for ln in text.split("\n")
             if not ln.lstrip().startswith("#")]
    blocks, current = [], []
    for ln in lines:
        if ln.strip() == "---":
            blocks.append("\n".join(current).strip())
            current = []
        else:
            current.append(ln)
    blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def parse_recipients(text):
    """-> list of (target, name_or_None), duplicates removed, order kept."""
    out, seen = [], set()
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            target, name = line.split(",", 1)
            target, name = target.strip(), name.strip() or None
        else:
            target, name = line, None
        if not target:
            continue
        key = target.lower().lstrip("@")
        if key in seen:
            continue
        seen.add(key)
        out.append((target, name))
    return out


SPIN_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def expand_spintax(text):
    while True:
        m = SPIN_RE.search(text)
        if not m:
            return text
        text = text[:m.start()] + random.choice(m.group(1).split("|")) + text[m.end():]


def render(variants, name):
    return expand_spintax(random.choice(variants)).replace(
        "{name}", name or "there").strip()


def norm_phone(s):
    return re.sub(r"[^\d]", "", s)


def is_known(target, known):
    ids, usernames, phones = known
    t = str(target).strip()
    if t.startswith("+"):
        return norm_phone(t) in phones
    t = t.lstrip("@").lower()
    if t.isdigit():
        return int(t) in ids or t in phones
    return t in usernames


def parse_hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


# ---------------------------------------------------------------- state
# Per-project record of who already received it, plus per-day counters split
# by known/new so the two caps can be enforced independently.

def load_state():
    st = _read_json(STATE_PATH, {})
    st.setdefault("projects", {})
    st.setdefault("history", {})
    st.setdefault("first_use", None)
    return st


def save_state(state):
    _write_json(STATE_PATH, state)


def project_state(state, project):
    ps = state["projects"].setdefault(project, {"sent": {}, "failed": {}})
    ps.setdefault("sent", {})
    ps.setdefault("failed", {})
    return ps


def reset_project_state(state, project):
    """New campaign for this project only — archive its record, start clean."""
    ps = project_state(state, project)
    arch = state.setdefault("archive", [])
    if ps["sent"]:
        arch.append({"project": project,
                     "finished": datetime.now().isoformat(timespec="seconds"),
                     "count": len(ps["sent"])})
    state["projects"][project] = {"sent": {}, "failed": {}}
    save_state(state)
    return state


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def sent_today(state):
    """-> (known_sent_today, new_sent_today)"""
    d = state["history"].get(_today(), {})
    return d.get("known", 0), d.get("new", 0)


def record_send(state, cls):
    d = state["history"].setdefault(_today(), {"known": 0, "new": 0})
    d[cls] = d.get(cls, 0) + 1


def warmup_ceiling(state, cfg):
    """Total messages allowed today during the first week. None = no ceiling."""
    if not cfg.get("use_warmup", True):
        return None
    first = state.get("first_use")
    if not first:
        return WARMUP_RAMP[0]
    try:
        day = (datetime.now().date() - datetime.fromisoformat(first).date()).days
    except Exception:
        return WARMUP_RAMP[0]
    return WARMUP_RAMP[day] if 0 <= day < len(WARMUP_RAMP) else None


def log_row(project, target, name, status, detail=""):
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "project", "target", "name",
                        "status", "detail"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), project,
                    target, name or "", status, detail])


# ---------------------------------------------------------------- engine

class Sender:
    def __init__(self, cfg, emit):
        self.cfg = cfg
        self.emit = emit
        self.client = None
        self._phone = None

    def log(self, msg, level="info"):
        self.emit("log", message=msg, level=level)

    # -- connection ----------------------------------------------------

    async def connect(self):
        self.client = TelegramClient(
            os.path.join(HERE, self.cfg["session_name"]),
            int(self.cfg["api_id"]), self.cfg["api_hash"],
        )
        await self.client.connect()
        if await self.client.is_user_authorized():
            me = await self.client.get_me()
            return {"state": "ready", "name": me.first_name,
                    "username": me.username, "id": me.id}
        return {"state": "need_phone"}

    async def send_code(self, phone):
        self._phone = phone
        await self.client.send_code_request(phone)
        return {"state": "need_code"}

    async def sign_in(self, code=None, password=None):
        try:
            if password:
                await self.client.sign_in(password=password)
            else:
                await self.client.sign_in(self._phone, code)
        except errors.SessionPasswordNeededError:
            return {"state": "need_password"}
        me = await self.client.get_me()
        return {"state": "ready", "name": me.first_name,
                "username": me.username, "id": me.id}

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    # -- account health ------------------------------------------------

    async def spambot_status(self):
        try:
            async with self.client.conversation("SpamBot", timeout=30) as conv:
                await conv.send_message("/start")
                reply = (await conv.get_response()).raw_text
        except Exception as e:
            return True, f"Could not reach @SpamBot ({type(e).__name__}). Continuing."
        low = reply.lower()
        if "no limits" in low or "free as a bird" in low:
            return True, reply
        if any(w in low for w in ("limited", "restricted", "cannot", "can't")):
            return False, reply
        return True, reply

    async def load_known_peers(self):
        """Saved contacts + anyone with an open private chat."""
        ids, usernames, phones = set(), set(), set()

        def add(u):
            ids.add(u.id)
            if getattr(u, "username", None):
                usernames.add(u.username.lower())
            if getattr(u, "phone", None):
                phones.add(norm_phone(u.phone))

        try:
            res = await self.client(functions.contacts.GetContactsRequest(hash=0))
            for u in getattr(res, "users", []):
                add(u)
        except Exception as e:
            self.log(f"Could not read your contact list ({type(e).__name__}).",
                     "warn")
        async for d in self.client.iter_dialogs():
            if d.is_user and not getattr(d.entity, "bot", False):
                add(d.entity)
        return ids, usernames, phones

    # -- groups --------------------------------------------------------

    async def list_groups(self):
        """Groups the account is in -> [{'title','id','count'}]"""
        out = []
        async for d in self.client.iter_dialogs():
            if d.is_group:
                out.append({"title": d.title or "(no name)", "id": d.id})
        return out

    async def group_members(self, group_id):
        """Real human members of a group -> [(target, name)]"""
        people, seen = [], set()
        me = await self.client.get_me()
        entity = await self.client.get_entity(group_id)
        async for u in self.client.iter_participants(entity):
            if not isinstance(u, User):
                continue
            if u.bot or u.deleted or u.id == me.id or u.id in seen:
                continue
            seen.add(u.id)
            target = f"@{u.username}" if u.username else str(u.id)
            people.append((target, u.first_name or None))
        return people

    # -- planning ------------------------------------------------------

    async def build_plan(self, recipients, state, project):
        known = await self.load_known_peers()
        ps = project_state(state, project)

        todo_known, todo_new, done = [], [], 0
        for target, name in recipients:
            if target in ps["sent"]:
                done += 1
                continue
            (todo_known if is_known(target, known) else todo_new).append(
                (target, name))

        k_done, n_done = sent_today(state)
        ceiling = warmup_ceiling(state, self.cfg)
        room_total = None if ceiling is None else max(0, ceiling - k_done - n_done)

        # people you know: no artificial cap unless one is configured
        if self.cfg.get("unlimited_known", True):
            k_room = len(todo_known)
        else:
            k_room = max(0, self.cfg["daily_cap_known"] - k_done)
        if room_total is not None:
            k_room = min(k_room, room_total)
        q_known = todo_known[:k_room]

        # strangers: hard cap, this is the dangerous group
        n_room = max(0, self.cfg["daily_cap_new"] - n_done)
        if room_total is not None:
            n_room = min(n_room, max(0, room_total - len(q_known)))
        q_new = todo_new[:n_room]

        queue = [(t, n, "known") for t, n in q_known] + \
                [(t, n, "new") for t, n in q_new]

        avg = (self.cfg["delay_min"] + self.cfg["delay_max"]) / 2
        nbreaks = len(queue) / max(1, (self.cfg["break_every_min"]
                                       + self.cfg["break_every_max"]) / 2)
        hours = (len(queue) * avg + nbreaks
                 * (self.cfg["break_min"] + self.cfg["break_max"]) / 2) / 3600

        return {
            "queue": queue,
            "known_total": len(todo_known), "new_total": len(todo_new),
            "known_queued": len(q_known), "new_queued": len(q_new),
            "known_today": k_done, "new_today": n_done,
            "warmup_ceiling": ceiling,
            "left_for_later": len(todo_known) + len(todo_new) - len(queue),
            "already_done": done,
            "hours": hours,
        }

    # -- pacing --------------------------------------------------------

    async def _wait_active_hours(self, stop):
        start = parse_hhmm(self.cfg["active_hours_start"])
        end = parse_hhmm(self.cfg["active_hours_end"])
        told = False
        while not stop.is_set():
            now = datetime.now()
            if start <= now.time() <= end:
                return True
            t = now.replace(hour=start.hour, minute=start.minute,
                            second=0, microsecond=0)
            if now.time() > end:
                t += timedelta(days=1)
            if not told:
                self.log(f"Outside sending hours "
                         f"({self.cfg['active_hours_start']}–"
                         f"{self.cfg['active_hours_end']}). Waiting until "
                         f"{t:%H:%M}. You can leave this window open.", "warn")
                told = True
            self.emit("waiting", seconds=(t - now).total_seconds(),
                      wait_kind="night")
            await self._sleep(min(60, (t - now).total_seconds()), stop)
        return False

    async def _sleep(self, secs, stop, tick=None):
        loop = asyncio.get_event_loop()
        end = loop.time() + secs
        while not stop.is_set():
            left = end - loop.time()
            if left <= 0:
                return True
            if tick:
                tick(left)
            await asyncio.sleep(min(1.0, left))
        return False

    async def _human_typing(self, entity, text, stop):
        await self._sleep(random.uniform(self.cfg["read_pause_min"],
                                        self.cfg["read_pause_max"]), stop)
        dur = len(text) * self.cfg["typing_per_char"] * random.uniform(0.8, 1.25)
        dur = max(self.cfg["typing_min"], min(self.cfg["typing_max"], dur))
        try:
            async with self.client.action(entity, "typing"):
                await self._sleep(dur, stop)
        except Exception:
            await self._sleep(dur, stop)

    # -- the run -------------------------------------------------------

    async def run(self, plan, variants, state, project, stop):
        queue = plan["queue"]
        ps = project_state(state, project)
        if not state.get("first_use"):
            state["first_use"] = datetime.now().isoformat(timespec="seconds")
            save_state(state)

        sent = failed = consecutive = since_break = 0
        next_break = random.randint(self.cfg["break_every_min"],
                                    self.cfg["break_every_max"])
        total = len(queue)
        reason = "finished"

        for i, (target, name, cls) in enumerate(queue, 1):
            if stop.is_set():
                reason = "stopped by you"
                break
            if not await self._wait_active_hours(stop):
                reason = "stopped by you"
                break
            self.emit("progress", done=i - 1, total=total, sent=sent,
                      failed=failed)

            try:
                entity = await self.client.get_entity(target)
            except errors.FloodWaitError as e:
                w = int(e.seconds * 1.2) + 5
                self.log(f"Telegram asked us to wait {w}s. Waiting — normal.",
                         "warn")
                if not await self._sleep(w, stop):
                    reason = "stopped by you"
                    break
                try:
                    entity = await self.client.get_entity(target)
                except Exception as e2:
                    self._fail(ps, state, project, target, name, "not_found",
                               type(e2).__name__,
                               f"{target}: could not be found")
                    failed += 1
                    consecutive += 1
                    continue
            except Exception as e:
                self._fail(ps, state, project, target, name, "not_found",
                           type(e).__name__,
                           f"{target}: could not be found — check the "
                           f"username or number")
                failed += 1
                consecutive += 1
                continue

            display = name or getattr(entity, "first_name", None) or "there"
            text = render(variants, display)

            try:
                await self._human_typing(entity, text, stop)
                if stop.is_set():
                    reason = "stopped by you"
                    break
                await self.client.send_message(entity, text)
                ps["sent"][target] = datetime.now().isoformat(timespec="seconds")
                record_send(state, cls)
                log_row(project, target, name, "sent", cls)
                sent += 1
                since_break += 1
                consecutive = 0
                self.log(f"Sent to {display} ({target})", "good")

            except errors.PeerFloodError:
                self.log("TELEGRAM HAS FLAGGED THIS ACCOUNT FOR SPAM. Stopping "
                         "now. Do not send again today. Check @SpamBot in "
                         "Telegram.", "bad")
                log_row(project, target, name, "peer_flood", "run aborted")
                save_state(state)
                reason = "Telegram flagged the account (PeerFlood)"
                break

            except errors.FloodWaitError as e:
                w = int(e.seconds * 1.2) + 10
                self.log(f"Telegram asked us to wait {w}s. Waiting, then "
                         f"retrying.", "warn")
                if not await self._sleep(w, stop):
                    reason = "stopped by you"
                    break
                try:
                    await self.client.send_message(entity, text)
                    ps["sent"][target] = datetime.now().isoformat(
                        timespec="seconds")
                    record_send(state, cls)
                    log_row(project, target, name, "sent", "after wait")
                    sent += 1
                    since_break += 1
                    consecutive = 0
                    self.log(f"Sent to {display} (after wait)", "good")
                except Exception as e2:
                    self._fail(ps, state, project, target, name, "failed",
                               type(e2).__name__, None)
                    failed += 1
                    consecutive += 1

            except (errors.UserPrivacyRestrictedError,
                    errors.UserIsBlockedError,
                    errors.UserBannedInChannelError) as e:
                self._fail(ps, state, project, target, name, "cannot_receive",
                           type(e).__name__,
                           f"{display}: cannot receive messages from you "
                           f"(their privacy setting, or they blocked you)",
                           level="warn")
                failed += 1
                consecutive = 0

            except Exception as e:
                self._fail(ps, state, project, target, name, "error",
                           type(e).__name__,
                           f"{target}: error — {type(e).__name__}: {e}")
                failed += 1
                consecutive += 1

            save_state(state)
            self.emit("progress", done=i, total=total, sent=sent, failed=failed)

            if consecutive >= self.cfg["stop_after_consecutive_errors"]:
                self.log(f"Stopping: {consecutive} problems in a row. Check "
                         f"your list of people.", "bad")
                reason = "too many errors in a row"
                break
            if i == total:
                break

            if since_break >= next_break:
                b = random.uniform(self.cfg["break_min"], self.cfg["break_max"])
                self.log(f"Taking a {b/60:.0f} minute break — this is what keeps "
                         f"the account safe.", "info")
                if not await self._sleep(b, stop, tick=lambda l: self.emit(
                        "waiting", seconds=l, wait_kind="break")):
                    reason = "stopped by you"
                    break
                since_break = 0
                next_break = random.randint(self.cfg["break_every_min"],
                                            self.cfg["break_every_max"])
            else:
                d = random.uniform(self.cfg["delay_min"], self.cfg["delay_max"])
                if not await self._sleep(d, stop, tick=lambda l: self.emit(
                        "waiting", seconds=l, wait_kind="gap")):
                    reason = "stopped by you"
                    break

        save_state(state)
        self.emit("finished", sent=sent, failed=failed, total=total,
                  reason=reason)
        return {"sent": sent, "failed": failed, "reason": reason}

    def _fail(self, ps, state, project, target, name, status, detail,
              msg, level="bad"):
        if msg:
            self.log(msg, level)
        ps["failed"][target] = detail
        log_row(project, target, name, status, detail)
        save_state(state)

# update-test marker
