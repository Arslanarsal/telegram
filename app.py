#!/usr/bin/env python3
"""
Telegram Sender — the window the user clicks. Nothing technical required.
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

BG = "#f4f6f8"
CARD = "#ffffff"
ACCENT = "#2481cc"
GOOD = "#1a7f37"
BAD = "#b3261e"
WARN = "#a15c00"
MUTED = "#5f6b76"

# Windows has no Helvetica; picking per-platform avoids ugly fallback fonts.
if sys.platform.startswith("win"):
    UI, MONO = "Segoe UI", "Consolas"
elif sys.platform == "darwin":
    UI, MONO = "Helvetica Neue", "Menlo"
else:
    UI, MONO = "DejaVu Sans", "DejaVu Sans Mono"


def F(size=11, bold=False):
    return (UI, size, "bold") if bold else (UI, size)


class Backend:
    """One asyncio loop on a background thread for the whole app."""

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
        self.current = self.projects.get("current") or S.project_names(self.projects)[0]
        self.backend = Backend(self.emit)
        self.plan = None
        self.running = False

        root.title("Telegram Sender")
        root.geometry("1060x800")
        root.minsize(920, 680)
        root.configure(bg=BG)

        self._header()
        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(0, 12))
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

    # ---------------------------------------------------------------- chrome

    def _header(self):
        h = tk.Frame(self.root, bg=ACCENT, height=62)
        h.pack(fill="x")
        h.pack_propagate(False)
        tk.Label(h, text="  Telegram Sender", bg=ACCENT, fg="white",
                 font=F(19, True)).pack(side="left", padx=10)
        self.who = tk.Label(h, text="", bg=ACCENT, fg="white", font=F(11))
        self.who.pack(side="right", padx=16)

    def _card(self, parent, title=None):
        c = tk.Frame(parent, bg=CARD, highlightbackground="#dde3e9",
                     highlightthickness=1)
        if title:
            tk.Label(c, text=title, bg=CARD, fg="#1b1f24",
                     font=F(12, True)).pack(anchor="w", padx=12, pady=(10, 0))
        return c

    # ---------------------------------------------------------------- setup

    def _setup_screen(self):
        self.setup = tk.Frame(self.body, bg=BG)
        c = self._card(self.setup, "Connect your Telegram account")
        c.pack(fill="x", pady=(16, 0))
        self.setup_msg = tk.Label(c, text="", bg=CARD, fg=MUTED, justify="left",
                                  wraplength=900, font=F(11))
        self.setup_msg.pack(anchor="w", padx=12, pady=(4, 8))
        # These two blocks are hidden entirely when the app ships with the
        # api keys already filled in — then all the user does is type a phone
        # number, and never sees my.telegram.org at all.
        self.api_help = tk.Frame(c, bg=CARD)
        self.api_help.pack(anchor="w", fill="x")
        tk.Label(self.api_help, text=(
            "Step 1  Click the button below — it opens my.telegram.org.\n"
            "Step 2  Log in with your phone number (the code arrives inside "
            "the Telegram app, not by SMS).\n"
            "Step 3  Click \"API development tools\". App title: mysender.  "
            "Short name: mysender.  Platform: Desktop.\n"
            "Step 4  Copy the two values it gives you — api_id and api_hash — "
            "into the boxes below.\n\nYou only do this once."),
            bg=CARD, fg="#1b1f24", justify="left", wraplength=900,
            font=F(11)).pack(anchor="w", padx=12)
        tk.Button(self.api_help, text="Open my.telegram.org", bg=ACCENT,
                  fg="white", font=F(11, True), relief="flat", cursor="hand2",
                  command=lambda: webbrowser.open("https://my.telegram.org")
                  ).pack(anchor="w", padx=12, pady=10)

        form = tk.Frame(c, bg=CARD)
        form.pack(anchor="w", padx=12, pady=(0, 12))
        self.api_rows = form
        self.e_api_id = self._field(form, 0, "api_id", 32,
                                    str(self.cfg.get("api_id") or ""))
        h = self.cfg.get("api_hash", "")
        self.e_api_hash = self._field(form, 1, "api_hash", 48,
                                      "" if "PUT_YOUR" in str(h) else h)
        self.e_phone = self._field(form, 2, "Your phone number", 32,
                                   self.cfg.get("last_phone", ""))
        self.lbl_phone_hint = tk.Label(
            form, text="with country code, e.g. +353871234567", bg=CARD,
            fg=MUTED, font=F(9))
        self.lbl_phone_hint.grid(row=3, column=1, sticky="w")

        self.b_connect = tk.Button(c, text="Connect", bg=GOOD, fg="white",
                                   font=F(12, True), relief="flat",
                                   cursor="hand2", command=self._on_connect)
        self.b_connect.pack(anchor="w", padx=12, pady=(0, 14))
        self.setup_status = tk.Label(self.setup, text="", bg=BG, fg=MUTED,
                                     wraplength=900, justify="left", font=F(11))
        self.setup_status.pack(anchor="w", pady=8)

    def _field(self, parent, row, label, width, value):
        tk.Label(parent, text=label, bg=CARD, width=11, anchor="w",
                 font=F(11)).grid(row=row, column=0, pady=4)
        e = tk.Entry(parent, width=width, font=F(11))
        e.grid(row=row, column=1, pady=4)
        e.insert(0, value)
        return e

    def preconfigured(self):
        """True when the app already ships with working api keys."""
        return S.config_ready(self.cfg)

    def show_setup(self, msg=""):
        if hasattr(self, "main"):
            self.main.pack_forget()
        self.setup.pack(fill="both", expand=True)
        if self.preconfigured():
            # hide the whole my.telegram.org rigmarole — just ask for a phone
            self.api_help.pack_forget()
            for w in (self.e_api_id, self.e_api_hash):
                w.grid_remove()
            for lbl in self.api_rows.grid_slaves(row=0) + \
                    self.api_rows.grid_slaves(row=1):
                lbl.grid_remove()
            if not msg:
                msg = ("Type your phone number and click Connect.\n\n"
                       "Telegram will send you a login code inside the "
                       "Telegram app (not by SMS).")
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
        self.setup_status.config(
            text=f"Could not connect: {e}\n\nCheck the api_id / api_hash and "
                 f"your internet connection.", fg=BAD)

    def _after_connect(self, res):
        st = res["state"]
        if st == "ready":
            self.who.config(text=f"Sending as: {res['name']}"
                            + (f"  (@{res['username']})" if res.get("username")
                               else ""))
            self.show_main()
        elif st == "need_phone":
            self.b_connect.config(state="normal")
            self.setup_msg.config(
                text="Type your phone number and click Connect.\n\nTelegram "
                     "sends you a login code inside the Telegram app — not by "
                     "SMS." if self.preconfigured() else
                     "Let's connect your Telegram account. One time only.")
            self.setup_status.config(text="", fg=MUTED)
        elif st == "need_code":
            code = simpledialog.askstring(
                "Telegram sent you a code",
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

    # ---------------------------------------------------------------- main

    def _main_screen(self):
        self.main = tk.Frame(self.body, bg=BG)

        # ---- project bar
        pb = self._card(self.main)
        pb.pack(fill="x", pady=(14, 0))
        row = tk.Frame(pb, bg=CARD)
        row.pack(fill="x", padx=12, pady=10)
        tk.Label(row, text="Project:", bg=CARD, font=F(12, True)).pack(side="left")
        self.cb_project = ttk.Combobox(row, state="readonly", width=30,
                                       font=F(11))
        self.cb_project.pack(side="left", padx=8)
        self.cb_project.bind("<<ComboboxSelected>>", self._on_switch_project)
        tk.Button(row, text="New", relief="flat", cursor="hand2", font=F(10),
                  command=self._on_new_project).pack(side="left", padx=2)
        tk.Button(row, text="Rename", relief="flat", cursor="hand2", font=F(10),
                  command=self._on_rename_project).pack(side="left", padx=2)
        tk.Button(row, text="Delete", relief="flat", cursor="hand2", font=F(10),
                  command=self._on_delete_project).pack(side="left", padx=2)
        tk.Label(row, text="each project keeps its own message, its own list, "
                           "and its own record of who was sent to",
                 bg=CARD, fg=MUTED, font=F(9)).pack(side="left", padx=12)

        top = tk.Frame(self.main, bg=BG)
        top.pack(fill="both", expand=True, pady=(12, 0))

        # ---- message
        left = self._card(top, "1.  Your message  (type it once)")
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="Just type your message. Optional: {name} becomes "
                            "their first name.", bg=CARD, fg=MUTED,
                 wraplength=480, justify="left",
                 font=F(9)).pack(anchor="w", padx=12, pady=(2, 6))
        self.t_msg = scrolledtext.ScrolledText(left, height=13, wrap="word",
                                               font=F(11), relief="solid",
                                               borderwidth=1, undo=True)
        self.t_msg.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        # ---- attachment
        ab = tk.Frame(left, bg=CARD)
        ab.pack(anchor="w", fill="x", padx=12, pady=(0, 4))
        tk.Button(ab, text="Attach photo / video / file…", relief="flat",
                  cursor="hand2", font=F(10),
                  command=self._on_attach).pack(side="left")
        self.b_unattach = tk.Button(ab, text="Remove", relief="flat",
                                    cursor="hand2", font=F(10), fg=BAD,
                                    command=self._on_unattach)
        self.b_unattach.pack(side="left", padx=6)
        self.lbl_attach = tk.Label(ab, text="", bg=CARD, fg=MUTED, font=F(9),
                                   wraplength=280, justify="left")
        self.lbl_attach.pack(side="left", padx=6)

        self.lbl_variants = tk.Label(left, text="", bg=CARD, fg=MUTED, font=F(9))
        self.lbl_variants.pack(anchor="w", padx=12, pady=(0, 10))

        # ---- recipients
        right = self._card(top, "2.  Who gets it  (one per line)")
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))
        tk.Label(right, text="@username  or  +353871234567       add a name "
                             "after a comma:   @ali_khan, Ali", bg=CARD,
                 fg=MUTED, justify="left", font=F(9)).pack(anchor="w", padx=12,
                                                           pady=(2, 6))
        self.t_rcpt = scrolledtext.ScrolledText(right, height=15, wrap="none",
                                                font=F(11), relief="solid",
                                                borderwidth=1, undo=True)
        self.t_rcpt.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        rb = tk.Frame(right, bg=CARD)
        rb.pack(anchor="w", padx=12, pady=(0, 10))
        tk.Button(rb, text="Add everyone from a group…", relief="flat",
                  cursor="hand2", font=F(10),
                  command=self._on_import_group).pack(side="left")
        tk.Button(rb, text="Load a file…", relief="flat", cursor="hand2",
                  font=F(10), command=self._on_load_file).pack(side="left",
                                                               padx=6)
        self.lbl_count = tk.Label(rb, text="", bg=CARD, fg=MUTED, font=F(9))
        self.lbl_count.pack(side="left", padx=8)

        for w in (self.t_msg, self.t_rcpt):
            w.bind("<KeyRelease>", lambda e: self._refresh_counts())

        # ---- send
        act = self._card(self.main, "3.  Send")
        act.pack(fill="x", pady=(14, 0))
        bar = tk.Frame(act, bg=CARD)
        bar.pack(fill="x", padx=12, pady=10)
        self.b_check = tk.Button(bar, text="Check first (sends nothing)",
                                 font=F(11), relief="flat", cursor="hand2",
                                 command=self._on_check)
        self.b_check.pack(side="left")
        self.b_send = tk.Button(bar, text="START SENDING", bg=GOOD, fg="white",
                                font=F(13, True), relief="flat", cursor="hand2",
                                padx=18, command=self._on_send)
        self.b_send.pack(side="left", padx=10)
        self.b_stop = tk.Button(bar, text="STOP", bg=BAD, fg="white",
                                font=F(12, True), relief="flat", cursor="hand2",
                                state="disabled", command=self._on_stop)
        self.b_stop.pack(side="left")
        tk.Button(bar, text="Send again to everyone…", relief="flat",
                  cursor="hand2", font=F(10),
                  command=self._on_new_campaign).pack(side="right")
        tk.Button(bar, text="Speed & limits", relief="flat", cursor="hand2",
                  font=F(10), command=self._on_settings).pack(side="right",
                                                              padx=8)
        tk.Button(bar, text="Account health", relief="flat", cursor="hand2",
                  font=F(10), command=self._on_health).pack(side="right", padx=8)

        self.pbar = ttk.Progressbar(act, mode="determinate")
        self.pbar.pack(fill="x", padx=12)
        self.lbl_status = tk.Label(act, text="Ready.", bg=CARD, fg=MUTED,
                                   anchor="w", font=F(11))
        self.lbl_status.pack(fill="x", padx=12, pady=(4, 10))

        logc = self._card(self.main, "Activity")
        logc.pack(fill="both", expand=True, pady=(14, 0))
        self.t_log = scrolledtext.ScrolledText(logc, height=8, wrap="word",
                                               font=(MONO, 10), state="disabled",
                                               relief="flat")
        self.t_log.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        for tag, col in (("good", GOOD), ("bad", BAD), ("warn", WARN),
                         ("info", "#1b1f24")):
            self.t_log.tag_config(tag, foreground=col)

    def show_main(self):
        self.setup.pack_forget()
        self.main.pack(fill="both", expand=True)
        self._reload_project_list()
        self._load_current_project()
        k, n = S.sent_today(self.state)
        ceil = S.warmup_ceiling(self.state, self.cfg)
        self.log(f"Connected. Today so far: {k} to people you know, {n} to new "
                 f"people.", "info")
        if ceil is not None:
            self.log(f"You are still in the first-week warm-up: up to {ceil} "
                     f"messages today. It rises every day, then there is no "
                     f"limit for your own contacts.", "info")
        else:
            self.log(f"No daily limit for people you already know. New people "
                     f"are capped at {self.cfg['daily_cap_new']} per day — that "
                     f"is the group Telegram bans accounts over.", "info")

    # ---------------------------------------------------------------- projects

    def _reload_project_list(self):
        names = S.project_names(self.projects)
        self.cb_project.config(values=names)
        if self.current not in names:
            self.current = names[0]
        self.cb_project.set(self.current)

    def _load_current_project(self):
        p = S.get_project(self.projects, self.current)
        self.t_msg.delete("1.0", "end")
        self.t_msg.insert("1.0", p.get("message", ""))
        self.t_rcpt.delete("1.0", "end")
        self.t_rcpt.insert("1.0", p.get("recipients", ""))
        self.attachment = p.get("attachment", "") or ""
        self._refresh_attach()
        self._refresh_counts()

    def _save_current_project(self):
        S.set_project(self.projects, self.current,
                      self.t_msg.get("1.0", "end").rstrip() + "\n",
                      self.t_rcpt.get("1.0", "end").rstrip() + "\n",
                      getattr(self, "attachment", ""))

    # ---------------------------------------------------------------- attach

    def _refresh_attach(self):
        a = getattr(self, "attachment", "")
        if a:
            missing = not os.path.exists(a)
            self.lbl_attach.config(
                text=("FILE IS MISSING: " if missing else
                      f"{S.attachment_kind(a)}: ") + os.path.basename(a),
                fg=BAD if missing else GOOD)
            self.b_unattach.pack(side="left", padx=6)
        else:
            self.lbl_attach.config(text="no file attached (text only)", fg=MUTED)
            self.b_unattach.pack_forget()

    def _on_attach(self):
        path = filedialog.askopenfilename(
            title="Choose a photo, video or file to send with the message",
            filetypes=[("Photos and videos",
                        "*.jpg *.jpeg *.png *.gif *.webp *.mp4 *.mov *.avi "
                        "*.mkv *.webm"),
                       ("All files", "*.*")])
        if not path:
            return
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > 2000:
            messagebox.showwarning("File too big",
                                   "Telegram's limit is 2 GB per file.")
            return
        self.attachment = path
        self._refresh_attach()
        self._save_current_project()
        self.log(f"Attached {S.attachment_kind(path)}: "
                 f"{os.path.basename(path)} ({size_mb:.1f} MB). Everyone on "
                 f"this list will get it with the message.", "good")
        if size_mb > 20:
            self.log(f"That is a big file ({size_mb:.0f} MB) — each send will "
                     f"take longer to upload.", "warn")

    def _on_unattach(self):
        self.attachment = ""
        self._refresh_attach()
        self._save_current_project()
        self.log("File removed — the message will be sent as text only.", "info")

    def _on_switch_project(self, _=None):
        if self.running:
            messagebox.showinfo("Still sending", "Please stop the current run "
                                                 "before switching project.")
            self.cb_project.set(self.current)
            return
        self._save_current_project()
        self.current = self.cb_project.get()
        self.projects["current"] = self.current
        S.save_projects(self.projects)
        self._load_current_project()
        ps = S.project_state(self.state, self.current)
        self.log(f"Switched to project \"{self.current}\" — "
                 f"{len(ps['sent'])} people already messaged in it.", "info")

    def _on_new_project(self):
        name = simpledialog.askstring("New project",
                                      "Name for this project?\n"
                                      "(e.g. Daily rates, VIP customers)",
                                      parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.projects["projects"]:
            messagebox.showwarning("Already exists",
                                   f"There is already a project called "
                                   f"\"{name}\".")
            return
        self._save_current_project()
        self.current = name
        S.set_project(self.projects, name, S.DEFAULT_MESSAGE, "")
        self._reload_project_list()
        self._load_current_project()
        self.log(f"Created project \"{name}\".", "good")

    def _on_rename_project(self):
        new = simpledialog.askstring("Rename project", "New name:",
                                     initialvalue=self.current, parent=self.root)
        if not new or not new.strip() or new.strip() == self.current:
            return
        new = new.strip()
        if new in self.projects["projects"]:
            messagebox.showwarning("Already exists", "That name is taken.")
            return
        self._save_current_project()
        self.projects["projects"][new] = self.projects["projects"].pop(
            self.current)
        if self.current in self.state["projects"]:
            self.state["projects"][new] = self.state["projects"].pop(
                self.current)
            S.save_state(self.state)
        self.current = new
        self.projects["current"] = new
        S.save_projects(self.projects)
        self._reload_project_list()
        self.log(f"Renamed to \"{new}\".", "good")

    def _on_delete_project(self):
        if len(self.projects["projects"]) == 1:
            messagebox.showinfo("Cannot delete",
                                "This is your only project. Create another "
                                "one first.")
            return
        if not messagebox.askyesno("Delete project",
                                   f"Delete \"{self.current}\" — its message "
                                   f"and its list of people?\n\nThis cannot be "
                                   f"undone."):
            return
        gone = self.current
        self.projects = S.delete_project(self.projects, gone)
        self.state["projects"].pop(gone, None)
        S.save_state(self.state)
        self.current = self.projects["current"]
        self._reload_project_list()
        self._load_current_project()
        self.log(f"Deleted \"{gone}\".", "warn")

    # ---------------------------------------------------------------- import

    def _on_import_group(self):
        self.log("Reading your groups…", "info")

        def done(groups):
            if not groups:
                messagebox.showinfo("No groups",
                                    "This account is not in any groups.")
                return
            self._pick_group(groups)

        self.backend.submit(self.backend.sender.list_groups(), done,
                            lambda e: self.log(f"Could not read groups: {e}",
                                               "bad"))

    def _pick_group(self, groups):
        win = tk.Toplevel(self.root)
        win.title("Add everyone from a group")
        win.configure(bg=BG)
        win.geometry("520x460")
        win.transient(self.root)
        tk.Label(win, text="Pick a group. Everyone in it will be added to your "
                           "list.", bg=BG, font=F(11), wraplength=480,
                 justify="left").pack(anchor="w", padx=14, pady=(14, 4))
        tk.Label(win, text="Note: people in a group who are not already your "
                           "contacts count as NEW people, and only a few of "
                           "those are sent per day on purpose. That limit is "
                           "what stops Telegram banning the account.",
                 bg=BG, fg=WARN, font=F(9), wraplength=480,
                 justify="left").pack(anchor="w", padx=14, pady=(0, 8))
        lb = tk.Listbox(win, font=F(11))
        lb.pack(fill="both", expand=True, padx=14)
        for g in groups:
            lb.insert("end", g["title"])

        def go():
            sel = lb.curselection()
            if not sel:
                return
            g = groups[sel[0]]
            win.destroy()
            self.log(f"Reading members of \"{g['title']}\"… this can take a "
                     f"minute for a big group.", "info")

            def done(people):
                if not people:
                    messagebox.showinfo("Nobody found",
                                        "Could not read the member list. Some "
                                        "groups hide it unless you are an "
                                        "admin.")
                    return
                existing = {t.lower().lstrip("@") for t, _ in
                            S.parse_recipients(self.t_rcpt.get("1.0", "end"))}
                added = 0
                lines = []
                for t, n in people:
                    if t.lower().lstrip("@") in existing:
                        continue
                    lines.append(f"{t}, {n}" if n else t)
                    added += 1
                if lines:
                    cur = self.t_rcpt.get("1.0", "end").rstrip()
                    self.t_rcpt.delete("1.0", "end")
                    self.t_rcpt.insert("1.0", (cur + "\n" if cur else "")
                                       + "\n".join(lines) + "\n")
                self._refresh_counts()
                self._save_current_project()
                self.log(f"Added {added} people from \"{g['title']}\" "
                         f"({len(people) - added} were already on the list).",
                         "good")

            self.backend.submit(self.backend.sender.group_members(g["id"]), done,
                                lambda e: self.log(f"Could not read members: "
                                                   f"{e}", "bad"))

        tk.Button(win, text="Add these people", bg=GOOD, fg="white",
                  font=F(11, True), relief="flat", cursor="hand2",
                  command=go).pack(pady=12)

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
        self.t_rcpt.delete("1.0", "end")
        self.t_rcpt.insert("1.0", text)
        self._refresh_counts()
        self._save_current_project()
        self.log(f"Loaded {len(S.parse_recipients(text))} people from "
                 f"{os.path.basename(path)}.", "info")

    # ---------------------------------------------------------------- settings

    def _on_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Speed & limits")
        win.configure(bg=BG)
        win.geometry("620x520")
        win.transient(self.root)

        tk.Label(win, text="Speed", bg=BG, font=F(13, True)).pack(anchor="w",
                                                                  padx=16,
                                                                  pady=(16, 2))
        tk.Label(win, text="Slower is safer. The pacing is the main thing "
                           "protecting the account.", bg=BG, fg=MUTED,
                 font=F(9), wraplength=560, justify="left").pack(anchor="w",
                                                                 padx=16)
        speed = tk.StringVar(value=self.cfg.get("speed", "safest"))
        for key, p in S.SPEED_PRESETS.items():
            tk.Radiobutton(win, text=p["label"], variable=speed, value=key,
                           bg=BG, font=F(11), anchor="w",
                           fg=BAD if key == "fast" else "#1b1f24").pack(
                anchor="w", padx=24)

        tk.Label(win, text="Daily limits", bg=BG, font=F(13, True)).pack(
            anchor="w", padx=16, pady=(16, 2))
        unlimited = tk.BooleanVar(value=self.cfg.get("unlimited_known", True))
        tk.Checkbutton(win, variable=unlimited, bg=BG, font=F(11), anchor="w",
                       text="No daily limit for people I already know "
                            "(recommended)").pack(anchor="w", padx=24)
        tk.Label(win, text="Your saved contacts and anyone you already have a "
                           "chat with. Telegram's spam limits are aimed at "
                           "strangers, not these people. It will simply send "
                           "to as many as fit in the day and carry on "
                           "tomorrow.", bg=BG, fg=MUTED, font=F(9),
                 wraplength=540, justify="left").pack(anchor="w", padx=44)

        f2 = tk.Frame(win, bg=BG)
        f2.pack(anchor="w", padx=24, pady=(10, 0))
        tk.Label(f2, text="Max NEW people per day:", bg=BG,
                 font=F(11)).pack(side="left")
        e_new = tk.Entry(f2, width=6, font=F(11))
        e_new.pack(side="left", padx=8)
        e_new.insert(0, str(self.cfg.get("daily_cap_new", 15)))
        tk.Label(win, text="People you have never messaged. THIS is the number "
                           "that gets accounts banned. 15 or less is sensible; "
                           "above 40 is asking for trouble.", bg=BG, fg=WARN,
                 font=F(9), wraplength=540, justify="left").pack(anchor="w",
                                                                 padx=44)

        f3 = tk.Frame(win, bg=BG)
        f3.pack(anchor="w", padx=24, pady=(12, 0))
        tk.Label(f3, text="Sending hours:", bg=BG, font=F(11)).pack(side="left")
        e_from = tk.Entry(f3, width=7, font=F(11))
        e_from.pack(side="left", padx=6)
        e_from.insert(0, self.cfg.get("active_hours_start", "09:00"))
        tk.Label(f3, text="to", bg=BG, font=F(11)).pack(side="left")
        e_to = tk.Entry(f3, width=7, font=F(11))
        e_to.pack(side="left", padx=6)
        e_to.insert(0, self.cfg.get("active_hours_end", "21:30"))

        warm = tk.BooleanVar(value=self.cfg.get("use_warmup", True))
        tk.Checkbutton(win, variable=warm, bg=BG, font=F(11),
                       text="Use the first-week warm-up (strongly recommended)"
                       ).pack(anchor="w", padx=24, pady=(12, 0))

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
                    f"{n} new people per day is well above what accounts "
                    f"survive long-term. Telegram restricts accounts for "
                    f"exactly this.\n\nSet it anyway?"):
                return
            if speed.get() == "fast" and not messagebox.askyesno(
                    "Fast is risky",
                    "Fast mode sends every 8–30 seconds. That is the pattern "
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

        tk.Button(win, text="Save", bg=GOOD, fg="white", font=F(12, True),
                  relief="flat", cursor="hand2", command=save).pack(pady=18)

    # ---------------------------------------------------------------- helpers

    def log(self, msg, level="info"):
        from datetime import datetime as _dt
        self.t_log.config(state="normal")
        self.t_log.insert("end", f"{_dt.now():%H:%M:%S}  {msg}\n", level)
        self.t_log.see("end")
        self.t_log.config(state="disabled")

    def _refresh_counts(self):
        v = S.parse_message_variants(self.t_msg.get("1.0", "end"))
        r = S.parse_recipients(self.t_rcpt.get("1.0", "end"))
        self.lbl_count.config(text=f"{len(r)} people")
        if not v:
            self.lbl_variants.config(text="No message written yet.", fg=BAD)
        elif len(v) == 1:
            self.lbl_variants.config(text="Ready — the same message goes to "
                                          "everyone.", fg=GOOD)
        else:
            self.lbl_variants.config(text=f"{len(v)} versions — it will mix "
                                          f"between them.", fg=GOOD)

    def _inputs(self):
        v = S.parse_message_variants(self.t_msg.get("1.0", "end"))
        r = S.parse_recipients(self.t_rcpt.get("1.0", "end"))
        if not v:
            messagebox.showwarning("No message", "Type your message first.")
            return None
        if not r:
            messagebox.showwarning("Nobody to send to",
                                   "Add at least one username or phone number.")
            return None
        return v, r

    # ---------------------------------------------------------------- actions

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
            text=f"{q} will go out now  •  about {plan['hours']:.1f} hours  •  "
                 f"{plan['known_queued']} you know, {plan['new_queued']} new  •  "
                 f"{plan['left_for_later']} left for later")
        self.log(f"Plan for \"{self.current}\": {q} messages now "
                 f"({plan['known_queued']} people you know, "
                 f"{plan['new_queued']} new).", "info")
        if plan["already_done"]:
            self.log(f"{plan['already_done']} already received this message — "
                     f"they will be skipped. Use \"Send again to everyone\" if "
                     f"you want them included.", "info")
        if plan["left_for_later"]:
            why = []
            if plan["new_total"] > plan["new_queued"]:
                why.append(f"{plan['new_total'] - plan['new_queued']} new people "
                           f"over today's limit of {self.cfg['daily_cap_new']}")
            if plan["warmup_ceiling"] is not None:
                why.append(f"first-week warm-up ceiling of "
                           f"{plan['warmup_ceiling']} a day")
            self.log(f"{plan['left_for_later']} people are left for the next "
                     f"days ({'; '.join(why) or 'daily limits'}). Nothing is "
                     f"lost — open this tomorrow and it carries on.", "warn")
        att = getattr(self, "attachment", "")
        if att:
            self.log(f"Attached {S.attachment_kind(att)}: "
                     f"{os.path.basename(att)}"
                     + ("  — WARNING: this file is missing!"
                        if not os.path.exists(att) else ""),
                     "bad" if not os.path.exists(att) else "info")
        if q:
            t, n, _ = plan["queue"][0]
            self.log("This is what one person will receive:", "info")
            self.log("    " + S.render(variants, n or "Ali").replace(
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
                    "Everyone on this list already got this message, or today's "
                    "limit for new people is used up.\n\nOpen this again "
                    "tomorrow and it carries on by itself.\n\nIf you want to "
                    "message the same people again, use \"Send again to "
                    "everyone\".")
                return
            att = getattr(self, "attachment", "")
            if att and not os.path.exists(att):
                self._reset_buttons()
                messagebox.showerror(
                    "The attached file is gone",
                    f"This project has a file attached, but it is no longer "
                    f"here:\n\n{att}\n\nAttach it again, or click Remove to "
                    f"send text only.")
                return
            extra = (f"\nWith {S.attachment_kind(att)}: "
                     f"{os.path.basename(att)}" if att else "")
            if not messagebox.askyesno(
                    "Ready to send",
                    f"Project: {self.current}\n\nSend to {q} people?\n"
                    f"({plan['known_queued']} you already know, "
                    f"{plan['new_queued']} new){extra}\n\n"
                    f"About {plan['hours']:.1f} hours — it sends slowly, like a "
                    f"person typing.\n\nLeave this window open. You can press "
                    f"STOP any time and nobody gets it twice."):
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
                self.emit("log", message="Checking your account health with "
                                         "@SpamBot…", level="info")
                ok, text = await self.backend.sender.spambot_status()
                self.emit("log", message="@SpamBot: "
                          + text.replace("\n", " ")[:220],
                          level="good" if ok else "bad")
                if not ok:
                    self.emit("blocked", text=text)
                    return None
            return await self.backend.sender.run(
                self.plan, variants, self.state, self.current,
                self.backend.stop_flag, getattr(self, "attachment", ""))

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
        self.log("Stopping. Progress is saved — nobody gets it twice.", "warn")

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
                                 "follow its buttons to appeal.\n\n") + text[:600])

        self.backend.submit(self.backend.sender.spambot_status(), done,
                            lambda e: self.log(f"Health check failed: {e}",
                                               "bad"))

    def _on_new_campaign(self):
        ps = S.project_state(self.state, self.current)
        if not messagebox.askyesno(
                "Send again to everyone?",
                f"Project \"{self.current}\" has already been sent to "
                f"{len(ps['sent'])} people.\n\nThis forgets that record so "
                f"everyone on the list can be messaged again with your new "
                f"message.\n\nContinue?"):
            return
        self.state = S.reset_project_state(self.state, self.current)
        self.log(f"\"{self.current}\" reset — everyone on the list can receive "
                 f"the next message.", "good")
        self.lbl_status.config(text="Ready to send to everyone again.")

    # ---------------------------------------------------------------- pump

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
                             + (f"  •  {d['failed']} could not be reached"
                                if d["failed"] else ""))
                elif kind == "waiting":
                    s = int(d["seconds"])
                    wk = d.get("wait_kind")
                    word = {"break": "On a break — back in",
                            "night": "Sleeping until morning —"}.get(
                                wk, "Next message in")
                    cur = self.lbl_status.cget("text").split("  |  ")[0]
                    self.lbl_status.config(
                        text=f"{cur}  |  {word} {s//3600}h {s%3600//60}m"
                        if wk == "night" else
                        f"{cur}  |  {word} {s//60}m {s%60:02d}s")
                elif kind == "blocked":
                    self.running = False
                    self._reset_buttons()
                    self.lbl_status.config(text="Blocked — account is limited.")
                    messagebox.showerror(
                        "Cannot send today",
                        "Telegram has limited this account, so nothing was "
                        "sent.\n\nOpen @SpamBot in Telegram, press its buttons "
                        "and appeal. Limits are usually lifted in 24–72 "
                        "hours.\n\n" + d["text"][:500])
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
            if hasattr(self, "t_msg") and self.main.winfo_exists():
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
