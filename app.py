#!/usr/bin/env python3
"""
Telegram Sender — the window.

Laid out like Telegram: groups down the left, the people in that group and the
message composer on the right. Pick a group, type a message, press Send.
"""

import asyncio
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import sender as S
import mailer as M

# Telegram Desktop's light palette
BG = "#f0f2f5"
PANEL = "#ffffff"
ACCENT = "#3390ec"
ACCENT_DARK = "#2b7cd3"
GREEN = "#4dcd5e"
GREEN_DARK = "#3ab34a"
RED = "#e15052"
TEXT = "#0f0f0f"
MUTED = "#707579"
LINE = "#e4e6eb"
HOVER = "#f4f4f5"
GOOD = "#279f43"
WARN = "#a15c00"
BAD = "#c0392b"

if sys.platform.startswith("win"):
    UI, MONO = "Segoe UI", "Consolas"
elif sys.platform == "darwin":
    UI, MONO = "Helvetica Neue", "Menlo"
else:
    UI, MONO = "DejaVu Sans", "DejaVu Sans Mono"


def F(size=11, bold=False):
    return (UI, size, "bold") if bold else (UI, size)


def flat_btn(parent, text, cmd, bg=None, fg=None, size=10, bold=False, pad=10):
    b = tk.Button(parent, text=text, command=cmd, relief="flat", bd=0,
                  cursor="hand2", font=F(size, bold),
                  bg=bg or PANEL, fg=fg or ACCENT,
                  activebackground=HOVER, activeforeground=fg or ACCENT_DARK,
                  padx=pad, pady=5, highlightthickness=0)
    return b


