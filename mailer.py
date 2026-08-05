"""Sending email from the user's own mailbox.

Deliberately built on nothing but the Python standard library — smtplib and
email are always there, so a `git pull` can never leave the app unable to
start because a download failed.

This module imports sender.py for the shared pieces (people parsing, the
{name} substitution, the sent-record, the CSV log). sender.py never imports
this one, so the Telegram side cannot be affected by anything in here.
"""

import asyncio
import base64
import json
import mimetypes
import os
import random
import smtplib
import socket
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import sender as S

# Same 10-second poll the Telegram side uses, so changing the sending hours
# mid-run takes effect just as quickly here.
HOURS_POLL_SECONDS = S.HOURS_POLL_SECONDS

# Most providers stop at 25 MB and count the encoding overhead, so 20 is the
# honest limit to enforce.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

SMTP_TIMEOUT = 30


# ------------------------------------------------------------ mail servers
# Filling these in for the user is the difference between "it works" and a
# support call. Anything not listed here is typed in by hand.
SMTP_HOSTS = {
    "gmail.com": ("smtp.gmail.com", 587, True),
    "googlemail.com": ("smtp.gmail.com", 587, True),
    "outlook.com": ("smtp-mail.outlook.com", 587, True),
    "hotmail.com": ("smtp-mail.outlook.com", 587, True),
    "hotmail.co.uk": ("smtp-mail.outlook.com", 587, True),
    "hotmail.ie": ("smtp-mail.outlook.com", 587, True),
    "live.com": ("smtp-mail.outlook.com", 587, True),
    "live.ie": ("smtp-mail.outlook.com", 587, True),
    "msn.com": ("smtp-mail.outlook.com", 587, True),
    "yahoo.com": ("smtp.mail.yahoo.com", 587, True),
    "yahoo.co.uk": ("smtp.mail.yahoo.com", 587, True),
    "yahoo.ie": ("smtp.mail.yahoo.com", 587, True),
    "ymail.com": ("smtp.mail.yahoo.com", 587, True),
    "icloud.com": ("smtp.mail.me.com", 587, True),
    "me.com": ("smtp.mail.me.com", 587, True),
    "aol.com": ("smtp.aol.com", 587, True),
    "eircom.net": ("mail1.eircom.net", 587, True),
    "zoho.com": ("smtp.zoho.com", 587, True),
    "zoho.eu": ("smtp.zoho.eu", 587, True),
    "gmx.com": ("mail.gmx.com", 587, True),
    "protonmail.com": ("127.0.0.1", 1025, False),  # needs their Bridge app
    "proton.me": ("127.0.0.1", 1025, False),
}

# Providers that will not take the account password, only an app password.
APP_PASSWORD_HOSTS = ("gmail.com", "googlemail.com", "outlook.com",
                      "hotmail.com", "hotmail.co.uk", "hotmail.ie",
                      "live.com", "live.ie", "msn.com", "yahoo.com",
                      "yahoo.co.uk", "yahoo.ie", "ymail.com",
                      "icloud.com", "me.com", "aol.com")

APP_PASSWORD_URLS = {
    "google": "https://myaccount.google.com/apppasswords",
    "microsoft": "https://account.live.com/proofs/AppPassword",
    "yahoo": "https://login.yahoo.com/account/security",
    "apple": "https://appleid.apple.com/account/manage",
}


def domain_of(address):
    return str(address).strip().rsplit("@", 1)[-1].lower() if "@" in \
        str(address) else ""


def guess_smtp(address):
    """-> (host, port, use_tls) or None when we do not recognise the domain."""
    return SMTP_HOSTS.get(domain_of(address))


def needs_app_password(address):
    return domain_of(address) in APP_PASSWORD_HOSTS


def app_password_url(address):
    d = domain_of(address)
    if d in ("gmail.com", "googlemail.com"):
        return APP_PASSWORD_URLS["google"]
    if d in ("outlook.com", "hotmail.com", "hotmail.co.uk", "hotmail.ie",
             "live.com", "live.ie", "msn.com"):
        return APP_PASSWORD_URLS["microsoft"]
    if d.startswith("yahoo") or d == "ymail.com":
        return APP_PASSWORD_URLS["yahoo"]
    if d in ("icloud.com", "me.com"):
        return APP_PASSWORD_URLS["apple"]
    return APP_PASSWORD_URLS["google"]


