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
import shutil
import sys
import time
from datetime import datetime, time as dtime, timedelta

from telethon import TelegramClient, errors, functions
from telethon.tl.types import InputPhoneContact, User

HERE = os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """Where the user's own files live — deliberately NOT the app folder.

    The app folder is a git clone and may sit on the Desktop, inside OneDrive.
    OneDrive locks files while it syncs, which is what stopped groups saving.
    LocalAppData (and its Mac/Linux equivalents) is never synced, so the data
    is out of OneDrive's reach no matter where the app itself is installed.
    """
    env = os.environ.get("TELEGRAM_SENDER_DATA")
    if env:
        base = env
    elif os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA")
                            or os.path.expanduser("~"), "TelegramSender")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/TelegramSender")
    else:
        base = os.path.expanduser("~/.local/share/TelegramSender")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        return HERE          # last resort: better to run than to refuse
    return base


DATA = data_dir()

# Tracked in git (placeholders only) — this one stays with the code.
CONFIG_TEMPLATE_PATH = os.path.join(HERE, "config.default.json")

# Everything below belongs to the user and lives outside the synced folder.
CONFIG_PATH = os.path.join(DATA, "config.json")
PROJECTS_PATH = os.path.join(DATA, "projects.json")
PROJECTS_BACKUP_PATH = os.path.join(DATA, "projects.backup.json")
JOURNAL_PATH = os.path.join(DATA, "people_journal.jsonl")
LOCK_PATH = os.path.join(DATA, "app.lock")
STATE_PATH = os.path.join(DATA, "state.json")
LOG_PATH = os.path.join(DATA, "sent_log.csv")

# Files left in the app folder by earlier versions get moved across once.
MIGRATE = ("config.json", "projects.json", "projects.backup.json",
           "people_journal.jsonl", "state.json", "sent_log.csv",
           "main_account.session", "main_account.session-journal")


def migrate_data():
    """Move any old files out of the app folder. Returns what was moved."""
    if DATA == HERE:
        return []
    moved = []
    for name in MIGRATE:
        src, dst = os.path.join(HERE, name), os.path.join(DATA, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.move(src, dst)
                moved.append(name)
            except Exception:
                try:
                    shutil.copy2(src, dst)
                    moved.append(name)
                except Exception:
                    pass
    return moved

# Legacy single-list files, imported once into a project if they exist.
MESSAGE_PATH = os.path.join(HERE, "message.txt")
RECIPIENTS_PATH = os.path.join(HERE, "recipients.txt")

# Max messages allowed on day 1, 2, 3... of using this tool. Telegram trusts
# gradual growth and punishes sudden spikes. After this list: no ceiling.
WARMUP_RAMP = [25, 40, 60, 90, 130, 180, 240]

# How often a waiting run re-checks the clock and the sending hours. Small so
# that changing the hours mid-wait takes effect almost immediately.
HOURS_POLL_SECONDS = 10

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
    "simulate_typing": True,
    "abort_on_peer_flood": True,
    "use_warmup": True,

    # ------------------------------------------------------------ email
    # Sending email uses the user's own mailbox over SMTP. Nothing here is
    # filled in by us — the window asks for it and writes it to config.json,
    # which is git-ignored. Never put a real address or password in
    # config.default.json: that file is tracked by git.
    "email_address": "",
    "email_password": "",
    "email_from_name": "",
    "email_smtp_host": "",
    "email_smtp_port": 587,
    "email_use_tls": True,
    # "password" or "microsoft". Microsoft blocked password sending for
    # personal Outlook/Hotmail/Live accounts, so those sign in instead and we
    # keep a refresh token rather than a password.
    "email_auth": "password",
    "email_oauth_refresh_token": "",
    "email_oauth_client_id": "",
    # Email is far less risky than Telegram, but providers still throttle.
    # 20-60s keeps a normal mailbox comfortably under every provider's radar.
    "email_delay_min": 20,
    "email_delay_max": 60,
    "email_daily_cap": 200,
    # How many addresses share one message when sending "to everyone".
    # Gmail rejects roughly 100+ recipients on a single message.
    "email_bcc_batch": 40,
    "email_unsubscribe": True,
    "email_unsubscribe_text": (
        "If you would rather not receive these, just reply with the word "
        "STOP and I will take you off the list."),

    # Lets someone reach the dashboard without a Telegram login — needed for
    # email-only use, and as the way back in if a Telegram session expires.
    "skip_telegram": False,
}


# ---------------------------------------------------------------- io helpers
# NOTE: every open() specifies encoding. Without this, Windows defaults to
# cp1252 and any accented name, €, or emoji crashes the whole app.

