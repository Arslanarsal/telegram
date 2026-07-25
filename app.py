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
        self.cfg = S.load_config()
        self.state = S.load_state()
        self.projects = S.load_projects()
        self.current = self.projects.get("current") or \
            S.project_names(self.projects)[0]
        self.backend = Backend(self.emit)
        self.plan = None
        self.running = False
        self.attachment = ""
        self.members = []          # [(target, name)] for the current group

        root.title("Telegram Sender")
        root.geometry("1120x780")
        root.minsize(960, 660)
        root.configure(bg=BG)

        self._header()
        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self._setup_screen()
        self._main_screen()

        self.root.after(80, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if not S.config_ready(self.cfg):
            self.show_setup("Let's connect your Telegram account. One time only.")
        else:
            self.show_setup("Connecting to Telegram…")
            self._try_connect()

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
        self.b_connect.pack(anchor="w", padx=24, pady=(6, 22))
        self.setup_status = tk.Label(self.setup, text="", bg=BG, fg=MUTED,
                                     wraplength=860, justify="left", font=F(11))
        self.setup_status.pack(anchor="w", padx=44)

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
        flat_btn(bar, "Speed & limits", self._on_settings).pack(side="right")
        flat_btn(bar, "Send again to everyone",
                 self._on_new_campaign).pack(side="right")
        tk.Frame(right, bg=LINE, height=1).pack(fill="x")

        mid = tk.Frame(right, bg=BG)
        mid.pack(fill="both", expand=True, padx=14, pady=12)

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

        ml = tk.Frame(pc, bg=PANEL)
        ml.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.lst_members = tk.Listbox(
            ml, font=(MONO, 10), relief="flat", bd=0, highlightthickness=1,
            highlightbackground=LINE, activestyle="none", bg="#fbfbfc", fg=TEXT,
            selectmode="extended", selectbackground=ACCENT,
            selectforeground="white", exportselection=False, height=7)
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
        tk.Label(cc, text="Message", bg=PANEL, fg=TEXT,
                 font=F(11, True)).pack(anchor="w", padx=12, pady=(10, 2))
        tk.Label(cc, text="Type it once. Optional: {name} becomes their first "
                          "name.", bg=PANEL, fg=MUTED,
                 font=F(9)).pack(anchor="w", padx=12)
        self.t_msg = tk.Text(cc, height=6, wrap="word", font=F(11),
                             relief="flat", bg="#fbfbfc", highlightthickness=1,
                             highlightbackground=LINE, highlightcolor=ACCENT,
                             undo=True, padx=8, pady=8)
        self.t_msg.pack(fill="x", padx=12, pady=(6, 8))
        self.t_msg.bind("<KeyRelease>", lambda e: self._refresh_msg_label())

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

        # -------- status + log
        st = tk.Frame(right, bg=BG)
        st.pack(fill="both", expand=False, padx=14, pady=(0, 10))
        self.pbar = ttk.Progressbar(st, mode="determinate")
        self.pbar.pack(fill="x")
        self.lbl_status = tk.Label(st, text="Ready.", bg=BG, fg=MUTED,
                                   anchor="w", font=F(10))
        self.lbl_status.pack(fill="x", pady=(4, 4))
        self.t_log = scrolledtext.ScrolledText(
            st, height=6, wrap="word", font=(MONO, 9), state="disabled",
            relief="flat", bg="#fbfbfc", highlightthickness=1,
            highlightbackground=LINE)
        self.t_log.pack(fill="both", expand=True)
        for tag, col in (("good", GOOD), ("bad", BAD), ("warn", WARN),
                         ("info", TEXT)):
            self.t_log.tag_config(tag, foreground=col)

    def show_main(self):
        self.setup.pack_forget()
        self.main.pack(fill="both", expand=True)
        self._reload_groups()
        self._load_current_project()
        k, n = S.sent_today(self.state)
        ceil = S.warmup_ceiling(self.state, self.cfg)
        self.log(f"Connected. Today so far: {k} to people you know, {n} to new "
                 f"people.", "info")
        if ceil is not None:
            self.log(f"First-week warm-up: up to {ceil} messages today. It "
                     f"rises daily, then there is no limit for your own "
                     f"contacts.", "info")
        else:
            self.log(f"No daily limit for people you already know. New people "
                     f"are capped at {self.cfg['daily_cap_new']} per day.",
                     "info")

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
        self._refresh_members()
        self._refresh_attach()
        self._refresh_msg_label()
        self.lbl_gname.config(text=self.current)

    def _recipients_text(self):
        return "\n".join(f"{t}, {n}" if n else t for t, n in self.members) + "\n"

    def _save_current_project(self):
        S.set_project(self.projects, self.current,
                      self.t_msg.get("1.0", "end").rstrip() + "\n",
                      self._recipients_text(), self.attachment)

    def _refresh_members(self):
        self.lst_members.delete(0, "end")
        for t, n in self.members:
            self.lst_members.insert("end", f" {t:<22} {n or ''}")
        self.lbl_gcount.config(text=f"{len(self.members)} people")
        self._reload_groups()

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

    def _on_add_person(self):
        raw = self.e_add.get().strip()
        if not raw:
            messagebox.showinfo("Nothing typed",
                                "Type a phone number or @username, e.g.\n\n"
                                "+353871234567, John")
            return
        have = {t.lower().lstrip("@") for t, _ in self.members}
        added = 0
        for t, n in S.parse_recipients(raw.replace(";", "\n")):
            if t.lower().lstrip("@") in have:
                continue
            self.members.append((t, n))
            have.add(t.lower().lstrip("@"))
            added += 1
        self.e_add.delete(0, "end")
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
        for i in sorted(sel, reverse=True):
            del self.members[i]
        self._refresh_members()
        self._save_current_project()
        self.log(f"Removed {len(sel)} from \"{self.current}\" "
                 f"({', '.join(names[:3])}{'…' if len(names) > 3 else ''}).",
                 "warn")

    def _on_load_file(self):
        path = filedialog.askopenfilename(
            title="Choose a file with usernames or phone numbers",
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
        for t, n in S.parse_recipients(text):
            if t.lower().lstrip("@") in have:
                continue
            self.members.append((t, n))
            have.add(t.lower().lstrip("@"))
            added += 1
        self._refresh_members()
        self._save_current_project()
        self.log(f"Added {added} people from {os.path.basename(path)}.", "good")

    def _on_import_group(self):
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
        got = self._inputs()
        if not got:
            return
        variants, recipients = got
        self._save_current_project()
        self.b_check.config(state="disabled")
        self.lbl_status.config(text="Checking…")
        self.state = S.load_state()

        def done(plan):
            self.b_check.config(state="normal")
            self.plan = plan
            self._show_plan(plan, variants)

        def err(e):
            self.b_check.config(state="normal")
            self.lbl_status.config(text="Check failed.")
            self.log(f"Check failed: {e}", "bad")

        self.backend.submit(self.backend.sender.build_plan(
            recipients, self.state, self.current), done, err)

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

    def _on_send(self):
        got = self._inputs()
        if not got:
            return
        variants, recipients = got
        self._save_current_project()
        self.state = S.load_state()
        self.b_send.config(state="disabled")
        self.b_check.config(state="disabled")
        self.lbl_status.config(text="Getting ready…")

        def after(plan):
            self.plan = plan
            self._show_plan(plan, variants)
            q = len(plan["queue"])
            if q == 0:
                self._reset_buttons()
                messagebox.showinfo(
                    "Nothing to send",
                    "Everyone in this group already got this message, or "
                    "today's limit for new people is used up.\n\nOpen this "
                    "tomorrow and it carries on by itself.\n\nTo message the "
                    "same people again, use \"Send again to everyone\".")
                return
            if self.attachment and not os.path.exists(self.attachment):
                self._reset_buttons()
                messagebox.showerror(
                    "The attached file is gone",
                    f"This group has a file attached but it is no longer "
                    f"here:\n\n{self.attachment}\n\nAttach it again, or remove "
                    f"it to send text only.")
                return
            extra = (f"\nWith {S.attachment_kind(self.attachment)}: "
                     f"{os.path.basename(self.attachment)}"
                     if self.attachment else "")
            if not messagebox.askyesno(
                    "Ready to send",
                    f"Group: {self.current}\n\nSend to {q} people?\n"
                    f"({plan['known_queued']} you know, {plan['new_queued']} "
                    f"new){extra}\n\nAbout {plan['hours']:.1f} hours — it sends "
                    f"slowly, like a person typing.\n\nLeave this window open. "
                    f"You can press STOP any time and nobody gets it twice."):
                self._reset_buttons()
                return
            self._start(variants)

        self.backend.submit(
            self.backend.sender.build_plan(recipients, self.state, self.current),
            after, lambda e: (self._reset_buttons(),
                              self.log(f"Could not prepare: {e}", "bad")))

    def _start(self, variants):
        self.running = True
        self.backend.stop_flag = threading.Event()
        self.b_stop.config(state="normal")
        self.pbar.config(maximum=max(1, len(self.plan["queue"])), value=0)

        async def go():
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
            return await self.backend.sender.run(
                self.plan, variants, self.state, self.current,
                self.backend.stop_flag, self.attachment)

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

    def _on_health(self):
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

    def _on_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Speed & limits")
        win.configure(bg=PANEL)
        win.geometry("620x520")
        win.transient(self.root)

        tk.Label(win, text="Speed", bg=PANEL, fg=TEXT,
                 font=F(13, True)).pack(anchor="w", padx=18, pady=(18, 2))
        tk.Label(win, text="Slower is safer. The pacing is the main thing "
                           "protecting the account.", bg=PANEL, fg=MUTED,
                 font=F(9), wraplength=560,
                 justify="left").pack(anchor="w", padx=18)
        speed = tk.StringVar(value=self.cfg.get("speed", "safest"))
        for key, p in S.SPEED_PRESETS.items():
            tk.Radiobutton(win, text=p["label"], variable=speed, value=key,
                           bg=PANEL, font=F(11), anchor="w",
                           fg=BAD if key == "fast" else TEXT,
                           selectcolor=PANEL).pack(anchor="w", padx=28)

        tk.Label(win, text="Daily limits", bg=PANEL, fg=TEXT,
                 font=F(13, True)).pack(anchor="w", padx=18, pady=(16, 2))
        unlimited = tk.BooleanVar(value=self.cfg.get("unlimited_known", True))
        tk.Checkbutton(win, variable=unlimited, bg=PANEL, font=F(11),
                       anchor="w", selectcolor=PANEL,
                       text="No daily limit for people I already know "
                            "(recommended)").pack(anchor="w", padx=28)
        tk.Label(win, text="Your saved contacts and anyone you already have a "
                           "chat with. Telegram's spam limits target strangers, "
                           "not these people.", bg=PANEL, fg=MUTED, font=F(9),
                 wraplength=540, justify="left").pack(anchor="w", padx=48)

        f2 = tk.Frame(win, bg=PANEL)
        f2.pack(anchor="w", padx=28, pady=(10, 0))
        tk.Label(f2, text="Max NEW people per day:", bg=PANEL,
                 font=F(11)).pack(side="left")
        e_new = tk.Entry(f2, width=6, font=F(11))
        e_new.pack(side="left", padx=8)
        e_new.insert(0, str(self.cfg.get("daily_cap_new", 15)))
        tk.Label(win, text="People you have never messaged. THIS is the number "
                           "that gets accounts banned. 15 or less is sensible; "
                           "above 40 is asking for trouble.", bg=PANEL, fg=WARN,
                 font=F(9), wraplength=540,
                 justify="left").pack(anchor="w", padx=48)

        f3 = tk.Frame(win, bg=PANEL)
        f3.pack(anchor="w", padx=28, pady=(12, 0))
        tk.Label(f3, text="Sending hours:", bg=PANEL, font=F(11)).pack(side="left")
        e_from = tk.Entry(f3, width=7, font=F(11))
        e_from.pack(side="left", padx=6)
        e_from.insert(0, self.cfg.get("active_hours_start", "09:00"))
        tk.Label(f3, text="to", bg=PANEL, font=F(11)).pack(side="left")
        e_to = tk.Entry(f3, width=7, font=F(11))
        e_to.pack(side="left", padx=6)
        e_to.insert(0, self.cfg.get("active_hours_end", "21:30"))

        warm = tk.BooleanVar(value=self.cfg.get("use_warmup", True))
        tk.Checkbutton(win, variable=warm, bg=PANEL, font=F(11),
                       selectcolor=PANEL,
                       text="Use the first-week warm-up (recommended)").pack(
            anchor="w", padx=28, pady=(12, 0))

        def save():
            try:
                n = int(e_new.get().strip())
                if n < 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("Check that number",
                                       "Max new people per day must be a whole "
                                       "number, 0 or more.")
                return
            for e in (e_from, e_to):
                try:
                    S.parse_hhmm(e.get().strip())
                except Exception:
                    messagebox.showwarning("Check the times",
                                           "Times must look like 09:00")
                    return
            if n > 40 and not messagebox.askyesno(
                    "That is high",
                    f"{n} new people per day is above what accounts survive "
                    f"long-term.\n\nSet it anyway?"):
                return
            if speed.get() == "fast" and not messagebox.askyesno(
                    "Fast is risky",
                    "Fast mode sends every 8–30 seconds — the pattern "
                    "Telegram's spam system looks for.\n\nUse it anyway?"):
                return
            S.apply_speed(self.cfg, speed.get())
            self.cfg["unlimited_known"] = bool(unlimited.get())
            self.cfg["daily_cap_new"] = n
            self.cfg["active_hours_start"] = e_from.get().strip()
            self.cfg["active_hours_end"] = e_to.get().strip()
            self.cfg["use_warmup"] = bool(warm.get())
            S.save_config(self.cfg)
            self.log(f"Settings saved — speed: {self.cfg['speed']}, new people "
                     f"per day: {n}.", "good")
            win.destroy()

        tk.Button(win, text="Save", command=save, bg=GREEN, fg="white",
                  font=F(12, True), relief="flat", bd=0, cursor="hand2",
                  padx=26, pady=8, activebackground=GREEN_DARK,
                  activeforeground="white").pack(pady=18)

    # ============================================================== pump

    def _drain(self):
        try:
            while True:
                kind, d = self.q.get_nowait()
                if kind == "_call":
                    d["fn"](d["arg"])
                elif kind == "log":
                    self.log(d["message"], d.get("level", "info"))
                elif kind == "progress":
                    self.pbar.config(value=d["done"])
                    self.lbl_status.config(
                        text=f"Sent {d['sent']} of {d['total']}"
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
                elif kind == "finished":
                    self.running = False
                    self._reset_buttons()
                    self.state = S.load_state()
                    msg = (f"Done. Sent {d['sent']} of {d['total']}."
                           + (f" {d['failed']} could not be reached."
                              if d["failed"] else ""))
                    self.lbl_status.config(text=f"{msg}  ({d['reason']})")
                    self.log(f"{msg}  Reason: {d['reason']}", "good")
                    self.log("Replies arrive in your normal Telegram, in each "
                             "person's private chat.", "info")
                    messagebox.showinfo("Finished", msg)
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
                self._save_current_project()
        except Exception:
            pass
        self.backend.stop_flag.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