# -------------------------------------------------- signing in to Microsoft
# Microsoft switched personal Outlook/Hotmail/Live accounts off password
# sending altogether (535 5.7.139). The only way left is OAuth, so instead of
# a password the user signs in with their real Microsoft account and we keep
# a refresh token. Nothing here needs a library: it is two HTTP calls and a
# base64 string.

MS_AUTH_BASE = "https://login.microsoftonline.com/common/oauth2/v2.0"
MS_SCOPE = "offline_access https://outlook.office.com/SMTP.Send"
# Registered as "Telegram Sender", any Microsoft account. A public client id
# is not a secret — it identifies the app, it does not authorise anything.
MS_CLIENT_ID = "828e3ae2-6d94-4dd1-ae0a-09aae387f53e"


class SignInError(Exception):
    """Something went wrong signing in, already worded for a human."""


def _post(url, fields, timeout=30):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            raise SignInError(f"Microsoft did not answer properly ({e.code}).")
    except urllib.error.URLError as e:
        raise SignInError("Could not reach Microsoft. Check your internet "
                          f"connection.\n\n({e.reason})")


def ms_client_id(cfg=None):
    return ((cfg or {}).get("email_oauth_client_id") or "").strip() \
        or MS_CLIENT_ID


def ms_start_signin(cfg=None):
    """Ask Microsoft for a short code the user types into their browser.

    -> {"user_code", "verification_uri", "device_code", "interval",
        "expires_in", "message"}
    """
    r = _post(f"{MS_AUTH_BASE}/devicecode",
              {"client_id": ms_client_id(cfg), "scope": MS_SCOPE})
    if "device_code" not in r:
        raise SignInError(_ms_error(r))
    return r


def ms_finish_signin(started, cfg=None, stop=None, on_wait=None):
    """Wait for them to finish in the browser. -> refresh_token."""
    interval = max(3, int(started.get("interval", 5)))
    deadline = time.time() + int(started.get("expires_in", 900))
    while time.time() < deadline:
        if stop is not None and stop.is_set():
            raise SignInError("Sign-in cancelled.")
        if on_wait:
            on_wait(int(deadline - time.time()))
        time.sleep(interval)
        r = _post(f"{MS_AUTH_BASE}/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": ms_client_id(cfg),
            "device_code": started["device_code"]})
        err = r.get("error")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval += 5
            continue
        if err:
            raise SignInError(_ms_error(r))
        if r.get("refresh_token"):
            return r["refresh_token"]
        raise SignInError("Microsoft signed you in but did not return the "
                          "permission we need. In the app registration, check "
                          "offline_access is added under API permissions.")
    raise SignInError("The sign-in code ran out before it was used. Press "
                      "\"Sign in with Microsoft\" and try again.")


def ms_access_token(refresh_token, cfg=None):
    """Swap the stored token for a fresh one. Lasts about an hour."""
    r = _post(f"{MS_AUTH_BASE}/token", {
        "grant_type": "refresh_token", "client_id": ms_client_id(cfg),
        "refresh_token": refresh_token, "scope": MS_SCOPE})
    if r.get("access_token"):
        return r["access_token"], r.get("refresh_token") or refresh_token
    raise SignInError(_ms_error(r, signed_out=True))


def _ms_error(r, signed_out=False):
    """Microsoft's error codes, in words that mean something."""
    code = r.get("error", "")
    desc = r.get("error_description", "") or ""
    if code == "invalid_client" or "AADSTS7000218" in desc:
        return ("Microsoft did not accept this app.\n\nIn the app "
                "registration, open Authentication and set \"Allow public "
                "client flows\" to Yes, then try again.")
    if code == "unauthorized_client" or "AADSTS700016" in desc:
        return ("Microsoft does not recognise this app.\n\nCheck the "
                "Application (client) ID is correct, and that the app allows "
                "personal Microsoft accounts.")
    if code in ("invalid_grant", "interaction_required") or signed_out:
        return ("Microsoft has signed this out. It happens if the password "
                "changed or the permission was withdrawn.\n\nPress \"Sign in "
                "with Microsoft\" again to reconnect. It takes a few seconds.")
    if code == "authorization_declined":
        return "You cancelled the sign-in. Press the button to try again."
    if code == "expired_token":
        return "The code ran out. Press the button to get a new one."
    if "AADSTS65001" in desc or code == "consent_required":
        return ("The permission to send email was not granted.\n\nSign in "
                "again and press Accept when Microsoft asks.")
    return f"Microsoft would not sign this in.\n\n{desc[:300] or code}"