class SaveError(Exception):
    """A save did not reach the disk. Never swallow this — the user must know."""


def _read(path, default=""):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _write(path, text, attempts=6):
    """Write, retrying through the transient locks OneDrive and antivirus take.

    Writes to a temp file, flushes to the physical disk, then swaps it in. If it
    still cannot be written after retrying, raises SaveError — silence here is
    what loses people's data.
    """
    last = None
    for i in range(attempts):
        try:
            tmp = f"{path}.tmp{os.getpid()}"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return
        except (PermissionError, OSError) as e:
            last = e
            try:
                os.remove(tmp)
            except Exception:
                pass
            time.sleep(0.2 * (i + 1))
    raise SaveError(f"Could not write {os.path.basename(path)} after "
                    f"{attempts} tries: {last}")


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    """Write, then read it back and prove it landed. Keeps a second copy too."""
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    _write(path, text)
    check = _read_json(path, None)
    if check != obj:
        raise SaveError(f"{os.path.basename(path)} did not save correctly — the "
                        f"file on disk does not match what was written.")
    # a second copy, so one bad write can never be the end of the data
    try:
        _write(path.replace(".json", "") + ".backup.json", text)
    except Exception:
        pass


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
ALWAYS_SAVE = ("api_id", "api_hash", "session_name", "last_phone",
               "email_address", "email_password", "email_from_name",
               "email_smtp_host", "email_smtp_port", "email_auth",
               "email_oauth_refresh_token", "email_oauth_client_id")


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

DEFAULT_MESSAGE = ""


def load_projects():
    data = _read_json(PROJECTS_PATH, None)
    if not (data and isinstance(data, dict) and data.get("projects")):
        # main file missing or unreadable — fall back to the second copy and
        # put it back where it belongs
        backup = _read_json(PROJECTS_BACKUP_PATH, None)
        if backup and isinstance(backup, dict) and backup.get("projects"):
            data = backup
            try:
                _write(PROJECTS_PATH, json.dumps(data, indent=2,
                                                 ensure_ascii=False))
            except Exception:
                pass
    if data and isinstance(data, dict) and data.get("projects"):
        return data
    data = {"projects": {"My first list": {"message": DEFAULT_MESSAGE,
                                          "recipients": ""}},
            "current": "My first list"}
    try:
        _write_json(PROJECTS_PATH, data)
    except SaveError:
        pass
    return data


def save_projects(data):
    _write_json(PROJECTS_PATH, data)


# ------------------------------------------------- append-only people journal
# Every add and removal is appended here before anything else happens. Even if
# projects.json is lost, corrupted, or clobbered by a second copy of the app,
# the list of people can always be rebuilt from this.

def journal(action, project, people):
    """action is 'add' or 'remove'; people is [(target, name)]."""
    try:
        with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
            for target, name in people:
                f.write(json.dumps({
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "action": action, "project": project,
                    "target": target, "name": name or ""},
                    ensure_ascii=False) + "\n")
    except Exception:
        pass          # the journal must never block real work


