#!/usr/bin/env python3
"""Self-test for the email side, and a regression alarm for the Telegram side.

Run it with:      ./venv/bin/python tools/selftest_email.py

Standard library only — no pytest, no network, no live account, no real
mailbox. Every file it touches is inside a throwaway temp folder.
"""

import os
import sys
import tempfile

# Point the app's data folder somewhere disposable BEFORE importing sender, so
# a bug in this script can never reach anyone's real lists or login.
_TMP = tempfile.mkdtemp(prefix="sendertest-")
os.environ["TELEGRAM_SENDER_DATA"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio          # noqa: E402
import csv              # noqa: E402
import re               # noqa: E402
import smtplib          # noqa: E402
import socket           # noqa: E402
import socketserver     # noqa: E402
import ssl              # noqa: E402
import threading        # noqa: E402

import sender as S      # noqa: E402
import mailer as M      # noqa: E402

if not os.path.abspath(S.DATA).startswith(os.path.abspath(_TMP)):
    sys.exit(f"REFUSING TO RUN: data folder is {S.DATA}, not the temp folder. "
             f"This test would have touched real data.")

PASS = FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {extra}")


def section(title):
    print(f"\n{title}\n" + "-" * len(title))


class Stop:
    """Stands in for threading.Event."""

    def __init__(self, flag=False):
        self.flag = flag

    def is_set(self):
        return self.flag


def fresh_state():
    return {"projects": {}, "history": {}, "first_use": None}


# ======================================================== 1. pure functions

def test_pure():
    section("1. Recognising an email address")
    for target, expect in [
            ("@john", False), ("john", False), ("+353871234567", False),
            ("353871234567", False), ("12345678", False),
            ("john@example.com", True), ("a@b.c", False),
            ("first.last+tag@sub.example.co.uk", True),
            ("tommy@eircom.net", True), ("", False),
            ("two words@x.com", False), ("no-at-sign.com", False),
            ("trailing@x.com ", True)]:
        check(f"is_email({target!r}) is {expect}",
              S.is_email(target) is expect, f"got {S.is_email(target)}")

    section("2. Splitting a mixed group")
    mixed = [("@ali", "Ali"), ("tom@x.com", "Tom"), ("+353871234567", "Pat"),
             ("mary@y.ie", "Mary"), ("12345678", None)]
    tg, em = S.split_channels(mixed)
    check("telegram side keeps its 3, in order",
          tg == [("@ali", "Ali"), ("+353871234567", "Pat"), ("12345678", None)],
          str(tg))
    check("email side keeps its 2, in order",
          em == [("tom@x.com", "Tom"), ("mary@y.ie", "Mary")], str(em))
    check("nothing is lost or duplicated", len(tg) + len(em) == len(mixed))

    section("3. Typing a person into the box")
    check("an email line parses as target + name",
          S.parse_recipients("john@example.com, John")
          == [("john@example.com", "John")])
    check("duplicate emails are dropped, case-insensitively",
          S.parse_recipients("A@X.com, A\na@x.com, again")
          == [("A@X.com", "A")])
    check("an email with no name is fine",
          S.parse_recipients("solo@x.com") == [("solo@x.com", None)])

    section("4. Guessing the mail server")
    for addr, host in [("x@gmail.com", "smtp.gmail.com"),
                       ("x@hotmail.ie", "smtp-mail.outlook.com"),
                       ("x@yahoo.co.uk", "smtp.mail.yahoo.com"),
                       ("x@eircom.net", "mail1.eircom.net")]:
        g = M.guess_smtp(addr)
        check(f"{addr} -> {host}", g and g[0] == host, str(g))
    check("an unknown provider is left for the user to type",
          M.guess_smtp("x@some-tiny-isp.ie") is None)
    check("gmail is flagged as needing an app password",
          M.needs_app_password("x@gmail.com"))
    check("a small provider is not",
          not M.needs_app_password("x@some-tiny-isp.ie"))


# ============================================ 2. the Telegram regression alarm

def test_telegram_untouched():
    section("5. The Telegram side has not moved")

    state = fresh_state()

    # Emails must never reach the Telegram planner.
    mixed = [("@ali", "Ali"), ("tom@x.com", "Tom"), ("+353871234567", "Pat")]
    tg, _ = S.split_channels(mixed)
    check("no email survives the split into the telegram list",
          not any(S.is_email(t) for t, _ in tg))

    # is_known would call every email a stranger, which is exactly why they
    # must be filtered out before build_plan ever sees them.
    known = (set(), {"ali"}, set())
    check("is_known would wrongly call an email a stranger (so we filter)",
          S.is_known("tom@x.com", known) is False)

    # 200 emails must not move the Telegram counters by one.
    before = S.sent_today(state)
    for _ in range(200):
        S.record_send(state, "email")
    after = S.sent_today(state)
    check("200 emails leave sent_today() unchanged", before == after,
          f"{before} -> {after}")
    check("the emails are counted separately", M.email_sent_today(state) == 200)

    # And the reverse: telegram sends must not move the email counter.
    S.record_send(state, "known")
    S.record_send(state, "new")
    check("telegram sends leave the email count alone",
          M.email_sent_today(state) == 200)
    check("telegram counters did move", S.sent_today(state) == (1, 1))

    # The warm-up ceiling is a Telegram concept and must ignore email.
    ceil = S.warmup_ceiling(state, {"use_warmup": True})
    k, n = S.sent_today(state)
    check("warm-up room is worked out from telegram only",
          ceil - k - n == ceil - 2, f"ceiling {ceil}, k={k} n={n}")

    # The CSV must keep exactly its six original columns.
    S.log_row("Test", "tom@x.com", "Tom", "sent", "email")
    S.log_row("Test", "@ali", "Ali", "sent", "known")
    with open(S.LOG_PATH, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    check("sent_log.csv header is still the original 6 columns",
          rows[0] == ["timestamp", "project", "target", "name", "status",
                      "detail"], str(rows[0]))
    check("every row has 6 fields", all(len(r) == 6 for r in rows),
          str([len(r) for r in rows]))

    rep, summary = S.delivery_report("Test")
    check("the delivery report reads both channels back",
          summary["sent"] == 2, str(summary))
    check("channel is worked out from the address, not the detail column",
          [S.is_email(r["target"]) for r in rep].count(True) == 1)

    # A saved project must round-trip every field, including the new ones.
    data = {"projects": {}, "current": None}
    S.set_project(data, "G", "msg", "a@b.com\n", "", "group",
                  {"id": 7, "title": "T"}, "Subject here",
                  {"telegram": False, "email": True})
    p = S.get_project(data, "G")
    check("subject survives a save", p["subject"] == "Subject here")
    check("channels survive a save", p["channels"]["email"] is True)
    check("mode survives a save", p["mode"] == "group")
    check("post_group survives a save", p["post_group"]["id"] == 7)

    # An old project written before this change must still open.
    old = {"projects": {"Old": {"message": "m", "recipients": "@x"}},
           "current": "Old"}
    q = S.get_project(old, "Old")
    check("a project saved by the old version still opens",
          q["subject"] == "" and q["channels"] is None and
          q["mode"] == "private", str(q))


# ================================================== 3. every failure explained

class Boom:
    """A fake SMTP connection that raises whatever we tell it to."""

    def __init__(self, exc=None, fail_times=0):
        self.exc = exc
        self.fail_times = fail_times
        self.sent = []

    def noop(self):
        return (250, b"ok")

    def send_message(self, msg, to_addrs=None):
        if self.exc is not None and self.fail_times != 0:
            if self.fail_times > 0:
                self.fail_times -= 1
            raise self.exc
        self.sent.append((msg, to_addrs))

    def quit(self):
        pass


def test_explain():
    section("6. Every failure in plain English")

    cases = [
        (smtplib.SMTPAuthenticationError(535, b"bad"), "auth_failed", True),
        (smtplib.SMTPSenderRefused(550, b"no", "me@x.com"),
         "sender_refused", True),
        (smtplib.SMTPRecipientsRefused({"a@b.com": (550, b"no")}),
         "bad_address", False),
        (smtplib.SMTPDataError(552, b"daily quota exceeded"),
         "daily_limit", True),
        (smtplib.SMTPDataError(550, b"looks like spam"), "rejected", False),
        (smtplib.SMTPNotSupportedError("no starttls"), "no_tls", True),
        (smtplib.SMTPServerDisconnected("bye"), "disconnected", False),
        (socket.gaierror("name or service not known"), "no_server", True),
        (ssl.SSLError("handshake"), "ssl_error", True),
        (ConnectionRefusedError("refused"), "refused", True),
        (socket.timeout("timed out"), "timeout", False),
        (ValueError("something odd"), "error", False),
    ]
    cfg = {"email_address": "me@gmail.com", "email_smtp_host": "smtp.x.com",
           "email_smtp_port": 587}
    for exc, want_status, want_fatal in cases:
        status, message, fatal = M.explain(exc, "them@x.com", cfg)
        check(f"{type(exc).__name__} -> {want_status}, fatal={want_fatal}",
              status == want_status and fatal == want_fatal,
              f"got {status}, fatal={fatal}")
        check(f"  ...and the wording is human",
              message and not re.search(r"SMTP\w*Error|Traceback|errno",
                                        message, re.I),
              message[:70])

    status, message, _ = M.explain(
        smtplib.SMTPAuthenticationError(535, b"bad"), "x", cfg)
    check("a wrong password explains app passwords",
          "App Password" in message and "16" in message)


def test_fatal_stops_run():
    section("7. A fatal error stops, a skippable one carries on")

    async def go(exc, fail_times):
        state = fresh_state()
        cfg = dict(S.DEFAULT_CONFIG)
        cfg.update(email_address="me@x.com", email_password="p",
                   email_smtp_host="localhost", email_delay_min=0,
                   email_delay_max=0, active_hours_start="00:00",
                   active_hours_end="23:59")
        events = []
        es = M.EmailSender(cfg, lambda k, **d: events.append((k, d)))
        boom = Boom(exc, fail_times)
        es._ensure = lambda: boom
        es._connect = lambda: boom
        people = [(f"p{i}@x.com", f"P{i}") for i in range(4)]
        plan = M.build_email_plan(people, state, "T", cfg)
        res = await es.run(plan, ["Hi {name}"], state, "T", Stop(),
                           subject="S")
        return res, events, boom

    res, events, boom = asyncio.run(
        go(smtplib.SMTPAuthenticationError(535, b"bad"), -1))
    check("a wrong password stops after the first person",
          res["sent"] == 0 and res["failed"] == 1, str(res))
    check("and the window is told to show it",
          any(k == "email_blocked" for k, _ in events))

    res, events, boom = asyncio.run(
        go(smtplib.SMTPRecipientsRefused({"a": (550, b"x")}), 1))
    check("one bad address is skipped, the other 3 still go",
          res["sent"] == 3 and res["failed"] == 1, str(res))
    check("no blocking dialog for a single bad address",
          not any(k == "email_blocked" for k, _ in events))

    res, events, boom = asyncio.run((lambda: go(None, 0))())
    check("with nothing wrong, all 4 are sent", res["sent"] == 4, str(res))
    check("a finished event names the channel",
          any(k == "finished" and d.get("channel") == "Email"
              for k, d in events))


def test_stop_button():
    section("8. STOP works")

    async def go():
        state = fresh_state()
        cfg = dict(S.DEFAULT_CONFIG)
        cfg.update(email_address="me@x.com", email_password="p",
                   email_smtp_host="localhost", email_delay_min=0,
                   email_delay_max=0, active_hours_start="00:00",
                   active_hours_end="23:59")
        es = M.EmailSender(cfg, lambda k, **d: None)
        boom = Boom()
        es._ensure = lambda: boom
        stop = Stop(True)          # pressed before it starts
        people = [(f"p{i}@x.com", None) for i in range(5)]
        plan = M.build_email_plan(people, state, "T", cfg)
        return await es.run(plan, ["Hi"], state, "T", stop, subject="S")

    res = asyncio.run(go())
    check("nothing is sent when STOP is already pressed", res["sent"] == 0)
    check("and it says so", res["reason"] == "stopped by you", res["reason"])


def test_planning():
    section("9. Who gets an email today")

    cfg = dict(S.DEFAULT_CONFIG)
    cfg["email_daily_cap"] = 3
    state = fresh_state()
    people = [(f"p{i}@x.com", f"P{i}") for i in range(10)]

    plan = M.build_email_plan(people, state, "T", cfg)
    check("today's cap is respected", len(plan["queue"]) == 3, str(plan))
    check("the rest is kept for tomorrow", plan["left_for_later"] == 7)

    # Pretend the first three went.
    ps = S.project_state(state, "T")
    for t, _ in people[:3]:
        ps["sent"][t] = "now"
        S.record_send(state, "email")

    plan = M.build_email_plan(people, state, "T", cfg)
    check("nobody is emailed twice", plan["already_done"] == 3)
    check("and today's allowance is used up", len(plan["queue"]) == 0,
          str(plan))

    state["history"] = {}
    plan = M.build_email_plan(people, state, "T", cfg)
    check("tomorrow it carries on from person 4",
          [t for t, _, _ in plan["queue"]] ==
          ["p3@x.com", "p4@x.com", "p5@x.com"], str(plan["queue"]))


# ================================================ 4. a real conversation

class FakeSMTPHandler(socketserver.StreamRequestHandler):
    """Just enough of SMTP to hold a real conversation over loopback."""

    def handle(self):
        self.wfile.write(b"220 test ESMTP\r\n")
        envelope = {"mail": None, "rcpt": [], "data": b""}
        while True:
            line = self.rfile.readline()
            if not line:
                return
            up = line.strip().upper()
            if up.startswith((b"EHLO", b"HELO")):
                self.wfile.write(b"250-test\r\n250 AUTH PLAIN LOGIN\r\n")
            elif up.startswith(b"AUTH"):
                self.wfile.write(b"235 ok\r\n")
            elif up.startswith(b"MAIL FROM"):
                envelope["mail"] = line.strip()
                self.wfile.write(b"250 ok\r\n")
            elif up.startswith(b"RCPT TO"):
                envelope["rcpt"].append(
                    line.strip().split(b"<")[1].split(b">")[0].decode())
                self.wfile.write(b"250 ok\r\n")
            elif up.startswith(b"DATA"):
                self.wfile.write(b"354 go\r\n")
                body = []
                while True:
                    ln = self.rfile.readline()
                    if ln in (b".\r\n", b".\n", b""):
                        break
                    body.append(ln)
                envelope["data"] = b"".join(body)
                self.server.captured.append(dict(envelope))
                envelope = {"mail": None, "rcpt": [], "data": b""}
                self.wfile.write(b"250 queued\r\n")
            elif up.startswith(b"NOOP"):
                self.wfile.write(b"250 ok\r\n")
            elif up.startswith(b"RSET"):
                self.wfile.write(b"250 ok\r\n")
            elif up.startswith(b"QUIT"):
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


class FakeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def test_end_to_end():
    section("10. A real send, over a real socket")

    srv = FakeServer(("127.0.0.1", 0), FakeSMTPHandler)
    srv.captured = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    cfg = dict(S.DEFAULT_CONFIG)
    cfg.update(email_address="tommy@example.com", email_password="secret",
               email_from_name="Tommy O Gorman", email_smtp_host="127.0.0.1",
               email_smtp_port=port, email_use_tls=False,
               email_delay_min=0, email_delay_max=0,
               active_hours_start="00:00", active_hours_end="23:59",
               email_unsubscribe=True,
               email_unsubscribe_text="Reply STOP to come off the list.")

    people = [("john@x.com", "John"), ("mary@y.ie", "Mary"),
              ("seamus@z.ie", "Séamus")]

    async def go():
        state = fresh_state()
        es = M.EmailSender(cfg, lambda k, **d: None)
        plan = M.build_email_plan(people, state, "T", cfg)
        return await es.run(plan, ["Hi {name},\n\nCome along on Sunday."],
                            state, "T", Stop(), subject="Sunday meeting")

    res = asyncio.run(go())
    check("all three were accepted by the server", res["sent"] == 3, str(res))
    check("the server saw three separate messages", len(srv.captured) == 3,
          str(len(srv.captured)))

    for cap, (addr, name) in zip(srv.captured, people):
        raw = cap["data"].decode("utf-8", "replace")
        check(f"{name}: went to only that one address",
              cap["rcpt"] == [addr], str(cap["rcpt"]))
        check(f"{name}: the To line is theirs", f"To: {addr}" in raw,
              raw.split("\n")[0][:60])
        check(f"{name}: has the subject", "Subject: Sunday meeting" in raw)
        check(f"{name}: no {{name}} left unreplaced", "{name}" not in raw)
        check(f"{name}: the unsubscribe line is there",
              "Reply STOP to come off the list." in
              raw.replace("=\r\n", "").replace("=\n", ""))
        check(f"{name}: has the List-Unsubscribe header",
              "List-Unsubscribe:" in raw)

    first = srv.captured[0]["data"].decode("utf-8", "replace")
    check("the name really is substituted per person", "Hi John," in first,
          first[-200:].replace("\r\n", " ")[:80])
    third = srv.captured[2]["data"]
    # Séamus is non-ASCII, so it must be encoded, never mangled or crashed.
    check("an accented name survives",
          b"S=C3=A9amus" in third or "Séamus".encode() in third
          or b"U6VhbXVz" in third, repr(third[-160:]))

    # --------- one message to everyone, addresses hidden
    srv.captured.clear()
    many = [(f"p{i}@x.com", f"P{i}") for i in range(9)]
    cfg["email_bcc_batch"] = 4

    async def go_bcc():
        state = fresh_state()
        es = M.EmailSender(cfg, lambda k, **d: None)
        return await es.run_bcc(many, ["Hello everyone."], state, "T", Stop(),
                                subject="News")

    res = asyncio.run(go_bcc())
    check("everyone got it", res["sent"] == 9, str(res))
    check("sent in batches of 4, so 3 messages", len(srv.captured) == 3,
          str(len(srv.captured)))
    check("every address was delivered to",
          sorted(a for c in srv.captured for a in c["rcpt"])
          == sorted(t for t, _ in many))
    for cap in srv.captured:
        raw = cap["data"].decode("utf-8", "replace")
        check("no Bcc header travels with the message",
              "bcc:" not in raw.lower(), raw[:100])
        check("nobody sees anyone else's address",
              not any(f"{t}" in raw for t, _ in many),
              [t for t, _ in many if t in raw])
        check("it is addressed to the sender themselves",
              "To: tommy@example.com" in raw)

    # --------- with a photo attached
    srv.captured.clear()
    photo = os.path.join(_TMP, "flyer.png")
    with open(photo, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 400)

    async def go_attach():
        state = fresh_state()
        es = M.EmailSender(cfg, lambda k, **d: None)
        plan = M.build_email_plan([("john@x.com", "John")], state, "T", cfg)
        return await es.run(plan, ["See the flyer, {name}."], state, "T",
                            Stop(), photo, "Our flyer")

    res = asyncio.run(go_attach())
    check("an email with a photo goes out", res["sent"] == 1, str(res))
    raw = srv.captured[0]["data"].decode("utf-8", "replace")
    check("the file name travels with it", "flyer.png" in raw)
    check("it is sent as an attachment",
          "Content-Disposition: attachment" in raw)
    check("the message text is still there", "See the flyer, John." in
          raw.replace("=\r\n", "").replace("=\n", ""))

    # --------- a file too big for email
    big = os.path.join(_TMP, "big.bin")
    with open(big, "wb") as f:
        f.seek(M.MAX_ATTACHMENT_BYTES + 1024)
        f.write(b"\0")
    srv.captured.clear()

    async def go_big():
        state = fresh_state()
        es = M.EmailSender(cfg, lambda k, **d: None)
        plan = M.build_email_plan([("john@x.com", "John")], state, "T", cfg)
        return await es.run(plan, ["Hi"], state, "T", Stop(), big, "Big")

    res = asyncio.run(go_big())
    check("a file too big does not crash it", res["failed"] == 1, str(res))
    check("and nothing half-sent went out", len(srv.captured) == 0)

    # --------- the subject can be personalised too
    srv.captured.clear()

    async def go_subj():
        state = fresh_state()
        es = M.EmailSender(cfg, lambda k, **d: None)
        plan = M.build_email_plan([("john@x.com", "John"),
                                   ("mary@y.ie", "Mary")], state, "T", cfg)
        return await es.run(plan, ["Hello."], state, "T", Stop(),
                            subject="A message for {name}")

    asyncio.run(go_subj())
    subs = [c["data"].decode("utf-8", "replace") for c in srv.captured]
    check("the subject is personalised per person",
          any("Subject: A message for John" in s for s in subs)
          and any("Subject: A message for Mary" in s for s in subs),
          str([s.split("Subject:")[1].split("\n")[0] for s in subs
               if "Subject:" in s]))

    # --------- somebody with no name saved
    srv.captured.clear()

    async def go_noname():
        state = fresh_state()
        es = M.EmailSender(cfg, lambda k, **d: None)
        plan = M.build_email_plan([("patrick.byrne@x.com", None)], state, "T",
                                  cfg)
        return await es.run(plan, ["Hi {name},"], state, "T", Stop(),
                            subject="Hello")

    asyncio.run(go_noname())
    body = srv.captured[0]["data"].decode("utf-8", "replace")
    check("someone with no name still gets a sensible greeting",
          "{name}" not in body and "Hi patrick.byrne," in body,
          body[-120:].replace("\r\n", " "))

    # --------- the test-email button
    async def go_test():
        es = M.EmailSender(cfg, lambda k, **d: None)
        return await es.test_connection("tommy@example.com")

    srv.captured.clear()
    ok, message = asyncio.run(go_test())
    check("the test email button works", ok, message)
    check("and actually sent something", len(srv.captured) == 1)

    srv.shutdown()
    srv.server_close()


def test_bad_server():
    section("11. A mail server that is not there")

    cfg = dict(S.DEFAULT_CONFIG)
    cfg.update(email_address="me@example.com", email_password="p",
               email_smtp_host="127.0.0.1", email_smtp_port=9,
               email_use_tls=False)

    async def go():
        es = M.EmailSender(cfg, lambda k, **d: None)
        return await es.test_connection()

    ok, message = asyncio.run(go())
    check("it fails cleanly instead of crashing", ok is False)
    check("and says something a person can act on",
          not re.search(r"Traceback|Errno|Exception", message), message[:80])
    print(f"        message shown: {message.splitlines()[0][:70]}")


def main():
    print(f"Testing in {_TMP}")
    test_pure()
    test_telegram_untouched()
    test_explain()
    test_fatal_stops_run()
    test_stop_button()
    test_planning()
    test_end_to_end()
    test_bad_server()

    print(f"\n{'=' * 50}\n  {PASS} passed, {FAIL} failed\n{'=' * 50}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