def _xoauth2_string(user, token):
    return base64.b64encode(
        f"user={user}\1auth=Bearer {token}\1\1".encode()).decode()


# --------------------------------------------------------- plain English
# Nobody using this should ever see the word "SMTPAuthenticationError".

# Each provider words this differently and puts the page somewhere else, so
# telling a Microsoft user to "create one for Mail" the way Google does just
# sends them hunting. Name their own provider and give their own steps.
APP_PASSWORD_STEPS = {
    "google": (
        "Google", "https://myaccount.google.com/apppasswords",
        "  1. Press the \"?\" button beside the Password box. It opens the\n"
        "     Google App Passwords page for you.\n"
        "  2. In the one box on that page type any name, e.g. Sender, and\n"
        "     click Create.\n"
        "  3. Google shows you 16 letters in four small groups.\n"
        "     Copy them straight away - that box never comes back.\n"
        "  4. Paste them into the Password box here. Spaces do not matter.\n\n"
        "If Google will not let you make one, 2-Step Verification is off.\n"
        "Turn it on at https://myaccount.google.com/signinoptions/twosv\n"
        "then try again."),
    "microsoft": (
        "Microsoft", "https://account.live.com/proofs/AppPassword",
        "  1. Press the \"?\" button beside the Password box. It opens the\n"
        "     Microsoft App Passwords page for you.\n"
        "  2. Click \"Create a new app password\".\n"
        "  3. Microsoft shows you a password made of letters.\n"
        "     Copy it straight away - it only appears once.\n"
        "  4. Paste it into the Password box here.\n\n"
        "If that page will not let you make one, two-step verification is\n"
        "off. Turn it on at https://account.microsoft.com/security\n"
        "(Advanced security options), then try again."),
    "yahoo": (
        "Yahoo", "https://login.yahoo.com/account/security",
        "  1. Press the \"?\" button beside the Password box.\n"
        "  2. Click \"Generate app password\".\n"
        "  3. Copy the password it shows you.\n"
        "  4. Paste it into the Password box here."),
    "apple": (
        "Apple", "https://appleid.apple.com/account/manage",
        "  1. Press the \"?\" button beside the Password box.\n"
        "  2. Go to Sign-In and Security, then App-Specific Passwords.\n"
        "  3. Create one and copy it.\n"
        "  4. Paste it into the Password box here."),
}


def app_password_help(address=""):
    """The wrong-password message, written for whoever they are actually with."""
    key = _provider_key(address)
    if not key:
        return ("Your email address or password was not accepted.\n\n"
                "Check both are typed correctly.\n\n"
                "Many email providers will not accept your normal password "
                "from another program and give you a separate \"app password\" "
                "instead. If yours does, use that here. Your email provider's "
                "help pages will say.")
    who, _url, steps = APP_PASSWORD_STEPS[key]
    return (f"Your email address or password was not accepted.\n\n"
            f"{who} will not let any program send email using your normal "
            f"password. You need a separate App Password. It is free and takes "
            f"two minutes.\n\n{steps}\n\n"
            f"Nothing is wrong with your account.")


def _provider_key(address):
    d = domain_of(address)
    if d in ("gmail.com", "googlemail.com"):
        return "google"
    if d in ("outlook.com", "hotmail.com", "hotmail.co.uk", "hotmail.ie",
             "live.com", "live.ie", "live.co.uk", "msn.com"):
        return "microsoft"
    if d.startswith("yahoo") or d == "ymail.com":
        return "yahoo"
    if d in ("icloud.com", "me.com"):
        return "apple"
    return None