def journal_recover(project):
    """Replay the journal for one group -> the people it should contain."""
    order, names = [], {}
    if not os.path.exists(JOURNAL_PATH):
        return []
    try:
        with open(JOURNAL_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("project") != project:
                    continue
                key = str(e.get("target", "")).lower().lstrip("@")
                if not key:
                    continue
                if e.get("action") == "add":
                    if key not in names:
                        order.append(key)
                    names[key] = (e["target"], e.get("name") or None)
                elif e.get("action") == "remove":
                    names.pop(key, None)
                    if key in order:
                        order.remove(key)
    except Exception:
        return []
    return [names[k] for k in order if k in names]


def journal_projects():
    """Group names that appear in the journal, for offering recovery."""
    out = set()
    if not os.path.exists(JOURNAL_PATH):
        return []
    try:
        with open(JOURNAL_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    out.add(json.loads(line)["project"])
                except Exception:
                    continue
    except Exception:
        pass
    return sorted(out)


# ------------------------------------------------------------- instance lock
# Two copies of the app running at once was one cause of people vanishing:
# whichever window closed last overwrote the other's file. The running app
# touches this file every few seconds; a fresh timestamp means it is alive.

LOCK_STALE_SECONDS = 25


def lock_is_held():
    try:
        if not os.path.exists(LOCK_PATH):
            return False
        return (time.time() - os.path.getmtime(LOCK_PATH)) < LOCK_STALE_SECONDS
    except Exception:
        return False


def lock_touch():
    try:
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def lock_release():
    try:
        os.remove(LOCK_PATH)
    except Exception:
        pass


def in_onedrive():
    p = HERE.replace("\\", "/").lower()
    return "onedrive" in p


def project_names(data):
    return sorted(data["projects"].keys())


def get_project(data, name):
    p = data["projects"].get(name, {"message": DEFAULT_MESSAGE,
                                    "recipients": ""})
    p.setdefault("attachment", "")
    p.setdefault("mode", "private")
    p.setdefault("post_group", None)
    p.setdefault("subject", "")
    # Which channels this group sends on. None means "never chosen" so the
    # window can fall back to a sensible default for what is in the list.
    p.setdefault("channels", None)
    return p


def set_project(data, name, message, recipients, attachment="",
                mode="private", post_group=None, subject="", channels=None):
    data["projects"][name] = {"message": message, "recipients": recipients,
                              "attachment": attachment or "",
                              "mode": mode or "private",
                              "post_group": post_group,
                              "subject": subject or "",
                              "channels": channels}
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


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

# Telegram refuses a caption longer than this; the text is sent separately then.
CAPTION_LIMIT = 1024


def attachment_kind(path):
    ext = os.path.splitext(str(path))[1].lower()
    if ext in IMAGE_EXT:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    return "document"


def is_known(target, known):
    ids, usernames, phones = known
    t = str(target).strip()
    if t.startswith("+"):
        return norm_phone(t) in phones
    t = t.lstrip("@").lower()
    if t.isdigit():
        return int(t) in ids or t in phones
    return t in usernames


# An email address is just another kind of target, so a person can be written
# as "john@example.com, John" in exactly the same list as "@ali, Ali". Nothing
# about how people are stored had to change to support email.
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")


def is_email(target):
    return bool(EMAIL_RE.match(str(target).strip()))


def split_channels(recipients):
    """[(target, name)] -> (telegram_people, email_people). Order kept.

    Telegram must never be handed an email address: get_entity() would fail on
    it, and five failures in a row abort the whole run.
    """
    tg, em = [], []
    for t, n in recipients:
        (em if is_email(t) else tg).append((t, n))
    return tg, em


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


def delivery_report(project=None, day=None):
    """Read sent_log.csv back -> (rows, summary). Proof of what actually went."""
    rows = []
    if not os.path.exists(LOG_PATH):
        return rows, {"sent": 0, "failed": 0, "days": 0}
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                if project and r.get("project") != project:
                    continue
                if day and not str(r.get("timestamp", "")).startswith(day):
                    continue
                rows.append(r)
    except Exception:
        return rows, {"sent": 0, "failed": 0, "days": 0}
    rows.reverse()                      # newest first
    sent = sum(1 for r in rows if r.get("status") == "sent")
    days = {str(r.get("timestamp", ""))[:10] for r in rows if r.get("timestamp")}
    return rows, {"sent": sent, "failed": len(rows) - sent, "days": len(days)}


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
            os.path.join(DATA, self.cfg["session_name"]),
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

    async def post_to_group(self, group_id, text, attachment=""):
        """Post ONE message into a Telegram group chat.

        The opposite of the normal mode: everyone in that group sees the same
        message, and everyone sees the replies. Use only when that is wanted.
        """
        entity = await self.client.get_entity(group_id)
        title = getattr(entity, "title", str(group_id))
        try:
            await self._deliver(entity, text, attachment)
        except errors.ChatWriteForbiddenError:
            raise ValueError(f"You are not allowed to post in \"{title}\" — "
                             f"only admins can write there.")
        except errors.ChatAdminRequiredError:
            raise ValueError(f"\"{title}\" only lets admins post.")
        return title

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
        """Hold until we are inside the allowed sending window.

        The window is re-read from the config on every pass, so changing the
        sending hours while a run is already waiting takes effect within a
        minute. Reading it once up front meant a run that went to sleep at
        21:30 kept last night's start time no matter what you changed it to.
        """
        told = None
        while not stop.is_set():
            start = parse_hhmm(self.cfg["active_hours_start"])
            end = parse_hhmm(self.cfg["active_hours_end"])
            now = datetime.now()
            t_now = now.time()

            # a window like 22:00-06:00 runs through midnight
            overnight = start > end
            inside = (start <= t_now <= end) if not overnight else \
                     (t_now >= start or t_now <= end)
            if inside:
                if told:
                    self.log("Inside sending hours now — carrying on.", "good")
                return True

            nxt = now.replace(hour=start.hour, minute=start.minute,
                              second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)

            window = (f"{self.cfg['active_hours_start']}–"
                      f"{self.cfg['active_hours_end']}")
            if told != window:
                # says it again if the hours are changed mid-wait
                self.log(f"Outside sending hours ({window}). Waiting until "
                         f"{nxt:%H:%M}. You can leave this window open.", "warn")
                told = window
            self.emit("waiting", seconds=(nxt - now).total_seconds(),
                      wait_kind="night")
            await self._sleep(min(HOURS_POLL_SECONDS,
                                  max(0.5, (nxt - now).total_seconds())),
                              stop)
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

    async def _human_typing(self, entity, text, stop, action="typing"):
        await self._sleep(random.uniform(self.cfg["read_pause_min"],
                                        self.cfg["read_pause_max"]), stop)
        dur = len(text) * self.cfg["typing_per_char"] * random.uniform(0.8, 1.25)
        dur = max(self.cfg["typing_min"], min(self.cfg["typing_max"], dur))
        if not self.cfg.get("simulate_typing", True):
            return await self._sleep(dur, stop)
        try:
            async with self.client.action(entity, action):
                await self._sleep(dur, stop)
        except Exception:
            await self._sleep(dur, stop)

    def _looks_like_phone(self, target):
        t = str(target).strip()
        return t.startswith("+") or (t.isdigit() and len(norm_phone(t)) >= 7)

    async def _resolve(self, target, name):
        """Find the person. A phone number that isn't a saved contact cannot be
        looked up, so add it as a contact first — the same thing you'd do by
        hand before messaging a new number."""
        try:
            return await self.client.get_entity(target)
        except errors.FloodWaitError:
            raise
        except Exception:
            if not self._looks_like_phone(target):
                raise
        phone = str(target).strip()
        if not phone.startswith("+"):
            phone = "+" + norm_phone(phone)
        res = await self.client(functions.contacts.ImportContactsRequest(
            [InputPhoneContact(client_id=random.randrange(1, 2 ** 62),
                               phone=phone,
                               first_name=(name or "Contact")[:64],
                               last_name="")]))
        if getattr(res, "users", None):
            self.log(f"{phone} was not in your contacts — added it so the "
                     f"message can be sent.", "warn")
            return res.users[0]
        raise ValueError("that number has no Telegram account, or their "
                         "privacy settings block being found by number")

    async def _deliver(self, entity, text, attachment):
        """One message, with a photo/video/file attached if there is one."""
        if not attachment:
            await self.client.send_message(entity, text)
            return
        if len(text) <= CAPTION_LIMIT:
            await self.client.send_file(entity, attachment, caption=text)
        else:
            # too long to be a caption — send the file, then the text
            await self.client.send_file(entity, attachment)
            await self.client.send_message(entity, text)

    # -- the run -------------------------------------------------------

    async def run(self, plan, variants, state, project, stop, attachment=""):
        queue = plan["queue"]
        ps = project_state(state, project)
        if attachment and not os.path.exists(attachment):
            self.log(f"The attached file is missing: {attachment}\nSending the "
                     f"text only.", "warn")
            attachment = ""
        if attachment:
            self.log(f"Attaching {attachment_kind(attachment)}: "
                     f"{os.path.basename(attachment)}", "info")
        upload_action = ("typing" if not attachment
                         else attachment_kind(attachment))
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
                entity = await self._resolve(target, name)
            except errors.FloodWaitError as e:
                w = int(e.seconds * 1.2) + 5
                self.log(f"Telegram asked us to wait {w}s. Waiting — normal.",
                         "warn")
                if not await self._sleep(w, stop):
                    reason = "stopped by you"
                    break
                try:
                    entity = await self._resolve(target, name)
                except Exception as e2:
                    self._fail(ps, state, project, target, name, "not_found",
                               type(e2).__name__,
                               f"{target}: could not be reached — {e2}")
                    failed += 1
                    consecutive += 1
                    continue
            except Exception as e:
                detail = (str(e) if isinstance(e, ValueError) else
                          "check the username or number is correct")
                self._fail(ps, state, project, target, name, "not_found",
                           type(e).__name__,
                           f"{target}: could not be reached — {detail}")
                failed += 1
                consecutive += 1
                continue

            display = name or getattr(entity, "first_name", None) or "there"
            text = render(variants, display)

            try:
                await self._human_typing(entity, text, stop, upload_action)
                if stop.is_set():
                    reason = "stopped by you"
                    break
                await self._deliver(entity, text, attachment)
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
                    await self._deliver(entity, text, attachment)
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