class Backend:
    def __init__(self, emit):
        self.emit = emit
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()
        self.sender = None
        self.stop_flag = threading.Event()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, on_done=None, on_error=None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def watch():
            try:
                res = fut.result()
            except Exception as e:
                if on_error:
                    self.emit("_call", fn=on_error, arg=e)
                return
            if on_done:
                self.emit("_call", fn=on_done, arg=res)

        threading.Thread(target=watch, daemon=True).start()
        return fut


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.migrated = S.migrate_data()   # must run before anything is loaded
        self.cfg = S.load_config()
        self.state = S.load_state()
        self.projects = S.load_projects()
        self.current = self.projects.get("current") or \
            S.project_names(self.projects)[0]
        self.backend = Backend(self.emit)
        self.plan = None
        self.running = False
        self.attachment = ""
        self.post_group = None
        self.members = []          # [(target, name)] for the current group
        self.save_failed = False
        self.tg_ready = False      # only true once Telegram is really logged in
        self.email_plan = None
        self.legs_left = 0         # how many channels are still sending
        self.leg_results = []

        root.title("Telegram Sender")
        # Tall enough that the settings row and the log are both on screen on a
        # normal laptop. Everything still fits down to the minimum size.
        # Fit the screen we are actually on. A fixed size taller than the
        # laptop screen would push the SEND button off the bottom.
        root.geometry(f"{min(1120, root.winfo_screenwidth() - 60)}"
                      f"x{min(860, root.winfo_screenheight() - 90)}")
        root.minsize(900, 600)
        root.configure(bg=BG)

        self._header()
        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self._setup_screen()
        self._main_screen()

        self.root.after(80, self._drain)
        self.root.after(2000, self._heartbeat)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if S.config_ready(self.cfg):
            self.show_setup("Connecting to Telegram…")
            self._try_connect()
        elif self.cfg.get("skip_telegram"):
            # email-only, or a Telegram login that has expired: never leave
            # anyone stranded on the login screen away from their lists
            self.show_main()
        else:
            self.show_setup("Let's connect your Telegram account. One time only.")

    def emit(self, kind, **data):
        self.q.put((kind, data))

    # ================================================================ header

    def _header(self):
        h = tk.Frame(self.root, bg=ACCENT, height=54)
        h.pack(fill="x")
        h.pack_propagate(False)
        tk.Label(h, text="Telegram Sender", bg=ACCENT, fg="white",
                 font=F(15, True)).pack(side="left", padx=18)
        self.who = tk.Label(h, text="", bg=ACCENT, fg="#d9ecff", font=F(10))
        self.who.pack(side="right", padx=18)
        # The way back in when Telegram is not connected — without this an
        # expired login leaves you stuck on the setup screen.
        self.b_connect_tg = tk.Button(
            h, text="Connect Telegram", command=lambda: self.show_setup(
                "Let's connect your Telegram account."),
            bg=ACCENT, fg="white", font=F(10, True), relief="flat", bd=0,
            cursor="hand2", padx=12, activeforeground="white",
            activebackground=ACCENT_DARK, highlightthickness=0)

    # ================================================================ setup

    def _setup_screen(self):
        self.setup = tk.Frame(self.body, bg=BG)
        wrap = tk.Frame(self.setup, bg=PANEL, highlightbackground=LINE,
                        highlightthickness=1)
        wrap.pack(padx=40, pady=30, fill="x")

        tk.Label(wrap, text="Connect your Telegram account", bg=PANEL, fg=TEXT,
                 font=F(15, True)).pack(anchor="w", padx=24, pady=(20, 4))
        self.setup_msg = tk.Label(wrap, text="", bg=PANEL, fg=MUTED,
                                  justify="left", wraplength=820, font=F(11))
        self.setup_msg.pack(anchor="w", padx=24, pady=(0, 12))

        self.api_help = tk.Frame(wrap, bg=PANEL)
        self.api_help.pack(anchor="w", fill="x")
        tk.Label(self.api_help, text=(
            "1.  Click the button below — it opens my.telegram.org\n"
            "2.  Log in with your phone number (the code arrives inside the "
            "Telegram app, not by SMS)\n"
            "3.  Click \"API development tools\".   App title: mysender    "
            "Short name: mysender    Platform: Desktop\n"
            "4.  Copy the two values it gives you into the boxes below"),
            bg=PANEL, fg=TEXT, justify="left", wraplength=820,
            font=F(11)).pack(anchor="w", padx=24)
        b = tk.Button(self.api_help, text="Open my.telegram.org", bg=ACCENT,
                      fg="white", font=F(11, True), relief="flat", bd=0,
                      cursor="hand2", padx=16, pady=7,
                      activebackground=ACCENT_DARK, activeforeground="white",
                      command=lambda: webbrowser.open("https://my.telegram.org"))
        b.pack(anchor="w", padx=24, pady=14)

        form = tk.Frame(wrap, bg=PANEL)
        form.pack(anchor="w", padx=24, pady=(0, 8))
        self.api_rows = form
        self.e_api_id = self._field(form, 0, "api_id", 34,
                                    str(self.cfg.get("api_id") or ""))
        h = self.cfg.get("api_hash", "")
        self.e_api_hash = self._field(form, 1, "api_hash", 50,
                                      "" if "PUT_YOUR" in str(h) else h)
        self.e_phone = self._field(form, 2, "Phone number", 34,
                                   self.cfg.get("last_phone", ""))
        tk.Label(form, text="with country code, e.g. +353871234567", bg=PANEL,
                 fg=MUTED, font=F(9)).grid(row=3, column=1, sticky="w",
                                           pady=(0, 4))

        self.b_connect = tk.Button(wrap, text="Connect", bg=GREEN, fg="white",
                                   font=F(12, True), relief="flat", bd=0,
                                   cursor="hand2", padx=28, pady=8,
                                   activebackground=GREEN_DARK,
                                   activeforeground="white",
                                   command=self._on_connect)
        self.b_connect.pack(anchor="w", padx=24, pady=(6, 6))

        flat_btn(wrap, "Skip — I only want to send email", self._on_skip_tg,
                 pad=6).pack(anchor="w", padx=24, pady=(0, 18))

        self.setup_status = tk.Label(self.setup, text="", bg=BG, fg=MUTED,
                                     wraplength=860, justify="left", font=F(11))
        self.setup_status.pack(anchor="w", padx=44)

    def _on_skip_tg(self):
        self.cfg["skip_telegram"] = True
        try:
            S.save_config(self.cfg)
        except S.SaveError:
            pass
        self.show_main()
        self.log("Telegram is not connected. Email sending works without it — "
                 "press \"Connect Telegram\" at the top any time.", "warn")

    def _need_telegram(self):
        """True if Telegram is usable. Explains itself if not."""
        if self.tg_ready and self.backend.sender is not None:
            return True
        messagebox.showinfo(
            "Telegram is not connected",
            "This part needs your Telegram account.\n\nPress \"Connect "
            "Telegram\" at the top right to log in.\n\nSending email works "
            "without it.")
        return False

    def _field(self, parent, row, label, width, value):
        tk.Label(parent, text=label, bg=PANEL, fg=MUTED, width=14, anchor="w",
                 font=F(10)).grid(row=row, column=0, pady=5, sticky="w")
        e = tk.Entry(parent, width=width, font=F(11), relief="flat",
                     bg="#f5f6f7", highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=ACCENT)
        e.grid(row=row, column=1, pady=5, ipady=5)
        e.insert(0, value)
        return e

    def preconfigured(self):
        return S.config_ready(self.cfg)

    def show_setup(self, msg=""):
        if hasattr(self, "main"):
            self.main.pack_forget()
        self.setup.pack(fill="both", expand=True)
        if self.preconfigured():
            self.api_help.pack_forget()
            for w in (self.e_api_id, self.e_api_hash):
                w.grid_remove()
            for lbl in self.api_rows.grid_slaves(row=0) + \
                    self.api_rows.grid_slaves(row=1):
                lbl.grid_remove()
            if not msg:
                msg = ("Type your phone number and click Connect.\n\nTelegram "
                       "sends a login code inside the Telegram app — not by SMS.")
        self.setup_msg.config(text=msg)

    def _on_connect(self):
        pre = self.preconfigured()
        api_id = str(self.cfg["api_id"]) if pre else self.e_api_id.get().strip()
        api_hash = self.cfg["api_hash"] if pre else self.e_api_hash.get().strip()
        phone = self.e_phone.get().strip()
        if not api_id.isdigit() or not api_hash:
            messagebox.showwarning("Almost there", "Paste both api_id (numbers "
                                   "only) and api_hash from my.telegram.org.")
            return
        if not phone:
            messagebox.showwarning("Almost there", "Enter your phone number "
                                   "with the country code, e.g. +353871234567")
            return
        self.cfg.update(api_id=int(api_id), api_hash=api_hash, last_phone=phone)
        S.save_config(self.cfg)
        self.b_connect.config(state="disabled")
        self.setup_status.config(text="Connecting…", fg=MUTED)
        self._try_connect(phone)

    def _try_connect(self, phone=None):
        self.backend.sender = S.Sender(self.cfg, self.emit)

        async def go():
            res = await self.backend.sender.connect()
            if res["state"] == "need_phone" and phone:
                return await self.backend.sender.send_code(phone)
            return res

        self.backend.submit(go(), self._after_connect, self._connect_error)

    def _connect_error(self, e):
        self.b_connect.config(state="normal")
        self.setup_status.config(text=f"Could not connect: {e}", fg=BAD)

    def _after_connect(self, res):
        st = res["state"]
        if st == "ready":
            self.tg_ready = True
            self.who.config(text=f"Sending as {res['name']}"
                            + (f"  ·  @{res['username']}"
                               if res.get("username") else ""))
            self.show_main()
        elif st == "need_phone":
            self.b_connect.config(state="normal")
            self.setup_msg.config(
                text="Type your phone number and click Connect.\n\nTelegram "
                     "sends a login code inside the Telegram app — not by SMS."
                if self.preconfigured() else
                "Let's connect your Telegram account. One time only.")
            self.setup_status.config(text="")
        elif st == "need_code":
            code = simpledialog.askstring(
                "Login code",
                "Open the Telegram app — the code arrives there as a message "
                "from Telegram, NOT by SMS.\n\nType it here:", parent=self.root)
            if not code:
                self.b_connect.config(state="normal")
                return
            self.setup_status.config(text="Checking the code…", fg=MUTED)
            self.backend.submit(self.backend.sender.sign_in(code=code.strip()),
                                self._after_connect, self._connect_error)
        elif st == "need_password":
            pw = simpledialog.askstring(
                "Two-step password",
                "This account has two-step verification.\nType your Telegram "
                "password (not the code):", show="*", parent=self.root)
            if not pw:
                self.b_connect.config(state="normal")
                return
            self.backend.submit(self.backend.sender.sign_in(password=pw),
                                self._after_connect, self._connect_error)

    # ================================================================ main

    def _main_screen(self):
        self.main = tk.Frame(self.body, bg=BG)

        # ---------------- left: groups
        side = tk.Frame(self.main, bg=PANEL, width=250)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        top = tk.Frame(side, bg=PANEL)
        top.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(top, text="GROUPS", bg=PANEL, fg=MUTED,
                 font=F(9, True)).pack(side="left")
        flat_btn(top, "+ New", self._on_new_project, size=10,
                 bold=True, pad=4).pack(side="right")

        lb = tk.Frame(side, bg=PANEL)
        lb.pack(fill="both", expand=True, padx=8)
        self.lst_groups = tk.Listbox(
            lb, font=F(11), relief="flat", bd=0, highlightthickness=0,
            activestyle="none", bg=PANEL, fg=TEXT, selectbackground=ACCENT,
            selectforeground="white", exportselection=False)
        self.lst_groups.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(lb, command=self.lst_groups.yview, width=10)
        sb.pack(side="right", fill="y")
        self.lst_groups.config(yscrollcommand=sb.set)
        self.lst_groups.bind("<<ListboxSelect>>", self._on_pick_group)

        row = tk.Frame(side, bg=PANEL)
        row.pack(fill="x", padx=10, pady=8)
        flat_btn(row, "Rename", self._on_rename_project, pad=4).pack(side="left")
        flat_btn(row, "Delete", self._on_delete_project, fg=RED,
                 pad=4).pack(side="left")

        tk.Frame(self.main, bg=LINE, width=1).pack(side="left", fill="y")

        # ---------------- right
        right = tk.Frame(self.main, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        bar = tk.Frame(right, bg=PANEL, height=56)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.lbl_gname = tk.Label(bar, text="", bg=PANEL, fg=TEXT,
                                  font=F(14, True))
        self.lbl_gname.pack(side="left", padx=18)
        self.lbl_gcount = tk.Label(bar, text="", bg=PANEL, fg=MUTED, font=F(10))
        self.lbl_gcount.pack(side="left")
        flat_btn(bar, "Account health", self._on_health).pack(side="right",
                                                             padx=(0, 8))
        flat_btn(bar, "Delivery report", self._on_report).pack(side="right",
                                                              padx=(0, 4))
        flat_btn(bar, "Send again to everyone",
                 self._on_new_campaign).pack(side="right")
        tk.Frame(right, bg=LINE, height=1).pack(fill="x")

        # Everything below scrolls if the screen is too short for it. On a
        # small laptop the settings row used to fall off the bottom with no
        # way to reach it.
        holder = tk.Frame(right, bg=BG)
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        canvas.pack(side="left", fill="both", expand=True, padx=(14, 0),
                    pady=12)
        self.mid_bar = ttk.Scrollbar(holder, orient="vertical",
                                     command=canvas.yview)
        canvas.configure(yscrollcommand=self._mid_scrolled)
        self.mid_canvas = canvas

        mid = tk.Frame(canvas, bg=BG)
        self._mid_window = canvas.create_window((0, 0), window=mid,
                                                anchor="nw")

        def _fit(_=None):
            canvas.itemconfigure(self._mid_window,
                                 width=canvas.winfo_width() - 14)
            canvas.configure(scrollregion=canvas.bbox("all"))

        mid.bind("<Configure>", _fit)
        canvas.bind("<Configure>", _fit)

        def _wheel(e):
            step = -1 if getattr(e, "delta", 0) > 0 or e.num == 4 else 1
            canvas.yview_scroll(step, "units")

        # Windows and Mac send <MouseWheel>; X11 sends buttons 4 and 5.
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(seq, _wheel)

        # -------- people in this group
        pc = tk.Frame(mid, bg=PANEL, highlightbackground=LINE,
                      highlightthickness=1)
        pc.pack(fill="both", expand=True)
        ph = tk.Frame(pc, bg=PANEL)
        ph.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(ph, text="People in this group", bg=PANEL, fg=TEXT,
                 font=F(11, True)).pack(side="left")
        flat_btn(ph, "Remove selected", self._on_remove_selected, fg=RED,
                 pad=6).pack(side="right")
        flat_btn(ph, "From a file…", self._on_load_file,
                 pad=6).pack(side="right")
        flat_btn(ph, "From a Telegram group…", self._on_import_group,
                 pad=6).pack(side="right")
        flat_btn(ph, "Recover people", self._on_recover, pad=6).pack(side="right")

        ml = tk.Frame(pc, bg=PANEL)
        ml.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.lst_members = tk.Listbox(
            ml, font=(MONO, 10), relief="flat", bd=0, highlightthickness=1,
            highlightbackground=LINE, activestyle="none", bg="#fbfbfc", fg=TEXT,
            selectmode="extended", selectbackground=ACCENT,
            selectforeground="white", exportselection=False, height=4)
        self.lst_members.pack(side="left", fill="both", expand=True)
        msb = tk.Scrollbar(ml, command=self.lst_members.yview, width=10)
        msb.pack(side="right", fill="y")
        self.lst_members.config(yscrollcommand=msb.set)

        addrow = tk.Frame(pc, bg=PANEL)
        addrow.pack(fill="x", padx=12, pady=(0, 12))
        self.e_add = tk.Entry(addrow, font=F(11), relief="flat", bg="#f5f6f7",
                              highlightthickness=1, highlightbackground=LINE,
                              highlightcolor=ACCENT)
        self.e_add.pack(side="left", fill="x", expand=True, ipady=6)
        self.e_add.bind("<Return>", lambda e: self._on_add_person())
        tk.Button(addrow, text="Add person", command=self._on_add_person,
                  bg=ACCENT, fg="white", font=F(10, True), relief="flat", bd=0,
                  cursor="hand2", padx=16, pady=6, activebackground=ACCENT_DARK,
                  activeforeground="white").pack(side="left", padx=(8, 0))
        tk.Label(pc, text="type  +353871234567, John   or   @username, John   "
                          "then press Add person (or hit Enter)",
                 bg=PANEL, fg=MUTED, font=F(9)).pack(anchor="w", padx=12,
                                                     pady=(0, 10))

        # -------- composer
        cc = tk.Frame(mid, bg=PANEL, highlightbackground=LINE,
                      highlightthickness=1)
        cc.pack(fill="x", pady=(12, 0))

        # The heading row doubles as the channel picker, so choosing where the
        # message goes costs no extra height on a small laptop screen.
        chan = self.chan_row = tk.Frame(cc, bg=PANEL)
        chan.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(chan, text="Message", bg=PANEL, fg=TEXT,
                 font=F(11, True)).pack(side="left")
        flat_btn(chan, "Email settings…", self._on_email_settings,
                 pad=6).pack(side="right")
        self.lbl_chan = tk.Label(chan, text="", bg=PANEL, fg=MUTED, font=F(9))
        self.lbl_chan.pack(side="right", padx=8)
        self.v_tg = tk.BooleanVar(value=True)
        self.v_email = tk.BooleanVar(value=False)
        tk.Checkbutton(chan, text="Email", variable=self.v_email, bg=PANEL,
                       font=F(10), selectcolor=PANEL, activebackground=PANEL,
                       command=self._on_channel_change).pack(side="right",
                                                             padx=4)
        tk.Checkbutton(chan, text="Telegram", variable=self.v_tg, bg=PANEL,
                       font=F(10), selectcolor=PANEL, activebackground=PANEL,
                       command=self._on_channel_change).pack(side="right",
                                                             padx=4)
        tk.Label(chan, text="Send by", bg=PANEL, fg=TEXT,
                 font=F(10, True)).pack(side="right", padx=(0, 4))

        tk.Label(cc, text="Type it once. Optional: {name} becomes their first "
                          "name.", bg=PANEL, fg=MUTED,
                 font=F(9)).pack(anchor="w", padx=12)
        self.t_msg = tk.Text(cc, height=6, wrap="word", font=F(11),
                             relief="flat", bg="#fbfbfc", highlightthickness=1,
                             highlightbackground=LINE, highlightcolor=ACCENT,
                             undo=True, padx=8, pady=8)
        self.t_msg.pack(fill="x", padx=12, pady=(6, 8))
        self.t_msg.bind("<KeyRelease>", lambda e: self._refresh_msg_label())

        # ---- subject: only an email needs one, so it only shows for email
        self.subj_row = tk.Frame(cc, bg=PANEL)
        tk.Label(self.subj_row, text="Subject", bg=PANEL, fg=TEXT,
                 font=F(10, True)).pack(side="left")
        self.e_subject = tk.Entry(self.subj_row, font=F(11), relief="flat",
                                  bg="#fbfbfc", highlightthickness=1,
                                  highlightbackground=LINE,
                                  highlightcolor=ACCENT)
        self.e_subject.pack(side="left", fill="x", expand=True, padx=(8, 0))
        tk.Label(self.subj_row, text="  (the line they see in their inbox)",
                 bg=PANEL, fg=MUTED, font=F(9)).pack(side="left")

        # ---- how to send: privately to each person, or one for everyone
        mode = self.mode_row = tk.Frame(cc, bg=PANEL)
        mode.pack(fill="x", padx=12, pady=(2, 6))
        self.v_mode = tk.StringVar(value="private")
        tk.Label(mode, text="Send as", bg=PANEL, fg=TEXT,
                 font=F(10, True)).pack(side="left")
        tk.Radiobutton(mode, text="a private message to each person",
                       variable=self.v_mode, value="private", bg=PANEL,
                       font=F(10), selectcolor=PANEL, activebackground=PANEL,
                       command=self._on_mode_change).pack(side="left", padx=(8, 4))
        tk.Radiobutton(mode, text="one message everyone sees",
                       variable=self.v_mode, value="group", bg=PANEL,
                       font=F(10), selectcolor=PANEL, activebackground=PANEL,
                       command=self._on_mode_change).pack(side="left", padx=4)
        self.b_pickgroup = flat_btn(mode, "Choose group…", self._on_pick_post_group,
                                    pad=6)
        self.lbl_mode = tk.Label(mode, text="", bg=PANEL, fg=MUTED, font=F(9))
        self.lbl_mode.pack(side="left", padx=6)

        act = tk.Frame(cc, bg=PANEL)
        act.pack(fill="x", padx=12, pady=(0, 12))
        flat_btn(act, "📎  Attach photo / video / file",
                 self._on_attach, pad=6).pack(side="left")
        self.b_unattach = flat_btn(act, "✕ remove", self._on_unattach, fg=RED,
                                   pad=4)
        self.lbl_attach = tk.Label(act, text="", bg=PANEL, fg=MUTED, font=F(9))
        self.lbl_attach.pack(side="left", padx=6)

        self.b_send = tk.Button(act, text="SEND  ➤", command=self._on_send,
                                bg=GREEN, fg="white", font=F(12, True),
                                relief="flat", bd=0, cursor="hand2", padx=24,
                                pady=7, activebackground=GREEN_DARK,
                                activeforeground="white")
        self.b_send.pack(side="right")
        self.b_stop = tk.Button(act, text="STOP", command=self._on_stop,
                                bg=RED, fg="white", font=F(11, True),
                                relief="flat", bd=0, cursor="hand2", padx=16,
                                pady=7, state="disabled",
                                activeforeground="white")
        self.b_stop.pack(side="right", padx=8)
        self.b_check = flat_btn(act, "Check first (sends nothing)",
                                self._on_check, pad=6)
        self.b_check.pack(side="right", padx=6)
        self.lbl_msg = tk.Label(act, text="", bg=PANEL, fg=MUTED, font=F(9))
        self.lbl_msg.pack(side="right", padx=8)

        # -------- settings, editable right here on the dashboard
        sc = tk.Frame(mid, bg=PANEL, highlightbackground=LINE,
                      highlightthickness=1)
        sc.pack(fill="x", pady=(12, 0))
        sr = tk.Frame(sc, bg=PANEL)
        sr.pack(fill="x", padx=12, pady=10)

        tk.Label(sr, text="Sending hours", bg=PANEL, fg=TEXT,
                 font=F(10, True)).pack(side="left")
        self.e_from = self._small_entry(sr, self.cfg.get("active_hours_start",
                                                         "09:00"))
        tk.Label(sr, text="to", bg=PANEL, fg=MUTED, font=F(10)).pack(side="left")
        self.e_to = self._small_entry(sr, self.cfg.get("active_hours_end",
                                                       "21:30"))

        tk.Label(sr, text="   Speed", bg=PANEL, fg=TEXT,
                 font=F(10, True)).pack(side="left")
        self.speed_labels = {k: v["label"].split("—")[0].strip()
                             for k, v in S.SPEED_PRESETS.items()}
        self.cb_speed = ttk.Combobox(sr, state="readonly", width=9, font=F(10),
                                     values=list(self.speed_labels.values()))
        self.cb_speed.set(self.speed_labels.get(self.cfg.get("speed", "safest"),
                                                "Safest"))
        self.cb_speed.pack(side="left", padx=6)
        self.cb_speed.bind("<<ComboboxSelected>>", lambda e: self._apply_settings())

        tk.Label(sr, text="   Max NEW people per day", bg=PANEL, fg=TEXT,
                 font=F(10, True)).pack(side="left")
        self.e_new = self._small_entry(sr, str(self.cfg.get("daily_cap_new", 15)),
                                       width=5)

        self.v_warm = tk.BooleanVar(value=self.cfg.get("use_warmup", True))
        tk.Checkbutton(sr, variable=self.v_warm, bg=PANEL, font=F(10),
                       selectcolor=PANEL, text="First-week warm-up",
                       activebackground=PANEL,
                       command=self._apply_settings).pack(side="left", padx=(12, 0))

        # second row — everything else, so nothing is hidden from him
        sr2 = tk.Frame(sc, bg=PANEL)
        sr2.pack(fill="x", padx=12, pady=(0, 6))

        self.v_unlim = tk.BooleanVar(value=self.cfg.get("unlimited_known", True))
        tk.Checkbutton(sr2, variable=self.v_unlim, bg=PANEL, font=F(10),
                       selectcolor=PANEL, activebackground=PANEL,
                       text="No limit for people I know",
                       command=self._apply_settings).pack(side="left")

        tk.Label(sr2, text="   if limited, max/day", bg=PANEL, fg=MUTED,
                 font=F(9)).pack(side="left")
        self.e_known = self._small_entry(sr2,
                                         str(self.cfg.get("daily_cap_known", 300)),
                                         width=5)

        tk.Label(sr2, text="   Stop after", bg=PANEL, fg=TEXT,
                 font=F(10, True)).pack(side="left")
        self.e_errs = self._small_entry(
            sr2, str(self.cfg.get("stop_after_consecutive_errors", 5)), width=4)
        tk.Label(sr2, text="errors in a row", bg=PANEL, fg=MUTED,
                 font=F(9)).pack(side="left")

        self.v_spam = tk.BooleanVar(
            value=self.cfg.get("check_spambot_before_run", True))
        tk.Checkbutton(sr2, variable=self.v_spam, bg=PANEL, font=F(10),
                       selectcolor=PANEL, activebackground=PANEL,
                       text="Check account health first",
                       command=self._apply_settings).pack(side="left", padx=(12, 0))

        self.v_typing = tk.BooleanVar(value=self.cfg.get("simulate_typing", True))
        tk.Checkbutton(sr2, variable=self.v_typing, bg=PANEL, font=F(10),
                       selectcolor=PANEL, activebackground=PANEL,
                       text="Show typing",
                       command=self._apply_settings).pack(side="left", padx=(8, 0))

        flat_btn(sr2, "Why is nothing sending?", self._on_diagnose,
                 bold=True, pad=6).pack(side="right")
        flat_btn(sr2, "Reset to safe defaults", self._on_reset_settings,
                 pad=6).pack(side="right")

        self.lbl_settings = tk.Label(sc, text="", bg=PANEL, fg=MUTED, font=F(9),
                                     anchor="w")
        self.lbl_settings.pack(fill="x", padx=12, pady=(0, 10))
        self._settings_hint()

        # -------- status + log
        st = tk.Frame(right, bg=BG)
        st.pack(fill="both", expand=False, padx=14, pady=(0, 10))
        self.pbar = ttk.Progressbar(st, mode="determinate")
        self.pbar.pack(fill="x")
        self.lbl_status = tk.Label(st, text="Ready.", bg=BG, fg=MUTED,
                                   anchor="w", font=F(10))
        self.lbl_status.pack(fill="x", pady=(4, 4))
        self.t_log = scrolledtext.ScrolledText(
            st, height=4, wrap="word", font=(MONO, 9), state="disabled",
            relief="flat", bg="#fbfbfc", highlightthickness=1,
            highlightbackground=LINE)
        self.t_log.pack(fill="both", expand=True)
        for tag, col in (("good", GOOD), ("bad", BAD), ("warn", WARN),
                         ("info", TEXT)):
            self.t_log.tag_config(tag, foreground=col)

    def _small_entry(self, parent, value, width=7):
        e = tk.Entry(parent, width=width, font=F(10), relief="flat",
                     justify="center", bg="#f5f6f7", highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=ACCENT)
        e.insert(0, value)
        e.pack(side="left", padx=5, ipady=3)
        e.bind("<Return>", lambda ev: self._apply_settings())
        e.bind("<FocusOut>", lambda ev: self._apply_settings())
        return e

    def _mid_scrolled(self, first, last):
        """Only show the scroll bar when there is something to scroll to."""
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.mid_bar.pack_forget()
        else:
            self.mid_bar.pack(side="right", fill="y", pady=12)
        self.mid_bar.set(first, last)

    def _settings_hint(self):
        self.lbl_settings.config(
            text="Change any of these whenever you like — it takes effect "
                 "straight away, even in the middle of a send.", fg=MUTED)

    # ------------------------------------------------------- email settings

    def _on_email_settings(self):
        """Everything about email, in one box, in plain words."""
        win = tk.Toplevel(self.root)
        win.title("Email settings")
        win.geometry(f"660x{min(660, self.root.winfo_screenheight() - 120)}")
        win.configure(bg=PANEL)
        win.transient(self.root)

        tk.Label(win, text="Sending email from your own address", bg=PANEL,
                 fg=TEXT, font=F(14, True)).pack(anchor="w", padx=20,
                                                 pady=(18, 2))
        tk.Label(win, text="Your emails go out from your own mailbox, so there "
                           "is nothing to pay and no account to sign up for.",
                 bg=PANEL, fg=MUTED, font=F(10), wraplength=580,
                 justify="left").pack(anchor="w", padx=20, pady=(0, 12))

        # Claim the bottom strip BEFORE anything else, so the buttons can
        # never be squeezed off the edge of a short screen.
        btns = tk.Frame(win, bg=PANEL)
        btns.pack(side="bottom", fill="x", padx=20, pady=(8, 16))
        status = tk.Label(win, text="", bg=PANEL, fg=MUTED, font=F(10),
                          wraplength=580, justify="left")
        status.pack(side="bottom", anchor="w", padx=20)

        form = tk.Frame(win, bg=PANEL)
        form.pack(fill="x", padx=20)
        rows = {}

        labels = {}

        def row(r, label, value, show=None, width=34):
            lb = tk.Label(form, text=label, bg=PANEL, fg=TEXT, font=F(10))
            lb.grid(row=r, column=0, sticky="e", pady=5, padx=(0, 10))
            labels[r] = lb
            e = tk.Entry(form, width=width, font=F(11), relief="flat",
                         bg="#f5f6f7", highlightthickness=1,
                         highlightbackground=LINE, highlightcolor=ACCENT,
                         show=show)
            e.insert(0, str(value))
            e.grid(row=r, column=1, sticky="w", pady=5, ipady=4)
            return e

        rows["addr"] = row(0, "Your email address", self.cfg.get(
            "email_address", ""))
        rows["pwd"] = row(1, "Password", self.cfg.get("email_password", ""),
                          show="•")
        lbl_pwd_label = labels[1]

        help_row = tk.Frame(form, bg=PANEL)
        help_row.grid(row=2, column=1, sticky="w")
        lbl_pw = tk.Label(help_row, bg=PANEL, fg=WARN, font=F(9),
                          wraplength=360, justify="left")
        lbl_pw.pack(side="left")

        def open_app_pw():
            webbrowser.open(M.app_password_url(rows["addr"].get().strip()))
            self.log("Opened the App Password page in your browser. Make a "
                     "password there, copy it, and paste it into the Password "
                     "box.", "info")

        flat_btn(help_row, "?", open_app_pw, pad=6).pack(side="left", padx=4)

        # Microsoft no longer accepts a password at all, so those accounts get
        # a sign-in button here instead of the password row.
        ms_row = tk.Frame(form, bg=PANEL)
        lbl_ms = tk.Label(ms_row, bg=PANEL, fg=TEXT, font=F(10),
                          wraplength=360, justify="left")
        lbl_ms.pack(side="left")

        def _is_microsoft():
            return M._provider_key(rows["addr"].get().strip()) == "microsoft"

        def _pw_hint():
            """Name their own provider — "Gmail, Outlook, Yahoo and iCloud"
            makes a Microsoft user wonder which bit is about them."""
            if _is_microsoft():
                # hide the password row entirely; it cannot work
                rows["pwd"].grid_remove()
                lbl_pwd_label.grid_remove()
                help_row.grid_remove()
                ms_row.grid(row=2, column=1, sticky="w", pady=(2, 6))
                b_ms.pack(side="left", padx=(0, 8))
                signed = bool(self.cfg.get("email_oauth_refresh_token"))
                lbl_ms.config(
                    text=("Signed in. Microsoft does not use a password here."
                          if signed else
                          "Microsoft no longer allows programs to send with a "
                          "password. Sign in once with your Microsoft account "
                          "instead."),
                    fg=GOOD if signed else WARN)
                return
            ms_row.grid_remove()
            rows["pwd"].grid()
            lbl_pwd_label.grid()
            help_row.grid()
            key = M._provider_key(rows["addr"].get().strip())
            if key:
                who = M.APP_PASSWORD_STEPS[key][0]
                lbl_pw.config(text=f"{who} needs an \"App Password\" here, NOT "
                                   f"your normal password. Press ? to make "
                                   f"one.")
            else:
                lbl_pw.config(text="Most providers need an \"App Password\" "
                                   "here rather than your normal one.")

        def sign_in_microsoft():
            addr = rows["addr"].get().strip()
            if not addr:
                status.config(text="Type your email address first.", fg=BAD)
                return
            self._microsoft_signin(addr, status, lbl_ms, rows)

        b_ms = tk.Button(ms_row, text="Sign in with Microsoft", bg=ACCENT,
                         fg="white", font=F(10, True), relief="flat", bd=0,
                         cursor="hand2", padx=14, pady=5,
                         activebackground=ACCENT_DARK,
                         activeforeground="white",
                         command=sign_in_microsoft)

        rows["from"] = row(3, "Your name on the email",
                           self.cfg.get("email_from_name", ""))
        rows["host"] = row(4, "Mail server", self.cfg.get("email_smtp_host",
                                                          ""))
        rows["port"] = row(5, "Port", self.cfg.get("email_smtp_port", 587),
                           width=8)

        lbl_guess = tk.Label(form, text="", bg=PANEL, fg=GOOD, font=F(9))
        lbl_guess.grid(row=6, column=1, sticky="w")

        def autofill(_=None):
            """Fill the server in for them the moment they type the address."""
            g = M.guess_smtp(rows["addr"].get().strip())
            if not g:
                if rows["addr"].get().strip():
                    lbl_guess.config(
                        text="I do not know this provider — ask them for their "
                             "outgoing mail server.", fg=WARN)
                return
            if not rows["host"].get().strip():
                rows["host"].delete(0, "end")
                rows["host"].insert(0, g[0])
                rows["port"].delete(0, "end")
                rows["port"].insert(0, str(g[1]))
            lbl_guess.config(text=f"Filled in for you: {g[0]}", fg=GOOD)

        _old_autofill = autofill

        def autofill(_=None):
            _old_autofill()
            _pw_hint()

        rows["addr"].bind("<FocusOut>", autofill)
        rows["addr"].bind("<Return>", autofill)

        tk.Frame(win, bg=LINE, height=1).pack(fill="x", padx=20, pady=14)

        pace = tk.Frame(win, bg=PANEL)
        pace.pack(fill="x", padx=20)
        tk.Label(pace, text="Gap between emails", bg=PANEL, fg=TEXT,
                 font=F(10)).pack(side="left")
        e_dmin = self._plain_entry(pace, self.cfg.get("email_delay_min", 20))
        tk.Label(pace, text="to", bg=PANEL, fg=MUTED, font=F(10)).pack(
            side="left")
        e_dmax = self._plain_entry(pace, self.cfg.get("email_delay_max", 60))
        tk.Label(pace, text="seconds      Most per day", bg=PANEL, fg=TEXT,
                 font=F(10)).pack(side="left", padx=(4, 0))
        e_cap = self._plain_entry(pace, self.cfg.get("email_daily_cap", 200))

        unsub = tk.Frame(win, bg=PANEL)
        unsub.pack(fill="x", padx=20, pady=(14, 4))
        v_unsub = tk.BooleanVar(value=bool(self.cfg.get("email_unsubscribe",
                                                        True)))
        tk.Checkbutton(unsub, text="Put a line at the bottom letting people "
                                   "opt out  (required in Ireland)",
                       variable=v_unsub, bg=PANEL, font=F(10),
                       selectcolor=PANEL,
                       activebackground=PANEL).pack(anchor="w")
        e_unsub = tk.Entry(win, font=F(10), relief="flat", bg="#f5f6f7",
                           highlightthickness=1, highlightbackground=LINE)
        e_unsub.insert(0, self.cfg.get("email_unsubscribe_text", ""))
        e_unsub.pack(fill="x", padx=20, ipady=4)
        tk.Label(win, text="It also stops people pressing \"spam\", which is "
                           "what gets an email address blocked.",
                 bg=PANEL, fg=MUTED, font=F(9)).pack(anchor="w", padx=20,
                                                     pady=(3, 0))

        def collect():
            try:
                port = int(rows["port"].get().strip() or 587)
                dmin = float(e_dmin.get().strip())
                dmax = float(e_dmax.get().strip())
                cap = int(e_cap.get().strip())
                if dmin < 0 or dmax < dmin or cap < 1:
                    raise ValueError
            except ValueError:
                status.config(text="The port, the gap and the daily most must "
                                   "be numbers, and the second gap cannot be "
                                   "smaller than the first.", fg=BAD)
                return None
            return {
                "email_address": rows["addr"].get().strip(),
                "email_password": rows["pwd"].get(),
                "email_from_name": rows["from"].get().strip(),
                "email_smtp_host": rows["host"].get().strip(),
                "email_smtp_port": port,
                "email_use_tls": port != 465,
                "email_delay_min": dmin,
                "email_delay_max": dmax,
                "email_daily_cap": cap,
                "email_unsubscribe": bool(v_unsub.get()),
                "email_unsubscribe_text": e_unsub.get().strip(),
            }

        def save(quiet=False):
            got = collect()
            if got is None:
                return False
            if got["email_address"] and not got["email_smtp_host"]:
                g = M.guess_smtp(got["email_address"])
                if g:
                    got["email_smtp_host"], got["email_smtp_port"] = g[0], g[1]
                    got["email_use_tls"] = g[2]
            self.cfg.update(got)      # same dict a running send is holding
            try:
                S.save_config(self.cfg)
            except S.SaveError as e:
                status.config(text=f"Could not save: {e}", fg=BAD)
                return False
            if not quiet:
                status.config(text="Saved.", fg=GOOD)
            self._refresh_mode()
            return True

        def test():
            if not save(quiet=True):
                return
            addr = self.cfg.get("email_address", "")
            if not addr:
                status.config(text="Fill in your email address first.", fg=BAD)
                return
            status.config(text="Trying to log in and send you a test email…",
                          fg=MUTED)
            win.update_idletasks()

            def done(res):
                ok, message = res
                status.config(text=message, fg=GOOD if ok else BAD)
                self.log(("Email test: " if ok else "Email test failed: ")
                         + message.splitlines()[0], "good" if ok else "bad")
                if not ok:
                    messagebox.showerror("Email is not working yet", message)

            self.backend.submit(
                M.EmailSender(self.cfg, self.emit).test_connection(addr),
                done, lambda e: status.config(text=str(e), fg=BAD))

        tk.Button(btns, text="Send myself a test email", command=test,
                  bg=ACCENT, fg="white", font=F(11, True), relief="flat",
                  bd=0, cursor="hand2", padx=18, pady=7,
                  activeforeground="white").pack(side="left")
        tk.Button(btns, text="Save and close",
                  command=lambda: save() and win.destroy(), bg=GREEN,
                  fg="white", font=F(11, True), relief="flat", bd=0,
                  cursor="hand2", padx=18, pady=7,
                  activeforeground="white").pack(side="right")
        flat_btn(btns, "Cancel", win.destroy, pad=8).pack(side="right", padx=8)

        autofill()

    def _microsoft_signin(self, addr, status, lbl_ms, rows):
        """Sign in with a real Microsoft account instead of a password.

        Microsoft shows a short code; they type it into their browser once and
        it stays connected afterwards.
        """
        status.config(text="Asking Microsoft for a sign-in code…", fg=MUTED)
        self.root.update_idletasks()

        try:
            started = M.ms_start_signin(self.cfg)
        except M.SignInError as e:
            status.config(text=str(e), fg=BAD)
            messagebox.showerror("Could not start the sign-in", str(e))
            return

        code = started["user_code"]
        url = started.get("verification_uri",
                          "https://microsoft.com/devicelogin")

        win = tk.Toplevel(self.root)
        win.title("Sign in with Microsoft")
        win.configure(bg=PANEL)
        win.geometry("520x360")
        win.transient(self.root)

        tk.Label(win, text="Two steps and you are done", bg=PANEL, fg=TEXT,
                 font=F(14, True)).pack(anchor="w", padx=24, pady=(20, 4))
        tk.Label(win, text="1.  Your browser is opening the Microsoft page.\n"
                           "2.  Type this code there, then sign in as "
                           f"{addr}.",
                 bg=PANEL, fg=MUTED, font=F(10), justify="left").pack(
            anchor="w", padx=24)

        box = tk.Frame(win, bg="#f5f6f7", highlightbackground=LINE,
                       highlightthickness=1)
        box.pack(fill="x", padx=24, pady=14)
        tk.Label(box, text=code, bg="#f5f6f7", fg=ACCENT,
                 font=(MONO, 26, "bold")).pack(pady=14)

        def copy_code():
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            lbl_state.config(text="Code copied. Paste it into the Microsoft "
                                  "page.", fg=GOOD)

        btn_row = tk.Frame(win, bg=PANEL)
        btn_row.pack(fill="x", padx=24)
        flat_btn(btn_row, "Copy the code", copy_code, pad=6).pack(side="left")
        flat_btn(btn_row, "Open the page again",
                 lambda: webbrowser.open(url), pad=6).pack(side="left", padx=8)

        lbl_state = tk.Label(win, text="Waiting for you to sign in…", bg=PANEL,
                             fg=MUTED, font=F(10), wraplength=460,
                             justify="left")
        lbl_state.pack(anchor="w", padx=24, pady=(14, 0))

        cancelled = threading.Event()
        flat_btn(win, "Cancel", lambda: (cancelled.set(), win.destroy()),
                 pad=8).pack(side="bottom", pady=14)

        webbrowser.open(url)
        self.log(f"Microsoft sign-in code: {code}  (page: {url})", "info")

        def done(refresh):
            try:
                win.destroy()
            except Exception:
                pass
            if not refresh:
                return
            self.cfg["email_auth"] = "microsoft"
            self.cfg["email_oauth_refresh_token"] = refresh
            self.cfg["email_address"] = addr
            self.cfg["email_password"] = ""
            g = M.guess_smtp(addr)
            if g:
                self.cfg["email_smtp_host"] = g[0]
                self.cfg["email_smtp_port"] = g[1]
                self.cfg["email_use_tls"] = g[2]
                rows["host"].delete(0, "end")
                rows["host"].insert(0, g[0])
                rows["port"].delete(0, "end")
                rows["port"].insert(0, str(g[1]))
            try:
                S.save_config(self.cfg)
            except S.SaveError as e:
                status.config(text=f"Signed in, but could not save: {e}",
                              fg=BAD)
                return
            lbl_ms.config(text="Signed in. Microsoft does not use a password "
                               "here.", fg=GOOD)
            status.config(text="Signed in with Microsoft. Now press \"Send "
                               "myself a test email\".", fg=GOOD)
            self.log(f"Signed in to Microsoft as {addr}.", "good")
            messagebox.showinfo(
                "Signed in",
                "You are connected to Microsoft.\n\nThere is no password to "
                "remember and you will not have to do this again.\n\nNow press "
                "\"Send myself a test email\" to make sure it works.")

        def failed(e):
            try:
                win.destroy()
            except Exception:
                pass
            if cancelled.is_set():
                return
            status.config(text=str(e), fg=BAD)
            messagebox.showerror("Sign-in did not finish", str(e))

        def tick(left):
            self.emit("_call", fn=lambda s: lbl_state.config(
                text=f"Waiting for you to sign in…  ({s // 60}m "
                     f"{s % 60:02d}s left)"), arg=left)

        async def go():
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda: M.ms_finish_signin(started, self.cfg, cancelled,
                                                 tick))

        self.backend.submit(go(), done, failed)

    def _plain_entry(self, parent, value, width=6):
        e = tk.Entry(parent, width=width, font=F(10), relief="flat",
                     justify="center", bg="#f5f6f7", highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=ACCENT)
        e.insert(0, str(value))
        e.pack(side="left", padx=5, ipady=3)
        return e

    def _apply_settings(self, _=None):
        """Validate and apply the dashboard settings immediately."""
        start, end = self.e_from.get().strip(), self.e_to.get().strip()
        for e, v in ((self.e_from, start), (self.e_to, end)):
            try:
                S.parse_hhmm(v)
            except Exception:
                self.lbl_settings.config(
                    text=f"\"{v}\" is not a time. Write it like 05:00 or 21:30.",
                    fg=BAD)
                e.delete(0, "end")
                e.insert(0, self.cfg["active_hours_start"] if e is self.e_from
                         else self.cfg["active_hours_end"])
                return
        try:
            known_cap = int(self.e_known.get().strip())
            errs = int(self.e_errs.get().strip())
            if known_cap < 1 or errs < 1:
                raise ValueError
        except ValueError:
            self.lbl_settings.config(
                text="Max/day and \"stop after errors\" must be whole numbers "
                     "of 1 or more.", fg=BAD)
            self.e_known.delete(0, "end")
            self.e_known.insert(0, str(self.cfg.get("daily_cap_known", 300)))
            self.e_errs.delete(0, "end")
            self.e_errs.insert(0, str(self.cfg.get("stop_after_consecutive_errors", 5)))
            return
        try:
            new_cap = int(self.e_new.get().strip())
            if new_cap < 0:
                raise ValueError
        except ValueError:
            self.lbl_settings.config(
                text="Max new people per day must be a whole number.", fg=BAD)
            self.e_new.delete(0, "end")
            self.e_new.insert(0, str(self.cfg.get("daily_cap_new", 15)))
            return

        picked = self.cb_speed.get()
        speed = next((k for k, lbl in self.speed_labels.items()
                      if lbl == picked), self.cfg.get("speed", "safest"))

        changes = []
        if start != self.cfg["active_hours_start"] or \
                end != self.cfg["active_hours_end"]:
            changes.append(f"hours {start}–{end}")
        if speed != self.cfg.get("speed"):
            if speed == "fast" and not messagebox.askyesno(
                    "Fast is risky",
                    "Fast sends every 8–30 seconds — the pattern Telegram's "
                    "spam system looks for.\n\nUse it anyway?"):
                self.cb_speed.set(self.speed_labels.get(self.cfg.get("speed"),
                                                        "Safest"))
                return
            changes.append(f"speed {speed}")
        if new_cap != self.cfg.get("daily_cap_new"):
            if new_cap > 40 and not messagebox.askyesno(
                    "That is high",
                    f"{new_cap} new people a day is above what accounts survive "
                    f"long term.\n\nSet it anyway?"):
                self.e_new.delete(0, "end")
                self.e_new.insert(0, str(self.cfg.get("daily_cap_new", 15)))
                return
            changes.append(f"max new/day {new_cap}")
        if bool(self.v_warm.get()) != bool(self.cfg.get("use_warmup", True)):
            changes.append("warm-up " + ("on" if self.v_warm.get() else "OFF"))
        for var, key, label in (
                (self.v_unlim, "unlimited_known", "no limit for known people"),
                (self.v_spam, "check_spambot_before_run", "health check"),
                (self.v_typing, "simulate_typing", "typing indicator")):
            if bool(var.get()) != bool(self.cfg.get(key, True)):
                changes.append(f"{label} " + ("on" if var.get() else "OFF"))
        if known_cap != self.cfg.get("daily_cap_known"):
            changes.append(f"known max/day {known_cap}")
        if errs != self.cfg.get("stop_after_consecutive_errors"):
            changes.append(f"stop after {errs} errors")
        if not changes:
            self._settings_hint()
            return

        # mutate the same dict the running sender holds, so it picks this up
        self.cfg["active_hours_start"] = start
        self.cfg["active_hours_end"] = end
        self.cfg["daily_cap_new"] = new_cap
        self.cfg["use_warmup"] = bool(self.v_warm.get())
        self.cfg["unlimited_known"] = bool(self.v_unlim.get())
        self.cfg["daily_cap_known"] = known_cap
        self.cfg["stop_after_consecutive_errors"] = errs
        self.cfg["check_spambot_before_run"] = bool(self.v_spam.get())
        self.cfg["simulate_typing"] = bool(self.v_typing.get())
        S.apply_speed(self.cfg, speed)
        try:
            S.save_config(self.cfg)
        except S.SaveError as e:
            self.lbl_settings.config(text=f"COULD NOT SAVE: {e}", fg=BAD)
            return
        self.lbl_settings.config(text="Saved: " + ", ".join(changes)
                                 + ".  Applies immediately, even mid-send.",
                                 fg=GOOD)
        self.log("Settings changed — " + ", ".join(changes) + ".", "good")
        if self.running:
            self.log("The send that is running will pick this up within a few "
                     "seconds.", "info")
        self.root.after(6000, self._settings_hint)

    def _on_reset_settings(self):
        if not messagebox.askyesno(
                "Reset settings",
                "Put the speed, limits and sending hours back to the safe "
                "defaults?\n\nYour groups, people and messages are not "
                "touched."):
            return
        for k in ("speed", "active_hours_start", "active_hours_end",
                  "unlimited_known", "daily_cap_known", "daily_cap_new",
                  "stop_after_consecutive_errors", "check_spambot_before_run",
                  "simulate_typing", "use_warmup"):
            self.cfg[k] = S.DEFAULT_CONFIG[k]
        S.apply_speed(self.cfg, S.DEFAULT_CONFIG["speed"])
        try:
            S.save_config(self.cfg)
        except S.SaveError as e:
            self.log(f"Could not save: {e}", "bad")
            return
        for e, v in ((self.e_from, self.cfg["active_hours_start"]),
                     (self.e_to, self.cfg["active_hours_end"]),
                     (self.e_new, self.cfg["daily_cap_new"]),
                     (self.e_known, self.cfg["daily_cap_known"]),
                     (self.e_errs, self.cfg["stop_after_consecutive_errors"])):
            e.delete(0, "end")
            e.insert(0, str(v))
        self.cb_speed.set(self.speed_labels[self.cfg["speed"]])
        self.v_warm.set(self.cfg["use_warmup"])
        self.v_unlim.set(self.cfg["unlimited_known"])
        self.v_spam.set(self.cfg["check_spambot_before_run"])
        self.v_typing.set(self.cfg["simulate_typing"])
        self.lbl_settings.config(text="Back to the safe defaults.", fg=GOOD)
        self.log("Settings reset to the safe defaults.", "good")

    def _on_diagnose(self):
        """Plain-English answer to 'why has nothing gone out?'"""
        issues, fine = [], []

        want_tg, want_em = bool(self.v_tg.get()), bool(self.v_email.get())
        tg_people, em_people = S.split_channels(self.members)
        group_mode = self.v_mode.get() == "group"

        # ---- which channels
        if not (want_tg or want_em):
            issues.append("Neither Telegram nor Email is ticked next to "
                          "\"Send by\", so there is nowhere for it to go.")
        if want_tg and not self.tg_ready:
            issues.append("Telegram is ticked but your Telegram account is not "
                          "connected. Press \"Connect Telegram\" at the top "
                          "right.")
        if want_tg and group_mode:
            if self.post_group:
                fine.append(f"Set to post once into "
                            f"\"{self.post_group['title']}\" — not private "
                            f"messages.")
            else:
                issues.append("You picked \"one message everyone sees\" but "
                              "have not chosen which Telegram group. Click "
                              "\"Choose group…\".")

        # ---- email setup
        if want_em:
            if not (self.cfg.get("email_address") or "").strip():
                issues.append("Email is ticked but your email address is not "
                              "filled in. Press \"Email settings…\".")
            elif not (self.cfg.get("email_password") or ""):
                issues.append("Email is ticked but the password is not filled "
                              "in. Press \"Email settings…\". Gmail, Outlook, "
                              "Yahoo and iCloud need an App Password, not your "
                              "normal one.")
            else:
                fine.append(f"Emails will come from "
                            f"{self.cfg['email_address']}.")
            if not self.e_subject.get().strip():
                issues.append("Emails need a subject line. Type one in the "
                              "Subject box.")
            if not em_people:
                issues.append(f"Email is ticked but nobody in "
                              f"\"{self.current}\" has an email address. Add "
                              f"them as:  john@example.com, John")
            else:
                fine.append(f"{len(em_people)} people have an email address.")

        if want_tg and not tg_people and not group_mode:
            issues.append(f"Telegram is ticked but nobody in "
                          f"\"{self.current}\" has a phone number or username. "
                          f"Add them as:  +353871234567, John")
        elif want_tg and tg_people:
            fine.append(f"{len(tg_people)} people can be reached on Telegram.")

        if not S.parse_message_variants(self.t_msg.get("1.0", "end")):
            issues.append("No message typed yet.")
        if not group_mode and not self.members:
            issues.append(f"There is nobody in \"{self.current}\". Add people "
                          f"with the box under the list.")
        if self.attachment and not os.path.exists(self.attachment):
            issues.append(f"The attached file is missing: "
                          f"{os.path.basename(self.attachment)}")
        if self.running:
            fine.append("A send is running right now — that is why SEND is "
                        "greyed out.")

        # clock
        try:
            from datetime import datetime as _dt
            start = S.parse_hhmm(self.cfg["active_hours_start"])
            end = S.parse_hhmm(self.cfg["active_hours_end"])
            now = _dt.now().time()
            overnight = start > end
            inside = (start <= now <= end) if not overnight else \
                     (now >= start or now <= end)
            if inside:
                fine.append(f"The clock is fine — inside your sending hours "
                            f"({self.cfg['active_hours_start']}–"
                            f"{self.cfg['active_hours_end']}).")
            else:
                issues.append(f"It is {now:%H:%M}, outside your sending hours "
                              f"({self.cfg['active_hours_start']}–"
                              f"{self.cfg['active_hours_end']}). It will start "
                              f"at {self.cfg['active_hours_start']}. Change the "
                              f"hours above if you want it sooner.")
        except Exception:
            pass

        if self.save_failed:
            issues.append("The last save did not reach the disk. Your changes "
                          "may not be kept.")

        k, n = S.sent_today(self.state)
        e_today = M.email_sent_today(self.state)
        ceil = S.warmup_ceiling(self.state, self.cfg)
        fine.append(f"Sent today: {k} Telegram to people you know, {n} Telegram "
                    f"to new people, {e_today} emails.")

        if want_tg:
            if ceil is not None and (k + n) >= ceil:
                issues.append(f"Today's first-week Telegram limit of {ceil} "
                              f"messages is used up. It rises tomorrow, or "
                              f"untick \"First-week warm-up\" above. Email is "
                              f"not affected by this.")
            if n >= self.cfg.get("daily_cap_new", 15):
                issues.append(f"Today's Telegram limit for NEW people "
                              f"({self.cfg['daily_cap_new']}) is used up. "
                              f"People already in your contacts, and email, "
                              f"are not affected.")
        if want_em:
            cap = self.cfg.get("email_daily_cap", 200)
            if e_today >= cap:
                issues.append(f"Today's email limit ({cap}) is used up. It "
                              f"starts again at midnight and nothing is lost. "
                              f"You can raise it in \"Email settings…\".")

        # Who is left, counted per channel — a person can be in this group
        # twice, once for Telegram and once for email.
        ps = S.project_state(self.state, self.current)
        if want_tg and tg_people and all(t in ps["sent"] for t, _ in tg_people):
            issues.append(f"All {len(tg_people)} Telegram people in "
                          f"\"{self.current}\" already received a message. "
                          f"Press SEND and say yes when it offers to send the "
                          f"new message to them again.")
        if want_em and em_people and all(t in ps["sent"] for t, _ in em_people):
            issues.append(f"All {len(em_people)} email people in "
                          f"\"{self.current}\" already received a message. "
                          f"Press SEND and say yes when it offers to send the "
                          f"new message to them again.")

        self._show_diagnosis(issues, fine)

    def _show_diagnosis(self, issues, fine):
        win = tk.Toplevel(self.root)
        win.title("Why is nothing sending?")
        win.configure(bg=PANEL)
        win.geometry("640x460")
        win.transient(self.root)
        if issues:
            tk.Label(win, text="Here is what is stopping it", bg=PANEL, fg=BAD,
                     font=F(14, True)).pack(anchor="w", padx=18, pady=(18, 6))
        else:
            tk.Label(win, text="Nothing is wrong — it is ready to send",
                     bg=PANEL, fg=GOOD,
                     font=F(14, True)).pack(anchor="w", padx=18, pady=(18, 6))
        box = tk.Text(win, wrap="word", font=F(11), relief="flat",
                      bg="#fbfbfc", highlightthickness=1,
                      highlightbackground=LINE, padx=12, pady=12)
        box.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        for i, m in enumerate(issues, 1):
            box.insert("end", f"{i}.  {m}\n\n", "bad")
        if fine:
            box.insert("end", "\nEverything else:\n\n", "head")
            for m in fine:
                box.insert("end", f"•  {m}\n", "ok")
        box.tag_config("bad", foreground=BAD)
        box.tag_config("ok", foreground=MUTED)
        box.tag_config("head", foreground=TEXT, font=F(11, True))
        box.config(state="disabled")
        tk.Button(win, text="Close", command=win.destroy, relief="flat", bd=0,
                  cursor="hand2", font=F(11), padx=20, pady=7).pack(pady=(0, 14))

    def show_main(self):
        self.setup.pack_forget()
        self.main.pack(fill="both", expand=True)
        self._reload_groups()
        self._load_current_project()

        if self.tg_ready:
            self.b_connect_tg.pack_forget()
            k, n = S.sent_today(self.state)
            ceil = S.warmup_ceiling(self.state, self.cfg)
            self.log(f"Connected. Today so far: {k} to people you know, {n} to "
                     f"new people.", "info")
            if ceil is not None:
                self.log(f"First-week warm-up: up to {ceil} messages today. It "
                         f"rises daily, then there is no limit for your own "
                         f"contacts.", "info")
            else:
                self.log(f"No daily limit for people you already know. New "
                         f"people are capped at "
                         f"{self.cfg['daily_cap_new']} per day.", "info")
        else:
            self.b_connect_tg.pack(side="right", padx=10, pady=10)
            self.who.config(text="Telegram not connected")

        e = M.email_sent_today(self.state)
        if self.cfg.get("email_address"):
            self.log(f"Email: {e} sent today, out of "
                     f"{self.cfg.get('email_daily_cap', 200)}.", "info")

    # ============================================================== groups

    def _reload_groups(self):
        names = S.project_names(self.projects)
        if self.current not in names:
            self.current = names[0]
        self.lst_groups.delete(0, "end")
        for nm in names:
            # the group on screen may have unsaved edits, so count live members
            n = (len(self.members) if nm == self.current else
                 len(S.parse_recipients(
                     S.get_project(self.projects, nm).get("recipients", ""))))
            self.lst_groups.insert("end", f"  {nm}   ({n})")
        self.lst_groups.selection_clear(0, "end")
        self.lst_groups.selection_set(names.index(self.current))

    def _on_pick_group(self, _=None):
        sel = self.lst_groups.curselection()
        if not sel:
            return
        name = S.project_names(self.projects)[sel[0]]
        if name == self.current:
            return
        if self.running:
            messagebox.showinfo("Still sending",
                                "Please press STOP before switching group.")
            self._reload_groups()
            return
        self._save_current_project()
        self.current = name
        self.projects["current"] = name
        S.save_projects(self.projects)
        self._load_current_project()
        ps = S.project_state(self.state, name)
        self.log(f"Group \"{name}\" — {len(ps['sent'])} people already "
                 f"messaged in it.", "info")

    def _load_current_project(self):
        p = S.get_project(self.projects, self.current)
        self.t_msg.delete("1.0", "end")
        self.t_msg.insert("1.0", p.get("message", ""))
        self.members = S.parse_recipients(p.get("recipients", ""))
        self.attachment = p.get("attachment", "") or ""
        self.post_group = p.get("post_group") or None
        self.v_mode.set(p.get("mode", "private"))
        self.e_subject.delete(0, "end")
        self.e_subject.insert(0, p.get("subject", "") or "")

        # A group that has never had its channels chosen picks the sensible
        # one from who is actually in it.
        ch = p.get("channels")
        if isinstance(ch, dict):
            self.v_tg.set(bool(ch.get("telegram", True)))
            self.v_email.set(bool(ch.get("email", False)))
            if not (self.v_tg.get() or self.v_email.get()):
                self.v_tg.set(True)
        else:
            tg_people, em_people = S.split_channels(self.members)
            self.v_tg.set(bool(tg_people) or not em_people)
            self.v_email.set(bool(em_people))
        self._refresh_members()
        self._refresh_mode()
        self._refresh_attach()
        self._refresh_msg_label()
        self.lbl_gname.config(text=self.current)

    def _recipients_text(self):
        return "\n".join(f"{t}, {n}" if n else t for t, n in self.members) + "\n"

    def _save_current_project(self, quiet=False):
        """Save, and SHOUT if it did not reach the disk. A silent failed save is
        exactly how people vanished before."""
        try:
            S.set_project(self.projects, self.current,
                          self.t_msg.get("1.0", "end").rstrip() + "\n",
                          self._recipients_text(), self.attachment,
                          self.v_mode.get(), self.post_group,
                          self.e_subject.get().strip(),
                          {"telegram": bool(self.v_tg.get()),
                           "email": bool(self.v_email.get())})
            self.save_failed = False
            return True
        except S.SaveError as e:
            self.save_failed = True
            self.log(f"COULD NOT SAVE! {e}", "bad")
            self.log("Your people are still on screen but are NOT written to "
                     "disk. Close OneDrive sync or move this folder out of "
                     "OneDrive, then press Save again.", "bad")
            if not quiet:
                messagebox.showerror(
                    "COULD NOT SAVE",
                    "Windows would not let the file be written, so your changes "
                    "are NOT saved yet.\n\n"
                    f"{e}\n\n"
                    "Usual cause: this folder is inside OneDrive, or antivirus "
                    "is holding the file.\n\n"
                    "Nothing is lost — everything you added is recorded "
                    "separately and can be restored with \"Recover people\".")
            return False
        except Exception as e:
            self.save_failed = True
            self.log(f"COULD NOT SAVE! {type(e).__name__}: {e}", "bad")
            if not quiet:
                messagebox.showerror("COULD NOT SAVE", f"{type(e).__name__}: {e}")
            return False

    def _refresh_members(self):
        self.lst_members.delete(0, "end")
        for t, n in self.members:
            # a glyph so he can see at a glance who is on which channel
            tag = "@" if S.is_email(t) else "T"
            self.lst_members.insert("end", f" {tag}  {t:<30} {n or ''}")
        tg_people, em_people = S.split_channels(self.members)
        bits = f"{len(self.members)} people"
        if tg_people and em_people:
            bits += f"  ({len(tg_people)} Telegram, {len(em_people)} email)"
        self.lbl_gcount.config(text=bits)
        self._reload_groups()
        if hasattr(self, "lbl_mode"):
            self._refresh_mode()

    def _on_new_project(self):
        name = simpledialog.askstring(
            "New group", "Name for this group?\n(e.g. Daily rates, VIP "
            "customers)", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.projects["projects"]:
            messagebox.showwarning("Already exists",
                                   f"There is already a group called \"{name}\".")
            return
        self._save_current_project()
        self.current = name
        S.set_project(self.projects, name, "", "")
        self._reload_groups()
        self._load_current_project()
        self.log(f"Created group \"{name}\". Add people to it below.", "good")

    def _on_rename_project(self):
        new = simpledialog.askstring("Rename group", "New name:",
                                     initialvalue=self.current,
                                     parent=self.root)
        if not new or not new.strip() or new.strip() == self.current:
            return
        new = new.strip()
        if new in self.projects["projects"]:
            messagebox.showwarning("Already exists", "That name is taken.")
            return
        self._save_current_project()
        self.projects["projects"][new] = \
            self.projects["projects"].pop(self.current)
        if self.current in self.state["projects"]:
            self.state["projects"][new] = \
                self.state["projects"].pop(self.current)
            S.save_state(self.state)
        self.current = new
        self.projects["current"] = new
        S.save_projects(self.projects)
        self._reload_groups()
        self._load_current_project()
        self.log(f"Renamed to \"{new}\".", "good")

    def _on_delete_project(self):
        if len(self.projects["projects"]) == 1:
            messagebox.showinfo("Cannot delete", "This is your only group. "
                                                 "Create another one first.")
            return
        if not messagebox.askyesno(
                "Delete group",
                f"Delete \"{self.current}\" and everyone in it?\n\nThis cannot "
                f"be undone."):
            return
        gone = self.current
        self.projects = S.delete_project(self.projects, gone)
        self.state["projects"].pop(gone, None)
        S.save_state(self.state)
        self.current = self.projects["current"]
        self._reload_groups()
        self._load_current_project()
        self.log(f"Deleted \"{gone}\".", "warn")

    # ============================================================== people

    @staticmethod
    def _parse_add_box(raw):
        """What was typed -> [(target, name)].

        Handles the everyday two-field form, and also the three-field form
        "+353871234567, John, john@x.com" which makes BOTH a Telegram entry
        and an email entry for the one person. parse_recipients only ever
        splits on the first comma, so the three-field case has to be pulled
        apart here before it gets there.
        """
        out = []
        for line in raw.replace(";", "\n").split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            bits = [b.strip() for b in line.split(",") if b.strip()]
            emails = [b for b in bits if S.is_email(b)]
            others = [b for b in bits if not S.is_email(b)]
            handles = [b for b in others
                       if b.startswith(("@", "+")) or b.replace(" ", "").isdigit()]
            names = [b for b in others if b not in handles]

            if emails:
                # Whichever way round they typed it, the address is the
                # address and anything else is the name. "John, john@x.com"
                # used to create a person called "john@x.com" whose Telegram
                # handle was "John".
                name = (names[0] if names else
                        handles[0] if len(handles) > 1 else None)
                for h in handles:
                    out.append((h, name))
                for e in emails:
                    out.append((e, name))
                continue
            out.extend(S.parse_recipients(line))
        return out

    def _on_add_person(self):
        raw = self.e_add.get().strip()
        if not raw:
            messagebox.showinfo(
                "Nothing typed",
                "Type one of these, then press Add:\n\n"
                "  +353871234567, John\n"
                "  @john_murphy, John\n"
                "  john@example.com, John\n\n"
                "Or all in one go, and it makes both:\n\n"
                "  +353871234567, John, john@example.com")
            return
        have = {t.lower().lstrip("@") for t, _ in self.members}
        added = 0
        for t, n in self._parse_add_box(raw):
            if t.lower().lstrip("@") in have:
                continue
            self.members.append((t, n))
            have.add(t.lower().lstrip("@"))
            added += 1
        self.e_add.delete(0, "end")
        if added:
            S.journal("add", self.current, self.members[-added:])
        self._refresh_members()
        self._save_current_project()
        if added:
            self.lst_members.see("end")
            self.log(f"Added {added} to \"{self.current}\".", "good")
        else:
            self.log("Already in this group — nothing added.", "warn")

    def _on_remove_selected(self):
        sel = list(self.lst_members.curselection())
        if not sel:
            messagebox.showinfo("Nobody selected",
                                "Click the people you want to remove first.\n\n"
                                "Hold Ctrl to pick several, or Shift for a "
                                "range.")
            return
        names = [self.members[i][0] for i in sel]
        if len(sel) > 1 and not messagebox.askyesno(
                "Remove these people?",
                f"Remove {len(sel)} people from \"{self.current}\"?"):
            return
        S.journal("remove", self.current, [self.members[i] for i in sel])
        for i in sorted(sel, reverse=True):
            del self.members[i]
        self._refresh_members()
        self._save_current_project()
        self.log(f"Removed {len(sel)} from \"{self.current}\" "
                 f"({', '.join(names[:3])}{'…' if len(names) > 3 else ''}).",
                 "warn")

    @staticmethod
    def _rows_from_file(text):
        """Read a list out of a file, whichever way round the columns are.

        A contacts export from Outlook or Google is "Name,Email", which the
        plain reader would turn into a person called "Email". Anything with a
        header row, or with the name first, is flipped here.
        """
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out = []
        for line in lines:
            line = line.strip().strip(",")
            if not line or line.startswith("#"):
                continue
            bits = [b.strip().strip('"') for b in line.split(",")]
            low = [b.lower() for b in bits]
            if low and low[0] in ("name", "first name", "firstname",
                                  "email", "e-mail", "email address",
                                  "phone", "number", "username"):
                continue                                   # a header row
            if len(bits) >= 2 and not S.is_email(bits[0]) \
                    and not bits[0].startswith(("@", "+")) \
                    and not bits[0].replace(" ", "").isdigit() \
                    and S.is_email(bits[1]):
                out.append((bits[1], bits[0] or None))     # Name,Email
                continue
            out.extend(S.parse_recipients(line))
        return out

    def _on_load_file(self):
        path = filedialog.askopenfilename(
            title="Choose a file with emails, usernames or phone numbers",
            filetypes=[("Text or CSV", "*.txt *.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            messagebox.showerror("Could not open that file", str(e))
            return
        have = {t.lower().lstrip("@") for t, _ in self.members}
        added = 0
        for t, n in self._rows_from_file(text):
            if t.lower().lstrip("@") in have:
                continue
            self.members.append((t, n))
            have.add(t.lower().lstrip("@"))
            added += 1
        if added:
            S.journal("add", self.current, self.members[-added:])
        self._refresh_members()
        self._save_current_project()
        self.log(f"Added {added} people from {os.path.basename(path)}.", "good")

    # ---------------------------------------------- private vs group posting

    def _on_channel_change(self):
        """Telegram and/or Email ticked."""
        if not (self.v_tg.get() or self.v_email.get()):
            # Untick both and there is nowhere to send. Put the last one back
            # rather than letting them sit in an impossible state.
            self.v_tg.set(True)
            self.log("Pick at least one — Telegram or Email.", "warn")
        self._refresh_mode()
        self._save_current_project()
        if self.v_email.get() and not self._email_configured():
            self.log("Email is ticked but not set up yet. Press \"Email "
                     "settings…\" and fill in your address and password.",
                     "warn")

    def _email_configured(self):
        return bool((self.cfg.get("email_address") or "").strip()
                    and (self.cfg.get("email_password") or ""))

    def _on_mode_change(self):
        self._refresh_mode()
        self._save_current_project()
        if self.v_mode.get() == "group":
            bits = []
            if self.v_tg.get():
                bits.append("one post in a Telegram group, where everyone "
                            "sees it and sees the replies")
            if self.v_email.get():
                bits.append("one email to everybody, with the addresses "
                            "hidden from each other")
            self.log("Mode: " + " and ".join(bits) + ".", "warn")
            self.log("{name} cannot be used this way — everyone gets the very "
                     "same words.", "warn")
            if self.v_tg.get() and not self.post_group:
                self._on_pick_post_group()
        else:
            self.log("Mode: a separate private message to each person. Nobody "
                     "sees anyone else.", "good")

    def _refresh_mode(self):
        group = self.v_mode.get() == "group"
        tg_people, em_people = S.split_channels(self.members)

        # the "choose a Telegram group" button only matters for Telegram
        if group and self.v_tg.get():
            self.b_pickgroup.pack(side="left", padx=4)
            self.lbl_mode.config(
                text=(f"→ posts once into \"{self.post_group['title']}\""
                      if self.post_group else "→ pick which group to post in"),
                fg=GOOD if self.post_group else BAD)
        else:
            self.b_pickgroup.pack_forget()
            if group:
                self.lbl_mode.config(text=f"→ one email to {len(em_people)} "
                                          f"people at once", fg=MUTED)
            else:
                bits = []
                if self.v_tg.get():
                    bits.append(f"{len(tg_people)} on Telegram")
                if self.v_email.get():
                    bits.append(f"{len(em_people)} by email")
                self.lbl_mode.config(text="→ " + " · ".join(bits) if bits
                                     else "", fg=MUTED)

        # the subject box only appears when it is actually needed
        if self.v_email.get():
            self.subj_row.pack(fill="x", padx=12, pady=(2, 4),
                               before=self.mode_row)
        else:
            self.subj_row.pack_forget()

        note = []
        if self.v_tg.get():
            note.append(f"{len(tg_people)} with Telegram")
        if self.v_email.get():
            note.append(f"{len(em_people)} with an email address")
        self.lbl_chan.config(
            text=("  " + " · ".join(note)) if note else "",
            fg=BAD if (self.v_email.get() and not em_people) or
                      (self.v_tg.get() and not tg_people and not group)
            else MUTED)

    def _on_pick_post_group(self):
        if not self._need_telegram():
            return
        self.log("Reading your Telegram groups…", "info")

        def done(groups):
            if not groups:
                messagebox.showinfo("No groups",
                                    "This account is not in any groups.")
                return
            win = tk.Toplevel(self.root)
            win.title("Which group to post in")
            win.configure(bg=PANEL)
            win.geometry("520x440")
            win.transient(self.root)
            tk.Label(win, text="Post the message into which Telegram group?",
                     bg=PANEL, fg=TEXT, font=F(12, True)).pack(anchor="w",
                                                               padx=16,
                                                               pady=(16, 2))
            tk.Label(win, text="One message goes into the group chat. Everyone "
                               "in it sees the message and everyone sees the "
                               "replies — the opposite of private sending.",
                     bg=PANEL, fg=WARN, font=F(9), wraplength=470,
                     justify="left").pack(anchor="w", padx=16, pady=(0, 10))
            lb = tk.Listbox(win, font=F(11), relief="flat", highlightthickness=1,
                            highlightbackground=LINE, activestyle="none",
                            selectbackground=ACCENT, selectforeground="white")
            lb.pack(fill="both", expand=True, padx=16)
            for g in groups:
                lb.insert("end", "  " + g["title"])

            def choose():
                sel = lb.curselection()
                if not sel:
                    messagebox.showinfo("Pick a group",
                                        "Click a group in the list first.")
                    return
                self.post_group = dict(groups[sel[0]])
                win.destroy()
                self._refresh_mode()
                self._save_current_project()
                self.log(f"Will post into \"{self.post_group['title']}\".",
                         "good")

            tk.Button(win, text="Use this group", command=choose, bg=GREEN,
                      fg="white", font=F(11, True), relief="flat", bd=0,
                      cursor="hand2", padx=20, pady=8,
                      activebackground=GREEN_DARK,
                      activeforeground="white").pack(pady=14)

        self.backend.submit(self.backend.sender.list_groups(), done,
                            lambda e: self.log(f"Could not read groups: {e}",
                                               "bad"))

    def _send_broadcast(self, variants, em_people):
        """One message everybody sees: posted in a Telegram group, and/or one
        email to everyone with the addresses hidden from each other."""
        want_tg = bool(self.v_tg.get())
        want_em = bool(self.v_email.get()) and bool(em_people)

        if want_tg and not self.post_group:
            messagebox.showwarning("No group chosen",
                                   "Click \"Choose group…\" and pick which "
                                   "Telegram group to post in.")
            return
        if self.v_email.get() and not em_people:
            want_em = False
            self.log("Nobody in this group has an email address.", "warn")
        if not (want_tg or want_em):
            messagebox.showinfo("Nothing to send",
                                "There is nowhere for this to go.")
            return

        text = S.render(variants, "")
        where = []
        if want_tg:
            where.append(f"posted once in \"{self.post_group['title']}\", "
                         f"where everyone in that group sees it")
        if want_em:
            where.append(f"emailed to {len(em_people)} people at once, with "
                         f"their addresses hidden from each other")
        warn = ("\n\nNote: {name} cannot be used this way — everyone gets the "
                "exact same words." if "{name}" in text else "")
        if not messagebox.askyesno(
                "Send one message to everyone?",
                "Your message will be:\n\n  · " + "\n  · ".join(where) + warn
                + "\n\nThis is not the private one-to-one sending."):
            return

        self.b_send.config(state="disabled")
        self.b_check.config(state="disabled")
        self.lbl_status.config(text="Sending…")

        subject = self.e_subject.get().strip()
        self.running = True
        self.backend.stop_flag = threading.Event()
        self.b_stop.config(state="normal")
        self.legs_left = 1 if want_em else 0
        self.leg_results = []

        def posted(title):
            S.log_row(self.current, f"group:{title}", "", "sent",
                      "posted in group")
            self.lbl_status.config(text=f"Posted in \"{title}\".")
            self.log(f"Posted in the Telegram group \"{title}\". Everyone in "
                     f"it can see it.", "good")
            if not want_em:
                self.running = False
                self._reset_buttons()
                messagebox.showinfo("Posted", f"Your message is now in "
                                              f"\"{title}\".")

        def err(e):
            self.running = False
            self._reset_buttons()
            self.lbl_status.config(text="Could not post.")
            self.log(f"Could not post: {e}", "bad")
            messagebox.showerror("Could not post", str(e))

        async def go():
            if want_tg:
                title = await self.backend.sender.post_to_group(
                    self.post_group["id"], text, self.attachment)
                self.emit("_call", fn=posted, arg=title)
            if want_em and not self.backend.stop_flag.is_set():
                await M.EmailSender(self.cfg, self.emit).run_bcc(
                    em_people, variants, self.state, self.current,
                    self.backend.stop_flag, self.attachment, subject)

        self.backend.submit(go(), on_error=err)

    def _on_recover(self):
        """Restore people (and whole groups) from the append-only journal."""
        rows = []
        for g in S.journal_projects():
            people = S.journal_recover(g)
            if not people:
                continue
            exists = g in self.projects["projects"]
            have = ({t.lower().lstrip("@") for t, _ in self.members}
                    if g == self.current else
                    {t.lower().lstrip("@") for t, _ in S.parse_recipients(
                        S.get_project(self.projects, g).get("recipients", ""))}
                    if exists else set())
            missing = [(t, n) for t, n in people
                       if t.lower().lstrip("@") not in have]
            rows.append({"group": g, "people": people, "missing": missing,
                         "exists": exists})
        if not rows:
            messagebox.showinfo("Nothing to recover",
                                "There is no record of people being added yet.")
            return
        need = [r for r in rows if r["missing"] or not r["exists"]]
        if not need:
            messagebox.showinfo(
                "Nothing missing",
                "Everything in the record is already in your groups.")
            return
        self._recover_dialog(need)

    def _recover_dialog(self, rows):
        win = tk.Toplevel(self.root)
        win.title("Recover people")
        win.configure(bg=PANEL)
        win.geometry("560x430")
        win.transient(self.root)
        tk.Label(win, text="These people were added before but are missing now.",
                 bg=PANEL, fg=TEXT, font=F(12, True)).pack(anchor="w", padx=16,
                                                           pady=(16, 2))
        tk.Label(win, text="Everything you ever add is written to a separate "
                           "record, so it can always be put back. Pick a group "
                           "and click Restore.", bg=PANEL, fg=MUTED, font=F(9),
                 wraplength=510, justify="left").pack(anchor="w", padx=16,
                                                      pady=(0, 10))
        lb = tk.Listbox(win, font=F(11), relief="flat", highlightthickness=1,
                        highlightbackground=LINE, activestyle="none",
                        selectbackground=ACCENT, selectforeground="white")
        lb.pack(fill="both", expand=True, padx=16)
        for r in rows:
            gone = "" if r["exists"] else "   [group is gone too]"
            lb.insert("end", f"  {r['group']} — {len(r['missing'])} missing "
                             f"of {len(r['people'])}{gone}")

        def restore(all_of_them=False):
            picks = rows if all_of_them else (
                [rows[i] for i in lb.curselection()])
            if not picks:
                messagebox.showinfo("Pick a group",
                                    "Click a group in the list first.")
                return
            win.destroy()
            done = 0
            for r in picks:
                g = r["group"]
                if not r["exists"]:
                    S.set_project(self.projects, g, "", "")
                if g == self.current:
                    self.members.extend(r["missing"])
                    self._refresh_members()
                    self._save_current_project()
                else:
                    p = S.get_project(self.projects, g)
                    people = S.parse_recipients(p.get("recipients", "")) \
                        + r["missing"]
                    try:
                        # Pass every field back through. Leaving these out used
                        # to silently wipe the group's send-mode and subject
                        # while "recovering" it.
                        S.set_project(
                            self.projects, g, p.get("message", ""),
                            "\n".join(f"{t}, {n}" if n else t
                                      for t, n in people) + "\n",
                            p.get("attachment", ""), p.get("mode", "private"),
                            p.get("post_group"), p.get("subject", ""),
                            p.get("channels"))
                    except S.SaveError as e:
                        self.log(f"Could not save \"{g}\": {e}", "bad")
                        continue
                done += len(r["missing"])
                self.log(f"Restored {len(r['missing'])} people into \"{g}\".",
                         "good")
            self.projects["current"] = self.current
            self._reload_groups()
            self._load_current_project()
            messagebox.showinfo("Recovered",
                               f"Put back {done} people.")

        br = tk.Frame(win, bg=PANEL)
        br.pack(pady=14)
        tk.Button(br, text="Restore selected", command=restore, bg=GREEN,
                  fg="white", font=F(11, True), relief="flat", bd=0,
                  cursor="hand2", padx=18, pady=8, activebackground=GREEN_DARK,
                  activeforeground="white").pack(side="left", padx=6)
        tk.Button(br, text="Restore everything",
                  command=lambda: restore(True), bg=ACCENT, fg="white",
                  font=F(11, True), relief="flat", bd=0, cursor="hand2",
                  padx=18, pady=8, activebackground=ACCENT_DARK,
                  activeforeground="white").pack(side="left", padx=6)

    def _on_import_group(self):
        if not self._need_telegram():
            return
        self.log("Reading your Telegram groups…", "info")

        def done(groups):
            if not groups:
                messagebox.showinfo("No groups",
                                    "This account is not in any groups.")
                return
            self._pick_tg_group(groups)

        self.backend.submit(self.backend.sender.list_groups(), done,
                            lambda e: self.log(f"Could not read groups: {e}",
                                               "bad"))

    def _pick_tg_group(self, groups):
        win = tk.Toplevel(self.root)
        win.title("Add everyone from a Telegram group")
        win.configure(bg=PANEL)
        win.geometry("540x480")
        win.transient(self.root)
        tk.Label(win, text="Pick a group — everyone in it is added to "
                           f"\"{self.current}\".", bg=PANEL, fg=TEXT,
                 font=F(11), wraplength=500,
                 justify="left").pack(anchor="w", padx=16, pady=(16, 4))
        tk.Label(win, text="People in a group who are not already your contacts "
                           "count as NEW, and only a few of those are sent per "
                           "day on purpose. That limit is what stops Telegram "
                           "banning the account.", bg=PANEL, fg=WARN, font=F(9),
                 wraplength=500, justify="left").pack(anchor="w", padx=16,
                                                      pady=(0, 10))
        lb = tk.Listbox(win, font=F(11), relief="flat", highlightthickness=1,
                        highlightbackground=LINE, activestyle="none",
                        selectbackground=ACCENT, selectforeground="white")
        lb.pack(fill="both", expand=True, padx=16)
        for g in groups:
            lb.insert("end", "  " + g["title"])

        def go():
            sel = lb.curselection()
            if not sel:
                messagebox.showinfo("Pick a group",
                                    "Click a group in the list first.")
                return
            g = groups[sel[0]]
            win.destroy()
            self.log(f"Reading members of \"{g['title']}\" — this can take a "
                     f"minute for a big group.", "info")

            def done(people):
                if not people:
                    messagebox.showinfo(
                        "Nobody found",
                        "Could not read that member list. Some groups hide it "
                        "unless you are an admin.")
                    return
                have = {t.lower().lstrip("@") for t, _ in self.members}
                added = 0
                for t, n in people:
                    if t.lower().lstrip("@") in have:
                        continue
                    self.members.append((t, n))
                    have.add(t.lower().lstrip("@"))
                    added += 1
                self._refresh_members()
                self._save_current_project()
                self.log(f"Added {added} people from \"{g['title']}\" "
                         f"({len(people) - added} were already there).", "good")

            self.backend.submit(self.backend.sender.group_members(g["id"]), done,
                                lambda e: self.log(f"Could not read members: "
                                                   f"{e}", "bad"))

        tk.Button(win, text="Add these people", command=go, bg=GREEN,
                  fg="white", font=F(11, True), relief="flat", bd=0,
                  cursor="hand2", padx=20, pady=8,
                  activebackground=GREEN_DARK,
                  activeforeground="white").pack(pady=14)

    # ============================================================== attach

    def _refresh_attach(self):
        a = self.attachment
        if a:
            missing = not os.path.exists(a)
            self.lbl_attach.config(
                text=("FILE MISSING: " if missing else
                      f"{S.attachment_kind(a)}: ") + os.path.basename(a),
                fg=BAD if missing else GOOD)
            self.b_unattach.pack(side="left", padx=2)
        else:
            self.lbl_attach.config(text="no file — text only", fg=MUTED)
            self.b_unattach.pack_forget()

    def _on_attach(self):
        path = filedialog.askopenfilename(
            title="Choose a photo, video or file to send with the message",
            filetypes=[("Photos and videos",
                        "*.jpg *.jpeg *.png *.gif *.webp *.mp4 *.mov *.avi "
                        "*.mkv *.webm"), ("All files", "*.*")])
        if not path:
            return
        mb = os.path.getsize(path) / (1024 * 1024)
        if mb > 2000:
            messagebox.showwarning("File too big",
                                   "Telegram's limit is 2 GB per file.")
            return
        self.attachment = path
        self._refresh_attach()
        self._save_current_project()
        self.log(f"Attached {S.attachment_kind(path)}: "
                 f"{os.path.basename(path)} ({mb:.1f} MB) — everyone in this "
                 f"group gets it with the message.", "good")
        if mb > 20:
            self.log(f"Big file ({mb:.0f} MB) — each send will take longer to "
                     f"upload.", "warn")

    def _on_unattach(self):
        self.attachment = ""
        self._refresh_attach()
        self._save_current_project()
        self.log("File removed — text only now.", "info")

    # ============================================================== helpers

    def log(self, msg, level="info"):
        from datetime import datetime as _dt
        self.t_log.config(state="normal")
        self.t_log.insert("end", f"{_dt.now():%H:%M:%S}  {msg}\n", level)
        self.t_log.see("end")
        self.t_log.config(state="disabled")

    def _refresh_msg_label(self):
        v = S.parse_message_variants(self.t_msg.get("1.0", "end"))
        if not v:
            self.lbl_msg.config(text="no message yet", fg=BAD)
        elif len(v) == 1:
            self.lbl_msg.config(text="same message to everyone", fg=MUTED)
        else:
            self.lbl_msg.config(text=f"{len(v)} versions, mixed", fg=GOOD)

    def _inputs(self):
        v = S.parse_message_variants(self.t_msg.get("1.0", "end"))
        if not v:
            messagebox.showwarning("No message", "Type your message first.")
            return None
        if not self.members:
            messagebox.showwarning("Nobody in this group",
                                   "Add at least one person to this group "
                                   "first.")
            return None
        return v, list(self.members)

    # ============================================================== actions

    def _on_check(self):
        """Show exactly what would happen, and send nothing."""
        got = self._inputs()
        if not got:
            return
        variants, _ = got
        self._save_current_project()
        self.lbl_status.config(text="Checking…")
        self.state = S.load_state()
        tg_people, em_people = S.split_channels(self.members)

        preview = S.render(variants, (em_people or tg_people or
                                      [("", "John")])[0][1] or "John")

        if self.v_email.get() and em_people:
            ep = M.build_email_plan(em_people, self.state, self.current,
                                    self.cfg)
            self._show_email_plan(ep, variants)
            subject = self.e_subject.get().strip()
            self.log(f"Email subject: {subject or '(none yet — type one)'}",
                     "info" if subject else "warn")

        if not (self.v_tg.get() and tg_people and self.tg_ready):
            self.log("This is what one person receives:", "info")
            self.log("    " + preview.replace("\n", "\n    "), "info")
            self.lbl_status.config(text="Checked. Nothing was sent.")
            return

        self.b_check.config(state="disabled")

        def done(plan):
            self.b_check.config(state="normal")
            self.plan = plan
            self._show_plan(plan, variants)

        def err(e):
            self.b_check.config(state="normal")
            self.lbl_status.config(text="Check failed.")
            self.log(f"Check failed: {e}", "bad")

        self.backend.submit(self.backend.sender.build_plan(
            tg_people, self.state, self.current), done, err)

    def _show_plan(self, plan, variants):
        q = len(plan["queue"])
        self.lbl_status.config(
            text=f"{q} will go out now  ·  about {plan['hours']:.1f} hours  ·  "
                 f"{plan['known_queued']} you know, {plan['new_queued']} new  ·  "
                 f"{plan['left_for_later']} left for later")
        self.log(f"\"{self.current}\": {q} messages now "
                 f"({plan['known_queued']} known, {plan['new_queued']} new).",
                 "info")
        if plan["already_done"]:
            self.log(f"{plan['already_done']} already got this — they will be "
                     f"skipped. Use \"Send again to everyone\" to include them.",
                     "info")
        if plan["left_for_later"]:
            self.log(f"{plan['left_for_later']} left for the next days (daily "
                     f"limit for new people). Nothing is lost — open this "
                     f"tomorrow and it carries on.", "warn")
        if self.attachment:
            miss = not os.path.exists(self.attachment)
            self.log(f"Attached {S.attachment_kind(self.attachment)}: "
                     f"{os.path.basename(self.attachment)}"
                     + ("  — FILE IS MISSING!" if miss else ""),
                     "bad" if miss else "info")
        if q:
            t, n, _ = plan["queue"][0]
            self.log("This is what one person receives:", "info")
            self.log("    " + S.render(variants, n or "John").replace(
                "\n", "\n    "), "info")

    def _channels_ok(self):
        """Are the ticked channels actually usable? Says why if not."""
        if not (self.v_tg.get() or self.v_email.get()):
            messagebox.showwarning(
                "Where should it go?",
                "Tick Telegram, Email, or both, next to \"Send by\".")
            return False
        if self.v_tg.get() and not self._need_telegram():
            return False
        if self.v_email.get():
            if not self._email_configured():
                messagebox.showwarning(
                    "Email is not set up yet",
                    "Press \"Email settings…\" and fill in your email address "
                    "and password.\n\nThen press \"Send myself a test email\" "
                    "to make sure it works before sending to your list.")
                self._on_email_settings()
                return False
            if not self.e_subject.get().strip():
                messagebox.showwarning(
                    "Emails need a subject",
                    "Type a subject in the Subject box — it is the line people "
                    "see in their inbox before they open it.")
                self.e_subject.focus_set()
                return False
        return True

    def _on_send(self):
        if not self._channels_ok():
            return

        variants = S.parse_message_variants(self.t_msg.get("1.0", "end"))
        if not variants:
            messagebox.showwarning("No message", "Type your message first.")
            return
        if not self.members and self.v_mode.get() != "group":
            messagebox.showwarning("Nobody in this group",
                                   "Add at least one person to this group "
                                   "first.")
            return

        self._save_current_project()
        self.state = S.load_state()
        tg_people, em_people = S.split_channels(self.members)

        # ---- one message everybody sees
        if self.v_mode.get() == "group":
            self._send_broadcast(variants, em_people)
            return

        # ---- a separate one to each person
        want_tg, want_em = bool(self.v_tg.get()), bool(self.v_email.get())
        if want_tg and not tg_people:
            want_tg = False
            self.log("Nobody in this group has a Telegram number or username — "
                     "sending by email only.", "warn")
        if want_em and not em_people:
            want_em = False
            self.log("Nobody in this group has an email address — sending by "
                     "Telegram only.", "warn")
        if not (want_tg or want_em):
            messagebox.showinfo(
                "Nothing to send",
                f"Nobody in \"{self.current}\" can be reached the way you have "
                f"ticked.\n\nAdd people as:\n"
                f"    +353871234567, John      (Telegram)\n"
                f"    john@example.com, John   (email)")
            return

        self.email_plan = (M.build_email_plan(em_people, self.state,
                                              self.current, self.cfg)
                           if want_em else None)

        self.b_send.config(state="disabled")
        self.b_check.config(state="disabled")
        self.lbl_status.config(text="Getting ready…")

        if not want_tg:
            self._confirm_and_start(variants, None)
            return

        self.backend.submit(
            self.backend.sender.build_plan(tg_people, self.state, self.current),
            lambda plan: self._confirm_and_start(variants, plan),
            lambda e: (self._reset_buttons(),
                       self.log(f"Could not prepare: {e}", "bad")))

    def _confirm_and_start(self, variants, plan):
        """One confirm box covering whichever channels are going out."""
        tg_people, em_people = S.split_channels(self.members)
        self.plan = plan
        if plan:
            self._show_plan(plan, variants)
        ep = self.email_plan
        if ep:
            self._show_email_plan(ep, variants)

        tg_q = len(plan["queue"]) if plan else 0
        em_q = len(ep["queue"]) if ep else 0

        if tg_q + em_q == 0:
            already = ((plan["already_done"] if plan else 0)
                       + (ep["already_done"] if ep else 0))
            if already:
                if messagebox.askyesno(
                        "They have already had a message",
                        f"Everyone in \"{self.current}\" has already received a "
                        f"message from this group.\n\nThat is why nothing is "
                        f"queued — it stops people being messaged twice by "
                        f"mistake.\n\nSend this NEW message to all "
                        f"{already} of them now?"):
                    self.state = S.reset_project_state(self.state,
                                                       self.current)
                    self.log(f"\"{self.current}\" reset — sending the new "
                             f"message to everyone.", "good")
                    self.email_plan = (
                        M.build_email_plan(em_people, self.state,
                                           self.current, self.cfg)
                        if self.v_email.get() and em_people else None)
                    if self.v_tg.get() and tg_people:
                        self.backend.submit(
                            self.backend.sender.build_plan(
                                tg_people, self.state, self.current),
                            lambda p: self._confirm_and_start(variants, p),
                            lambda e: (self._reset_buttons(),
                                       self.log(f"Could not prepare: {e}",
                                                "bad")))
                    else:
                        self._confirm_and_start(variants, None)
                    return
                self._reset_buttons()
                return
            self._reset_buttons()
            messagebox.showinfo(
                "Nothing to send",
                "There is nobody to send to right now — today's limit is used "
                "up.\n\nOpen this tomorrow and it carries on by itself.")
            return

        if self.attachment and not os.path.exists(self.attachment):
            self._reset_buttons()
            messagebox.showerror(
                "The attached file is gone",
                f"This group has a file attached but it is no longer "
                f"here:\n\n{self.attachment}\n\nAttach it again, or remove "
                f"it to send text only.")
            return

        lines = []
        if tg_q:
            lines.append(f"    {tg_q} on Telegram  "
                         f"({plan['known_queued']} you know, "
                         f"{plan['new_queued']} new)")
        if em_q:
            lines.append(f"    {em_q} by email")
        extra = (f"\nWith {S.attachment_kind(self.attachment)}: "
                 f"{os.path.basename(self.attachment)}"
                 if self.attachment else "")
        hours = (plan["hours"] if plan else 0) + (ep["hours"] if ep else 0)

        # Say up front who is being left out. Otherwise "2 people in this
        # group" followed by "Sent 1 of 1" reads as a lost person.
        skipped = ((plan["already_done"] if plan else 0)
                   + (ep["already_done"] if ep else 0))
        note = ""
        if skipped:
            note = (f"\n\n{skipped} more in this group already had this "
                    f"message, so {'they are' if skipped > 1 else 'it is'} "
                    f"not included. That is why it says "
                    f"{tg_q + em_q} and not {tg_q + em_q + skipped}.\n"
                    f"Use \"Send again to everyone\" if you want "
                    f"{'them' if skipped > 1 else 'it'} to get it again.")

        total = tg_q + em_q
        who = "1 person" if total == 1 else f"{total} people"
        if not messagebox.askyesno(
                "Ready to send",
                f"Group: {self.current}\n\nSend to {who}?\n\n"
                + "\n".join(lines) + f"{extra}{note}\n\nAbout {hours:.1f} "
                f"hours — it sends slowly, like a person.\n\nLeave this window "
                f"open. You can press STOP any time and nobody gets it twice."):
            self._reset_buttons()
            return
        self._start(variants)

    def _show_email_plan(self, plan, variants):
        q = len(plan["queue"])
        self.log(f"Email: {q} to send now"
                 + (f", {plan['left_for_later']} left for tomorrow"
                    if plan["left_for_later"] else "") + ".", "info")
        # Without this, "2 people in the group" and "Sent 1 of 1" look like
        # the app lost somebody.
        if plan["already_done"]:
            n = plan["already_done"]
            self.log(f"{n} of them already had an email from this group, so "
                     f"{'they are' if n > 1 else 'it is'} skipped — nobody "
                     f"gets the same thing twice. Use \"Send again to "
                     f"everyone\" if you want {'them' if n > 1 else 'it'} "
                     f"included.", "warn")
        if plan["left_for_later"]:
            self.log(f"Today's email limit is {plan['cap']}. Nothing is lost — "
                     f"open this tomorrow and it carries on.", "warn")

    def _start(self, variants):
        """Run whichever channels were ticked, one after the other.

        Never both at once: each engine reports its own "finished", and two of
        those arriving together would re-enable the SEND button while the
        second was still going.
        """
        tg_plan = self.plan if (self.plan and self.plan["queue"]) else None
        em_plan = (self.email_plan
                   if (self.email_plan and self.email_plan["queue"]) else None)

        self.running = True
        self.backend.stop_flag = threading.Event()
        self.b_stop.config(state="normal")
        self.legs_left = (1 if tg_plan else 0) + (1 if em_plan else 0)
        self.leg_results = []
        self.pbar.config(maximum=max(1, len(
            (tg_plan or em_plan)["queue"])), value=0)

        subject = self.e_subject.get().strip()

        async def go():
            stop = self.backend.stop_flag
            if tg_plan:
                if self.cfg.get("check_spambot_before_run", True):
                    self.emit("log", message="Checking account health with "
                                             "@SpamBot…", level="info")
                    ok, text = await self.backend.sender.spambot_status()
                    self.emit("log", message="@SpamBot: "
                              + text.replace("\n", " ")[:200],
                              level="good" if ok else "bad")
                    if not ok:
                        self.emit("blocked", text=text)
                        return None
                await self.backend.sender.run(
                    tg_plan, variants, self.state, self.current, stop,
                    self.attachment)
                if stop.is_set():
                    # STOP during Telegram means stop everything.
                    return None
            if em_plan:
                await M.EmailSender(self.cfg, self.emit).run(
                    em_plan, variants, self.state, self.current, stop,
                    self.attachment, subject)
            return None

        self.backend.submit(go(), on_error=self._run_error)

    def _run_error(self, e):
        self.running = False
        self._reset_buttons()
        self.log(f"Stopped because of an error: {e}", "bad")
        self.lbl_status.config(text="Stopped because of an error.")

    def _on_stop(self):
        self.backend.stop_flag.set()
        self.b_stop.config(state="disabled")
        self.lbl_status.config(text="Stopping…")
        self.log("Stopping. Progress saved — nobody gets it twice.", "warn")

    def _reset_buttons(self):
        self.b_send.config(state="normal")
        self.b_check.config(state="normal")
        self.b_stop.config(state="disabled")

    def _on_report(self):
        """Exactly who received what, and when. Proof, not guesswork."""
        rows, summary = S.delivery_report(self.current)
        win = tk.Toplevel(self.root)
        win.title(f"Delivery report — {self.current}")
        win.configure(bg=PANEL)
        win.geometry("780x520")
        win.transient(self.root)

        head = tk.Frame(win, bg=PANEL)
        head.pack(fill="x", padx=16, pady=(16, 6))
        tk.Label(head, text=f"{self.current}", bg=PANEL, fg=TEXT,
                 font=F(14, True)).pack(side="left")
        # Which channel a row came from is worked out from the address itself,
        # never from the detail column — that column already carries other
        # things like "after wait" for Telegram rows.
        by_email = sum(1 for r in rows
                       if r.get("status") == "sent"
                       and S.is_email(r.get("target", "")))
        by_tg = summary["sent"] - by_email
        counts = []
        if by_tg:
            counts.append(f"{by_tg} delivered by Telegram")
        if by_email:
            counts.append(f"{by_email} delivered by email")
        if not counts:
            counts.append(f"{summary['sent']} delivered")

        tk.Label(head, text="   " + "   ·   ".join(counts)
                            + (f"   ·   {summary['failed']} could not be reached"
                               if summary["failed"] else "")
                            + f"   ·   over {summary['days']} day(s)",
                 bg=PANEL, fg=GOOD if not summary["failed"] else WARN,
                 font=F(11)).pack(side="left")

        tk.Label(win, text="Every message this app has actually sent for this "
                           "group. Times are when the message was accepted for "
                           "delivery.",
                 bg=PANEL, fg=MUTED, font=F(9)).pack(anchor="w", padx=16)

        wrap = tk.Frame(win, bg=PANEL)
        wrap.pack(fill="both", expand=True, padx=16, pady=8)
        cols = ("when", "how", "who", "name", "status")
        tv = ttk.Treeview(wrap, columns=cols, show="headings")
        for c, w in zip(cols, (145, 80, 200, 130, 210)):
            tv.heading(c, text={"when": "When", "how": "How",
                                "who": "Sent to", "name": "Name",
                                "status": "Result"}[c])
            tv.column(c, width=w, anchor="w")
        for r in rows:
            when = str(r.get("timestamp", "")).replace("T", "  ")
            st = r.get("status", "")
            target = str(r.get("target", ""))
            how = "Email" if S.is_email(target) else "Telegram"
            nice = {"sent": "delivered",
                    "cannot_receive": "blocked you / privacy setting",
                    "not_found": "number not found",
                    "peer_flood": "STOPPED — Telegram spam flag",
                    "auth_failed": "email password not accepted",
                    "bad_address": "email address refused",
                    "sender_refused": "your own address was refused",
                    "daily_limit": "your email provider's daily limit",
                    "rejected": "the mail server refused it",
                    "disconnected": "the mail server hung up",
                    "timeout": "the mail server did not answer",
                    "no_server": "mail server not found",
                    "ssl_error": "secure connection failed",
                    "refused": "mail server refused the connection",
                    "no_connection": "no internet",
                    "no_tls": "mail server does not support secure sending",
                    "error": "error"}.get(st, st)
            if r.get("detail") and st != "sent":
                nice += f"  ({r['detail']})"
            # leading space keeps Tcl from reading "+3538..." as a number and
            # swallowing the plus sign
            tv.insert("", "end", values=(when, how, " " + target,
                                         r.get("name", ""), nice),
                      tags=("ok" if st == "sent" else "bad",))
        tv.tag_configure("ok", foreground=GOOD)
        tv.tag_configure("bad", foreground=BAD)
        tv.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        sb.pack(side="right", fill="y")
        tv.configure(yscrollcommand=sb.set)

        if not rows:
            tk.Label(win, text="Nothing has been sent for this group yet.",
                     bg=PANEL, fg=MUTED, font=F(11)).pack(pady=8)

        br = tk.Frame(win, bg=PANEL)
        br.pack(pady=(0, 14))
        tk.Button(br, text="Open the full log in Excel", command=self._open_log,
                  bg=ACCENT, fg="white", font=F(10, True), relief="flat", bd=0,
                  cursor="hand2", padx=16, pady=7,
                  activebackground=ACCENT_DARK,
                  activeforeground="white").pack(side="left", padx=6)
        tk.Button(br, text="Close", command=win.destroy, relief="flat", bd=0,
                  cursor="hand2", font=F(10), padx=16, pady=7).pack(side="left")

    def _open_log(self):
        path = S.LOG_PATH
        if not os.path.exists(path):
            messagebox.showinfo("No log yet", "Nothing has been sent yet.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}" &')
        except Exception as e:
            messagebox.showinfo("Log file", f"The full log is here:\n\n{path}\n\n({e})")

    def _on_health(self):
        if not self._need_telegram():
            return
        self.log("Asking @SpamBot about your account…", "info")

        def done(res):
            ok, text = res
            self.log("@SpamBot: " + text.replace("\n", " ")[:300],
                     "good" if ok else "bad")
            messagebox.showinfo("Account health",
                                ("Your account looks fine.\n\n" if ok else
                                 "YOUR ACCOUNT IS CURRENTLY LIMITED.\nDo not "
                                 "send today. Open @SpamBot in Telegram and "
                                 "follow its buttons to appeal.\n\n")
                                + text[:600])

        self.backend.submit(self.backend.sender.spambot_status(), done,
                            lambda e: self.log(f"Health check failed: {e}",
                                               "bad"))

    def _on_new_campaign(self):
        ps = S.project_state(self.state, self.current)
        if not messagebox.askyesno(
                "Send again to everyone?",
                f"\"{self.current}\" has already been sent to "
                f"{len(ps['sent'])} people.\n\nThis forgets that record so "
                f"everyone in the group can be messaged again with your new "
                f"message.\n\nContinue?"):
            return
        self.state = S.reset_project_state(self.state, self.current)
        self.log(f"\"{self.current}\" reset — everyone can receive the next "
                 f"message.", "good")
        self.lbl_status.config(text="Ready to send to everyone again.")

    # ============================================================== pump

    def _heartbeat(self):
        """Keeps the lock file fresh so a second copy knows we are alive."""
        S.lock_touch()
        self.root.after(5000, self._heartbeat)

    def _on_leg_finished(self, d):
        """One channel has finished. Only tidy up once they all have — the
        Telegram part finishing must not free up the SEND button while the
        email part is still going."""
        where = d.get("channel", "Telegram")
        self.leg_results.append(d)
        self.legs_left = max(0, self.legs_left - 1)

        line = (f"{where}: sent {d['sent']} of {d['total']}."
                + (f" {d['failed']} could not be reached."
                   if d["failed"] else ""))
        self.log(f"{line}  Reason: {d['reason']}",
                 "good" if d["sent"] else "warn")

        if self.legs_left > 0:
            self.lbl_status.config(text=f"{line}  Now starting the email part…")
            return

        self.running = False
        self._reset_buttons()
        self.state = S.load_state()

        sent = sum(r["sent"] for r in self.leg_results)
        failed = sum(r["failed"] for r in self.leg_results)
        total = sum(r["total"] for r in self.leg_results)
        parts = [f"{r.get('channel', 'Telegram')}: {r['sent']} of {r['total']}"
                 for r in self.leg_results]
        msg = (f"Done. Sent {sent} of {total}."
               + (f" {failed} could not be reached." if failed else ""))
        if len(self.leg_results) > 1:
            msg += "\n\n" + "\n".join(parts)

        # "Sent 1 of 1" out of a group of 2 looks like somebody was lost.
        skipped = ((self.plan or {}).get("already_done", 0)
                   + (self.email_plan or {}).get("already_done", 0))
        if skipped:
            msg += (f"\n\n{skipped} other{'s' if skipped > 1 else ''} in this "
                    f"group had already been sent this, so "
                    f"{'they were' if skipped > 1 else 'it was'} skipped — "
                    f"nobody gets it twice.\n\nUse \"Send again to everyone\" "
                    f"at the top if you want everybody to get it.")

        self.lbl_status.config(
            text=f"Done. Sent {sent} of {total}."
                 + (f"  ({self.leg_results[-1]['reason']})"))
        if any(r.get("channel") != "Email" for r in self.leg_results):
            self.log("Telegram replies arrive in your normal Telegram, in each "
                     "person's private chat.", "info")
        if any(r.get("channel") == "Email" for r in self.leg_results):
            self.log("Email replies arrive in your normal inbox.", "info")
        messagebox.showinfo("Finished", msg)

    def _drain(self):
        try:
            while True:
                kind, d = self.q.get_nowait()
                if kind == "_call":
                    d["fn"](d["arg"])
                elif kind == "log":
                    self.log(d["message"], d.get("level", "info"))
                elif kind == "progress":
                    # each leg re-sizes the bar for its own total
                    self.pbar.config(maximum=max(1, d["total"]),
                                     value=d["done"])
                    where = d.get("channel", "Telegram")
                    self.lbl_status.config(
                        text=f"{where}: sent {d['sent']} of {d['total']}"
                             + (f"  ·  {d['failed']} could not be reached"
                                if d["failed"] else ""))
                elif kind == "waiting":
                    s = int(d["seconds"])
                    wk = d.get("wait_kind")
                    cur = self.lbl_status.cget("text").split("   |   ")[0]
                    if wk == "night":
                        txt = f"Sleeping until morning — {s//3600}h {s%3600//60}m"
                    elif wk == "break":
                        txt = f"On a break — back in {s//60}m {s%60:02d}s"
                    else:
                        txt = f"Next message in {s//60}m {s%60:02d}s"
                    self.lbl_status.config(text=f"{cur}   |   {txt}")
                elif kind == "blocked":
                    self.running = False
                    self._reset_buttons()
                    self.lbl_status.config(text="Blocked — account is limited.")
                    messagebox.showerror(
                        "Cannot send today",
                        "Telegram has limited this account, so nothing was "
                        "sent.\n\nOpen @SpamBot in Telegram and appeal. Limits "
                        "are usually lifted in 24–72 hours.\n\n"
                        + d["text"][:500])
                elif kind == "email_blocked":
                    self.lbl_status.config(text="Email settings need fixing.")
                    messagebox.showerror("Email did not go out", d["text"])
                elif kind == "finished":
                    self._on_leg_finished(d)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _on_close(self):
        if self.running and not messagebox.askyesno(
                "Still sending",
                "It is still sending. Close anyway?\n\nProgress is saved — you "
                "can carry on later and nobody gets it twice."):
            return
        try:
            if self.main.winfo_ismapped():
                # Only write if the file has not been changed by someone else
                # since we loaded it — otherwise a second copy of the app would
                # overwrite the newer data on the way out.
                on_disk = S.load_projects()
                mine = S.get_project(self.projects, self.current)
                theirs = S.get_project(on_disk, self.current)
                changed_elsewhere = (
                    theirs.get("recipients", "") != mine.get("recipients", "")
                    and len(S.parse_recipients(theirs.get("recipients", "")))
                    > len(self.members))
                if changed_elsewhere:
                    self.log("Another copy of the app changed this group — not "
                             "overwriting it on the way out.", "warn")
                else:
                    self._save_current_project(quiet=True)
        except Exception:
            pass
        S.lock_release()
        self.backend.stop_flag.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    if S.lock_is_held():
        if not messagebox.askyesno(
                "Already running",
                "Telegram Sender looks like it is already open in another "
                "window.\n\nRunning two copies at once can lose your groups and "
                "people, because each one saves over the other.\n\n"
                "Open a second copy anyway? (Not recommended — click No, and "
                "use the window that is already open.)"):
            root.destroy()
            return
    S.lock_touch()
    app = App(root)
    if app.migrated:
        app.log(f"Moved your data somewhere safe: {S.DATA}", "good")
        app.log(f"({', '.join(app.migrated)}) — OneDrive can no longer "
                f"interfere with it.", "info")
    app.log(f"Your groups and login are stored in: {S.DATA}", "info")
    if S.in_onedrive():
        app.log("This app folder is inside OneDrive, but your data is NOT — it "
                "is kept outside, so syncing cannot affect your groups.",
                "info")
    root.mainloop()


if __name__ == "__main__":
    main()