def explain(exc, addr="", cfg=None):
    """Any exception from sending -> (status_for_the_log, message, fatal).

    fatal=True means stop the whole run: carrying on would just repeat the
    same failure for every single person.
    """
    cfg = cfg or {}
    host = cfg.get("email_smtp_host", "the mail server")
    port = cfg.get("email_smtp_port", 587)
    me = cfg.get("email_address", "your address")

    if isinstance(exc, smtplib.SMTPAuthenticationError):
        # Microsoft has switched personal Outlook/Hotmail/Live accounts off
        # password sign-in altogether. An app password is correct and still
        # refused, so telling them to go and make another one would send them
        # round in circles forever.
        raw = f"{getattr(exc, 'smtp_error', b'')}".lower()
        if "basic authentication is disabled" in raw or "5.7.139" in raw:
            return ("basic_auth_off",
                    "Microsoft has turned off password sending for this "
                    "account, so no password will work here — not even an App "
                    "Password.\n\nThis is a change Microsoft made to "
                    "Outlook.com, Hotmail and Live accounts. There is no "
                    "setting to switch it back on, and nothing is wrong with "
                    "your account or your password.\n\nThe simple way round "
                    "it is to send your emails from a Gmail address instead. "
                    "Gmail is free, it takes a few minutes to set up, and your "
                    "people will receive the emails exactly the same.\n\n"
                    "Send your developer a screenshot of this and he will "
                    "sort it out with you.", True)
        return "auth_failed", app_password_help(me), True

    if isinstance(exc, smtplib.SMTPSenderRefused):
        return ("sender_refused",
                f"The mail server would not accept mail from {me}.\n\n"
                f"This usually means the address in Email settings is not the "
                f"same one you logged in with. Check they match exactly.", True)

    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return ("bad_address",
                f"{addr} was refused by the mail server. Check the address is "
                f"spelled correctly.", False)

    if isinstance(exc, smtplib.SMTPDataError):
        text = f"{getattr(exc, 'smtp_error', b'')}".lower()
        if "quota" in text or "limit" in text or "rate" in text:
            return ("daily_limit",
                    "Your email provider has reached its limit for today. "
                    "Nothing is lost — open this tomorrow and it carries on "
                    "from exactly where it stopped.", True)
        return ("rejected",
                f"The mail server refused this message for {addr}. Usually "
                f"that means it looked like spam — try shorter text with "
                f"fewer links.", False)

    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return ("no_tls",
                "This mail server does not support the secure connection this "
                "app needs. Try port 465 in Email settings, or ask your email "
                "provider which port to use.", True)

    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return ("disconnected",
                "The mail server hung up. Reconnecting and trying again.",
                False)

    if isinstance(exc, socket.gaierror):
        return ("no_server",
                f"Could not find the mail server \"{host}\".\n\nCheck your "
                f"internet is working, and check the \"Mail server\" box in "
                f"Email settings is spelled correctly.", True)

    if isinstance(exc, (ssl.SSLError,)):
        return ("ssl_error",
                "The secure connection to the mail server failed.\n\nIf you "
                "are on a work or hotel network, its firewall may be blocking "
                "email. Try port 465 in Email settings instead of 587.", True)

    if isinstance(exc, ConnectionRefusedError):
        return ("refused",
                f"The mail server refused the connection on port {port}.\n\n"
                f"Try 587 in Email settings, or 465 if your provider needs it.",
                True)

    # socket.timeout is TimeoutError on modern Python; keep both.
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ("timeout",
                "The mail server did not answer within 30 seconds. Check your "
                "internet connection.", False)

    if isinstance(exc, OSError):
        return ("no_connection",
                "Could not reach the mail server. Check your internet "
                "connection is working.", True)

    return ("error",
            f"Could not send to {addr} — {exc}", False)


# ------------------------------------------------------------- planning

def build_email_plan(recipients, state, project, cfg):
    """Who gets an email today. A pure function — no network, no waiting.

    Email deliberately has no warm-up and no known/new split: those exist
    because Telegram bans accounts for messaging strangers. Email does not
    work that way. The only limit is the provider's daily allowance.
    """
    ps = S.project_state(state, project)
    todo = [(t, n) for t, n in recipients if t not in ps["sent"]]
    done = len(recipients) - len(todo)

    used = email_sent_today(state)
    cap = max(0, int(cfg.get("email_daily_cap", 200)))
    room = max(0, cap - used)

    queue = [(t, n, "email") for t, n in todo[:room]]
    avg = (float(cfg.get("email_delay_min", 20)) +
           float(cfg.get("email_delay_max", 60))) / 2.0
    return {
        "queue": queue,
        "total": len(queue),
        "already_done": done,
        "left_for_later": len(todo) - len(queue),
        "sent_today": used,
        "cap": cap,
        "hours": (len(queue) * avg) / 3600.0,
    }


def email_sent_today(state):
    """How many emails went out today.

    Stored under its own "email" key in the same history dict the Telegram
    side uses. sent_today() only ever reads "known" and "new", so email
    traffic can never eat into the Telegram warm-up or the daily new-people
    cap — and vice versa.
    """
    return state.get("history", {}).get(S._today(), {}).get("email", 0)


