#!/usr/bin/env python3
"""Drives the real window with a stand-in Telegram, and checks nothing broke.

Run it with:      ./venv/bin/python tools/selftest_ui.py

Needs a screen (it really opens the window, off to one side). Every file it
touches is inside a throwaway temp folder, so it can never see real data.
"""

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="uitest-")
os.environ["TELEGRAM_SENDER_DATA"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio          # noqa: E402
import tkinter as tk    # noqa: E402
from tkinter import messagebox, filedialog, simpledialog   # noqa: E402

import sender as S      # noqa: E402
import mailer as M      # noqa: E402

if not os.path.abspath(S.DATA).startswith(os.path.abspath(_TMP)):
    sys.exit(f"REFUSING TO RUN: data folder is {S.DATA}")

# Give it credentials so it goes straight past the login screen.
S.save_config(dict(S.DEFAULT_CONFIG, api_id=1, api_hash="x" * 32,
                   email_address="tommy@example.com", email_password="pw",
                   email_smtp_host="127.0.0.1", email_smtp_port=2525,
                   email_use_tls=False, email_delay_min=0, email_delay_max=0,
                   active_hours_start="00:00", active_hours_end="23:59"))

import app as A         # noqa: E402

PASS = FAIL = 0
ANSWERS = {}            # what the pop-up boxes should answer
SEEN = []               # every pop-up that appeared


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {extra}")


def section(t):
    print(f"\n{t}\n" + "-" * len(t))


# ---- swallow every dialog so the test never blocks on a click
def _box(kind):
    def f(title=None, message=None, **kw):
        SEEN.append((kind, title, message))
        return ANSWERS.get(title, True if kind == "askyesno" else "ok")
    return f


for name in ("showinfo", "showwarning", "showerror"):
    setattr(messagebox, name, _box(name))
messagebox.askyesno = _box("askyesno")
simpledialog.askstring = lambda *a, **k: ANSWERS.get("askstring", "x")
filedialog.askopenfilename = lambda *a, **k: ANSWERS.get("openfile", "")


class FakeSender:
    """Stands in for the Telegram engine. Sends nothing, anywhere."""

    def __init__(self, cfg, emit):
        self.cfg, self.emit = cfg, emit
        self.ran = []

    async def connect(self):
        return {"state": "ready", "name": "Tommy", "username": "tommy",
                "id": 1}

    async def build_plan(self, recipients, state, project):
        ps = S.project_state(state, project)
        todo = [(t, n) for t, n in recipients if t not in ps["sent"]]
        return {"queue": [(t, n, "known") for t, n in todo],
                "known_total": len(todo), "new_total": 0,
                "known_queued": len(todo), "new_queued": 0,
                "known_today": 0, "new_today": 0, "warmup_ceiling": None,
                "left_for_later": 0,
                "already_done": len(recipients) - len(todo), "hours": 0.1}

    async def run(self, plan, variants, state, project, stop, attachment=""):
        self.ran.append(("telegram", len(plan["queue"])))
        ps = S.project_state(state, project)
        for t, n, cls in plan["queue"]:
            ps["sent"][t] = "now"
            S.record_send(state, "known")
            S.log_row(project, t, n, "sent", cls)
        S.save_state(state)
        self.emit("finished", sent=len(plan["queue"]), failed=0,
                  total=len(plan["queue"]), reason="finished")
        return {}

    async def spambot_status(self):
        return True, "Good news, no limits are currently applied."

    async def post_to_group(self, gid, text, attachment=""):
        self.ran.append(("post", gid))
        return "The Group"

    async def list_groups(self):
        return [{"title": "The Group", "id": 42}]

    async def group_members(self, gid):
        return [("@one", "One"), ("@two", "Two")]

    async def disconnect(self):
        pass


class FakeEmail:
    """Stands in for the email engine. Sends nothing, anywhere."""
    calls = []

    def __init__(self, cfg, emit):
        self.cfg, self.emit = cfg, emit

    async def run(self, plan, variants, state, project, stop, attachment="",
                  subject=""):
        FakeEmail.calls.append(("run", [t for t, _, _ in plan["queue"]],
                                subject))
        ps = S.project_state(state, project)
        for t, n, _ in plan["queue"]:
            ps["sent"][t] = "now"
            S.record_send(state, "email")
            S.log_row(project, t, n, "sent", "email")
        S.save_state(state)
        self.emit("finished", sent=len(plan["queue"]), failed=0,
                  total=len(plan["queue"]), reason="finished", channel="Email")
        return {}

    async def run_bcc(self, people, variants, state, project, stop,
                      attachment="", subject=""):
        FakeEmail.calls.append(("bcc", [t for t, _ in people], subject))
        self.emit("finished", sent=len(people), failed=0, total=len(people),
                  reason="finished", channel="Email")
        return {}

    async def test_connection(self, to=None):
        return True, "Success."


A.S.Sender = FakeSender
A.M.EmailSender = FakeEmail


def pump(app, times=40):
    """Let the window and the background loop both get a turn."""
    for _ in range(times):
        app.root.update()
        app.root.update_idletasks()
        import time
        time.sleep(0.02)


def main():
    global PASS, FAIL
    print(f"Testing in {_TMP}")

    root = tk.Tk()
    root.geometry("1120x780+2000+2000")     # out of the way
    app = A.App(root)
    pump(app)

    section("1. It opens and logs in")
    check("the dashboard is showing", app.main.winfo_ismapped())
    check("Telegram reports as connected", app.tg_ready)

    section("2. Adding people of both kinds")
    app.e_add.insert(0, "+353871234567, John")
    app._on_add_person()
    app.e_add.insert(0, "mary@example.com, Mary")
    app._on_add_person()
    app.e_add.insert(0, "@seamus, Seamus")
    app._on_add_person()
    check("three people are in the group", len(app.members) == 3,
          str(app.members))

    tg, em = S.split_channels(app.members)
    check("two are Telegram, one is email", len(tg) == 2 and len(em) == 1,
          f"{tg} / {em}")
    check("the list shows an email marker",
          any(l.strip().startswith("@ ") for l in
              app.lst_members.get(0, "end")),
          str(list(app.lst_members.get(0, "end"))))

    section("3. One person, phone and email at once")
    app.e_add.insert(0, "+353861111111, Pat, pat@example.com")
    app._on_add_person()
    check("it made both entries", len(app.members) == 5, str(app.members))
    check("the phone one is there", ("+353861111111", "Pat") in app.members)
    check("the email one is there too",
          ("pat@example.com", "Pat") in app.members)

    section("4. It remembers everything after a restart")
    app.t_msg.insert("1.0", "Hi {name}, see you Sunday.")
    app.e_subject.insert(0, "Sunday meeting")
    app.v_email.set(True)
    app.v_tg.set(True)
    app._save_current_project()

    p = S.get_project(S.load_projects(), app.current)
    check("the subject was written to disk", p["subject"] == "Sunday meeting",
          str(p.get("subject")))
    check("both channels were written to disk",
          p["channels"] == {"telegram": True, "email": True},
          str(p.get("channels")))
    check("the people were written to disk",
          len(S.parse_recipients(p["recipients"])) == 5)

    app._load_current_project()
    check("it reads back the subject",
          app.e_subject.get() == "Sunday meeting", app.e_subject.get())
    check("it reads back both ticks", app.v_tg.get() and app.v_email.get())

    section("5. Email only")
    FakeEmail.calls.clear()
    app.v_tg.set(False)
    app.v_email.set(True)
    app._on_channel_change()
    pump(app)
    app._on_send()
    pump(app, 60)

    check("the email engine was used", any(c[0] == "run"
                                           for c in FakeEmail.calls),
          str(FakeEmail.calls))
    check("Telegram was NOT used", not app.backend.sender.ran,
          str(app.backend.sender.ran))
    sent_to = FakeEmail.calls[0][1] if FakeEmail.calls else []
    check("only the two email addresses were queued",
          sorted(sent_to) == ["mary@example.com", "pat@example.com"],
          str(sent_to))
    check("no phone number leaked into the email queue",
          not any(t.startswith("+") for t in sent_to), str(sent_to))
    check("the subject went with it",
          FakeEmail.calls and FakeEmail.calls[0][2] == "Sunday meeting")
    check("the buttons are usable again",
          str(app.b_send["state"]) == "normal", app.b_send["state"])

    section("6. Both channels, one after the other")
    S.save_state(S.reset_project_state(S.load_state(), app.current))
    app.state = S.load_state()
    FakeEmail.calls.clear()
    app.backend.sender.ran.clear()
    app.v_tg.set(True)
    app.v_email.set(True)
    app._on_channel_change()
    app._on_send()
    pump(app, 80)

    check("Telegram ran", any(c[0] == "telegram"
                              for c in app.backend.sender.ran),
          str(app.backend.sender.ran))
    check("email ran too", any(c[0] == "run" for c in FakeEmail.calls),
          str(FakeEmail.calls))
    check("Telegram got only the 3 Telegram people",
          app.backend.sender.ran[0][1] == 3, str(app.backend.sender.ran))
    check("both legs are counted", len(app.leg_results) == 2,
          str(app.leg_results))
    check("SEND is enabled once, at the very end",
          str(app.b_send["state"]) == "normal" and app.legs_left == 0)
    check("it is no longer marked as running", not app.running)

    section("7. Nobody is messaged twice")
    FakeEmail.calls.clear()
    app.backend.sender.ran.clear()
    ANSWERS["They have already had a message"] = False
    app._on_send()
    pump(app, 60)
    check("nothing was sent again",
          not FakeEmail.calls and not app.backend.sender.ran,
          f"{FakeEmail.calls} {app.backend.sender.ran}")
    check("and it offered to resend instead",
          any(t == "They have already had a message" for _, t, _ in SEEN))
    ANSWERS.pop("They have already had a message")

    section("7b. It says WHY somebody was skipped")
    # Tommy's exact report: 2 people in the group, "Sent 1 of 1", and no
    # explanation of where the other one went.
    # its own group, so it cannot disturb what the later checks look at
    was = app.current
    S.set_project(app.projects, "Tommy case", "hi",
                  "designertommy1@gmail.com, Tommy\n"
                  "eugenebyrne24@gmail.com, Eug\n")
    st = S.load_state()
    S.project_state(st, "Tommy case")["sent"]["designertommy1@gmail.com"] = "y"
    S.save_state(st)
    app.state = st

    logged = []
    real_log = app.log
    app.log = lambda m, lv="info": (logged.append(m), real_log(m, lv))
    ep = M.build_email_plan([("designertommy1@gmail.com", "Tommy"),
                             ("eugenebyrne24@gmail.com", "Eug")],
                            app.state, "Tommy case", app.cfg)
    app._show_email_plan(ep, ["hi"])
    app.log = real_log
    check("only the new person is queued", len(ep["queue"]) == 1, str(ep))
    check("it counts the one already done", ep["already_done"] == 1)
    check("and it SAYS so in the log",
          any("already had an email" in m for m in logged), str(logged))
    check("and points at Send again to everyone",
          any("Send again to everyone" in m for m in logged))
    app.current = was
    app.projects = S.load_projects()
    app._load_current_project()

    section("8. One message everyone sees")
    FakeEmail.calls.clear()
    app.backend.sender.ran.clear()
    app.post_group = {"id": 42, "title": "The Group"}
    app.v_mode.set("group")
    app._on_send()
    pump(app, 80)
    check("it posted in the Telegram group",
          any(c[0] == "post" for c in app.backend.sender.ran),
          str(app.backend.sender.ran))
    check("and emailed everyone at once",
          any(c[0] == "bcc" for c in FakeEmail.calls), str(FakeEmail.calls))
    bcc_to = [c for c in FakeEmail.calls if c[0] == "bcc"]
    check("only email addresses went into that",
          bcc_to and all(S.is_email(t) for t in bcc_to[0][1]),
          str(bcc_to))
    app.v_mode.set("private")

    section("9. Why is nothing sending?")

    def diagnose():
        issues = []
        real = app._show_diagnosis
        app._show_diagnosis = lambda i, f: issues.extend(i)
        app._on_diagnose()
        app._show_diagnosis = real
        return " || ".join(issues)

    app.v_tg.set(False)
    app.v_email.set(False)
    check("it spots that no channel is ticked",
          "Neither Telegram nor Email" in diagnose())

    app.v_email.set(True)
    app.e_subject.delete(0, "end")
    check("it spots a missing subject", "subject" in diagnose().lower())
    app.e_subject.insert(0, "Sunday meeting")

    saved_pw = app.cfg["email_password"]
    app.cfg["email_password"] = ""
    d = diagnose()
    check("it spots a missing password", "password" in d.lower(), d[:90])
    check("and mentions App Passwords", "App Password" in d)
    app.cfg["email_password"] = saved_pw

    app.cfg["email_daily_cap"] = 1
    app.state["history"] = {S._today(): {"email": 99}}
    check("it spots the daily email limit",
          "email limit" in diagnose().lower())
    app.cfg["email_daily_cap"] = 200
    app.state = S.load_state()

    app.v_tg.set(True)
    app.tg_ready = False
    check("it spots Telegram not being connected",
          "not connected" in diagnose().lower())
    app.tg_ready = True

    section("10. The delivery report")
    rows, summary = S.delivery_report(app.current)
    check("it recorded both channels", summary["sent"] > 0, str(summary))
    emails = [r for r in rows if S.is_email(r["target"])]
    tgs = [r for r in rows if not S.is_email(r["target"])]
    check("email rows are recognised", len(emails) >= 2, str(len(emails)))
    check("telegram rows are recognised", len(tgs) >= 2, str(len(tgs)))
    import csv as _csv
    with open(S.LOG_PATH, encoding="utf-8") as f:
        raw = list(_csv.reader(f))
    check("the CSV still has exactly its original 6 columns",
          raw[0] == ["timestamp", "project", "target", "name", "status",
                     "detail"], str(raw[0]))
    check("and every row has 6 fields", all(len(r) == 6 for r in raw),
          str(sorted({len(r) for r in raw})))
    app._on_report()
    pump(app)
    check("the report window opens without error", True)

    section("11. Groups still work")
    before = len(S.project_names(app.projects))
    ANSWERS["askstring"] = "Second list"
    app._on_new_project()
    pump(app)
    check("a new group was made",
          len(S.project_names(app.projects)) == before + 1)
    check("the new group is empty and selected",
          app.current == "Second list" and not app.members)

    app.e_add.insert(0, "new@example.com, New Person")
    app._on_add_person()
    check("a person can be added to it", len(app.members) == 1)

    app._save_current_project()
    app.current = "My first list"
    app._load_current_project()
    check("the first group still has its 5 people", len(app.members) == 5,
          str(len(app.members)))
    check("and still has its message",
          "Sunday" in app.t_msg.get("1.0", "end"))

    section("12. However he happens to type it")
    for typed, want in [
            ("john@x.com, John", [("john@x.com", "John")]),
            ("John, john@x.com", [("john@x.com", "John")]),
            ("John Murphy, john@x.com", [("john@x.com", "John Murphy")]),
            ("+353871234567, John, john@x.com",
             [("+353871234567", "John"), ("john@x.com", "John")]),
            ("john@x.com, John, +353871234567",
             [("+353871234567", "John"), ("john@x.com", "John")]),
            ("@ali, Ali", [("@ali", "Ali")]),
            ("+353871234567", [("+353871234567", None)]),
            ("a@b.com; c@d.com", [("a@b.com", None), ("c@d.com", None)]),
            ("john@x.com", [("john@x.com", None)])]:
        got = app._parse_add_box(typed)
        check(f"{typed!r}", got == want, f"got {got}")
    check("no name is ever used as the address",
          not any(not S.is_email(t) and not t.startswith(("@", "+"))
                  and not t.isdigit()
                  for t, _ in app._parse_add_box("Mary, mary@y.ie")))

    section("13. Reading a contacts file")
    check("Name,Email order is understood",
          app._rows_from_file("John Murphy,john@x.com\nMary,mary@y.ie")
          == [("john@x.com", "John Murphy"), ("mary@y.ie", "Mary")],
          str(app._rows_from_file("John Murphy,john@x.com")))
    check("a header row is skipped",
          app._rows_from_file("Name,Email\nJohn,john@x.com")
          == [("john@x.com", "John")])
    check("the old Telegram order still works",
          app._rows_from_file("+353871234567, John")
          == [("+353871234567", "John")])

    section("14. Email-only, with no Telegram at all")
    app.tg_ready = False
    app.backend.sender = None
    ok = app._need_telegram()
    check("Telegram-only buttons refuse politely instead of crashing",
          ok is False)
    app._on_import_group()
    app._on_health()
    app._on_pick_post_group()
    check("and none of them crashed", True)

    root.destroy()
    print(f"\n{'=' * 50}\n  {PASS} passed, {FAIL} failed\n{'=' * 50}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