# ------------------------------------------------------------- the sender

class EmailSender:
    """Mirrors sender.Sender's contract exactly: same emit() events, same
    sent-record, same CSV log, same stop flag. The window does not need to
    know which engine it is talking to."""

    def __init__(self, cfg, emit):
        self.cfg = cfg
        self.emit = emit
        self.conn = None

    def log(self, msg, level="info"):
        self.emit("log", message=msg, level=level)

    # -- connection ----------------------------------------------------

    def _connect(self):
        host = (self.cfg.get("email_smtp_host") or "").strip()
        port = int(self.cfg.get("email_smtp_port") or 587)
        addr = (self.cfg.get("email_address") or "").strip()
        pwd = self.cfg.get("email_password") or ""
        use_tls = bool(self.cfg.get("email_use_tls", True))

        if not host:
            guess = guess_smtp(addr)
            if not guess:
                raise ValueError(
                    "No mail server is set. Open Email settings and fill in "
                    "your email address — for most providers the rest fills "
                    "itself in.")
            host, port, use_tls = guess

        # timeout is not optional: without it a dead connection blocks
        # forever and the STOP button stops doing anything.
        if port == 465:
            conn = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT,
                                    context=ssl.create_default_context())
        else:
            conn = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
            conn.ehlo()
            if use_tls:
                conn.starttls(context=ssl.create_default_context())
                conn.ehlo()

        if self.cfg.get("email_auth") == "microsoft":
            # Signed in with a Microsoft account instead of a password.
            token, fresh = ms_access_token(
                self.cfg.get("email_oauth_refresh_token") or "", self.cfg)
            if fresh != self.cfg.get("email_oauth_refresh_token"):
                # Microsoft rotates these; keep the new one or the next run
                # would have to sign in all over again.
                self.cfg["email_oauth_refresh_token"] = fresh
                try:
                    S.save_config(self.cfg)
                except Exception:
                    pass
            conn.ehlo_or_helo_if_needed()
            code, resp = conn.docmd(
                "AUTH", "XOAUTH2 " + _xoauth2_string(addr, token))
            if code != 235:
                # Microsoft answers a failed XOAUTH2 with a base64 blob.
                raise smtplib.SMTPAuthenticationError(code, resp)
            return conn

        conn.login(addr, pwd)
        return conn

    def _ensure(self):
        """Providers drop idle connections, and our gaps are 20-60 seconds.
        A cheap NOOP turns that from a failed message into a non-event."""
        if self.conn is not None:
            try:
                code, _ = self.conn.noop()
                if code == 250:
                    return self.conn
            except Exception:
                pass
            try:
                self.conn.quit()
            except Exception:
                pass
            self.conn = None
        self.conn = self._connect()
        return self.conn

    def _close(self):
        if self.conn is not None:
            try:
                self.conn.quit()
            except Exception:
                pass
            self.conn = None

    # -- composing -----------------------------------------------------

    def _footer(self, text):
        if not self.cfg.get("email_unsubscribe", True):
            return text
        note = (self.cfg.get("email_unsubscribe_text") or "").strip()
        if not note:
            return text
        return f"{text}\n\n--\n{note}"

    def _compose(self, to_addrs, display_to, subject, body, attachment=""):
        addr = (self.cfg.get("email_address") or "").strip()
        name = (self.cfg.get("email_from_name") or "").strip()

        msg = EmailMessage()
        msg["From"] = formataddr((name, addr)) if name else addr
        msg["To"] = display_to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        if self.cfg.get("email_unsubscribe", True):
            # Required in Ireland/EU, and it also keeps people pressing
            # "unsubscribe" instead of "spam", which is what gets an address
            # blocked.
            msg["List-Unsubscribe"] = f"<mailto:{addr}?subject=unsubscribe>"
        msg.set_content(self._footer(body))

        if attachment:
            self._attach(msg, attachment)
        return msg

    def _attach(self, msg, path):
        size = os.path.getsize(path)
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"\"{os.path.basename(path)}\" is "
                f"{size / 1048576:.0f} MB. Email cannot carry a file that big "
                f"— most providers stop at 25 MB. Put it in Google Drive or "
                f"Dropbox and paste the link into your message instead.")
        ctype, encoding = mimetypes.guess_type(path)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(path))

    # -- sending -------------------------------------------------------

    def _blocking_send(self, msg, rcpts):
        """Runs in a worker thread — smtplib blocks and would freeze the
        window otherwise."""
        conn = self._ensure()
        try:
            conn.send_message(msg, to_addrs=rcpts)
        except smtplib.SMTPServerDisconnected:
            # One silent retry on a fresh connection. This is the single most
            # common hiccup on a long run and it is not worth telling anyone.
            self._close()
            self._ensure().send_message(msg, to_addrs=rcpts)

    async def _send(self, msg, rcpts):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._blocking_send, msg, rcpts)

    async def test_connection(self, to=None):
        """Log in and optionally send one email to the user themselves.
        -> (ok, plain_english_message)"""
        addr = (self.cfg.get("email_address") or "").strip()
        if not addr:
            return False, "Fill in your email address first."
        if self.cfg.get("email_auth") == "microsoft":
            if not (self.cfg.get("email_oauth_refresh_token") or ""):
                return False, ("Press \"Sign in with Microsoft\" first.")
        elif not (self.cfg.get("email_password") or ""):
            return False, "Fill in your password first."

        def work():
            self._close()
            conn = self._ensure()
            if to:
                msg = self._compose(
                    [to], to, "Test from your sender",
                    "This is a test.\n\nIf you are reading this, your email "
                    "settings are correct and you can send to your list.")
                conn.send_message(msg, to_addrs=[to])
            self._close()

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, work)
        except Exception as e:
            self._close()
            _, message, _ = explain(e, to or addr, self.cfg)
            return False, message
        if to:
            return True, (f"Success. A test email is on its way to {to} — "
                          f"check your inbox in a moment.\n\nIf it is not "
                          f"there, look in the Spam folder.")
        return True, "Success. Your email settings are correct."

    # -- pacing --------------------------------------------------------
    # Deliberately a copy of Sender._wait_active_hours / Sender._sleep rather
    # than a shared helper. Those two functions gate every Telegram send, and
    # the Telegram side is working; it is not worth touching them to save
    # forty lines here. Keep the two in step if either ever changes.

    async def _wait_active_hours(self, stop):
        told = None
        while not stop.is_set():
            start = S.parse_hhmm(self.cfg["active_hours_start"])
            end = S.parse_hhmm(self.cfg["active_hours_end"])
            now = datetime.now()
            t_now = now.time()

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
                self.log(f"Outside sending hours ({window}). Emails will start "
                         f"at {nxt:%H:%M}. You can leave this window open.",
                         "warn")
                told = window
            self.emit("waiting", seconds=(nxt - now).total_seconds(),
                      wait_kind="night")
            await self._sleep(min(HOURS_POLL_SECONDS,
                                  max(0.5, (nxt - now).total_seconds())), stop)
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

    # -- one email each ------------------------------------------------

    async def run(self, plan, variants, state, project, stop, attachment="",
                  subject=""):
        """A separate, private email to each person, with their own name in it.

        Same shape as Sender.run: same events, same sent-record, same log.
        """
        queue = plan["queue"]
        total = len(queue)
        sent = failed = consecutive = 0
        reason = "finished"
        ps = S.project_state(state, project)

        if attachment and not os.path.exists(attachment):
            self.log("The attached file is gone — sending the text only.",
                     "warn")
            attachment = ""

        self.log(f"Email: {total} to send.", "info")

        for i, item in enumerate(queue, 1):
            target, name = item[0], item[1]
            if stop.is_set():
                reason = "stopped by you"
                break
            if not await self._wait_active_hours(stop):
                reason = "stopped by you"
                break
            self.emit("progress", done=i - 1, total=total, sent=sent,
                      failed=failed, channel="Email")

            display = name or target.split("@")[0]
            body = S.render(variants, display)
            subj = S.render([subject], display) if subject else "(no subject)"

            try:
                msg = self._compose([target], target, subj, body, attachment)
                await self._send(msg, [target])
                ps["sent"][target] = datetime.now().isoformat(
                    timespec="seconds")
                S.record_send(state, "email")
                S.log_row(project, target, name, "sent", "email")
                sent += 1
                consecutive = 0
                self.log(f"Emailed {display} ({target})", "good")
            except Exception as e:
                status, message, fatal = explain(e, target, self.cfg)
                self.log(message if fatal else f"{target}: {message}",
                         "bad" if fatal else "warn")
                ps["failed"][target] = status
                S.log_row(project, target, name, status, "email")
                failed += 1
                consecutive += 1
                if fatal:
                    self.emit("email_blocked", text=message)
                    reason = "email settings need fixing"
                    S.save_state(state)
                    break

            S.save_state(state)
            self.emit("progress", done=i, total=total, sent=sent,
                      failed=failed, channel="Email")

            if consecutive >= int(self.cfg.get(
                    "stop_after_consecutive_errors", 5)):
                self.log("Too many failures in a row — stopping so nothing "
                         "else is wasted.", "bad")
                reason = "too many errors in a row"
                break
            if i == total:
                break

            d = random.uniform(float(self.cfg.get("email_delay_min", 20)),
                               float(self.cfg.get("email_delay_max", 60)))
            if not await self._sleep(d, stop, tick=lambda l: self.emit(
                    "waiting", seconds=l, wait_kind="gap")):
                reason = "stopped by you"
                break

        self._close()
        S.save_state(state)
        self.emit("finished", sent=sent, failed=failed, total=total,
                  reason=reason, channel="Email")
        return {"sent": sent, "failed": failed, "reason": reason}

    # -- one email to everyone ----------------------------------------

    async def run_bcc(self, people, variants, state, project, stop,
                      attachment="", subject=""):
        """One message everyone gets, with the addresses hidden from each
        other. Sent in batches because providers refuse very long lists."""
        addr = (self.cfg.get("email_address") or "").strip()
        batch = max(1, int(self.cfg.get("email_bcc_batch", 40)))
        chunks = [people[i:i + batch] for i in range(0, len(people), batch)]
        total = len(people)
        sent = failed = 0
        reason = "finished"
        ps = S.project_state(state, project)

        if attachment and not os.path.exists(attachment):
            self.log("The attached file is gone — sending the text only.",
                     "warn")
            attachment = ""

        # {name} cannot be personalised when everyone shares one message.
        body = S.render(variants, "there")
        subj = S.render([subject], "there") if subject else "(no subject)"

        self.log(f"Email: one message to {total} people"
                 + (f", in {len(chunks)} batches." if len(chunks) > 1 else "."),
                 "info")

        for ci, chunk in enumerate(chunks, 1):
            if stop.is_set():
                reason = "stopped by you"
                break
            if not await self._wait_active_hours(stop):
                reason = "stopped by you"
                break
            rcpts = [t for t, _ in chunk]
            try:
                # "To" is the sender's own address and the real recipients ride
                # only in the envelope. There is deliberately no Bcc header —
                # a Bcc header would travel with the message.
                msg = self._compose(rcpts, addr, subj, body, attachment)
                await self._send(msg, rcpts)
                stamp = datetime.now().isoformat(timespec="seconds")
                for t, n in chunk:
                    ps["sent"][t] = stamp
                    S.record_send(state, "email")
                    S.log_row(project, t, n, "sent", "email")
                sent += len(chunk)
                who = ("1 person" if len(chunk) == 1
                       else f"{len(chunk)} people")
                self.log(f"Sent to {who}"
                         + (f" (batch {ci} of {len(chunks)})."
                            if len(chunks) > 1 else "."), "good")
            except Exception as e:
                status, message, fatal = explain(e, ", ".join(rcpts[:3]),
                                                 self.cfg)
                self.log(message, "bad")
                for t, n in chunk:
                    ps["failed"][t] = status
                    S.log_row(project, t, n, status, "email")
                failed += len(chunk)
                if fatal:
                    self.emit("email_blocked", text=message)
                    reason = "email settings need fixing"
                    S.save_state(state)
                    break

            S.save_state(state)
            self.emit("progress", done=sent + failed, total=total, sent=sent,
                      failed=failed, channel="Email")

            if ci < len(chunks):
                d = random.uniform(float(self.cfg.get("email_delay_min", 20)),
                                   float(self.cfg.get("email_delay_max", 60)))
                if not await self._sleep(d, stop, tick=lambda l: self.emit(
                        "waiting", seconds=l, wait_kind="gap")):
                    reason = "stopped by you"
                    break

        self._close()
        S.save_state(state)
        self.emit("finished", sent=sent, failed=failed, total=total,
                  reason=reason, channel="Email")
        return {"sent": sent, "failed": failed, "reason": reason}
