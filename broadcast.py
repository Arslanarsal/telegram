#!/usr/bin/env python3
"""
Command-line version. Most people should use the window: python3 app.py

    python3 broadcast.py --list                 # show projects
    python3 broadcast.py --project "Daily" --check
    python3 broadcast.py --project "Daily" --yes
"""

import argparse
import asyncio
import threading

import sender as S


def emit(kind, **d):
    if kind == "log":
        print({"good": "  [ok] ", "bad": "  [!!] ",
               "warn": "  [..] "}.get(d.get("level"), "  ") + d["message"],
              flush=True)
    elif kind == "finished":
        print(f"\nDone. sent={d['sent']} failed={d['failed']} ({d['reason']})")


async def amain(args):
    cfg = S.load_config()
    projects = S.load_projects()

    if args.list:
        state = S.load_state()
        for n in S.project_names(projects):
            p = S.get_project(projects, n)
            ps = S.project_state(state, n)
            print(f"  {n:30} {len(S.parse_recipients(p['recipients'])):>5} people"
                  f"   {len(ps['sent']):>5} already sent")
        return

    if not S.config_ready(cfg):
        raise SystemExit("Not set up yet. Run the window once to connect:\n"
                         "    python3 app.py")

    name = args.project or projects.get("current") or S.project_names(projects)[0]
    if name not in projects["projects"]:
        raise SystemExit(f"No project called {name!r}. Use --list to see them.")
    proj = S.get_project(projects, name)

    variants = S.parse_message_variants(proj["message"])
    recipients = S.parse_recipients(proj["recipients"])
    if not variants:
        raise SystemExit(f"Project {name!r} has no message.")
    if not recipients:
        raise SystemExit(f"Project {name!r} has nobody in it.")

    state = S.load_state()
    snd = S.Sender(cfg, emit)
    res = await snd.connect()
    if res["state"] != "ready":
        raise SystemExit("Not logged in yet. Run: python3 app.py")
    print(f"Sending as: {res['name']} (@{res.get('username') or 'no username'})")
    print(f"Project: {name}")

    plan = await snd.build_plan(recipients, state, name)
    print(f"\n  on list ............... {len(recipients)}")
    print(f"  already done .......... {plan['already_done']}")
    print(f"  today so far .......... {plan['known_today']} known, "
          f"{plan['new_today']} new")
    print(f"  queued now ............ {len(plan['queue'])} "
          f"({plan['known_queued']} known, {plan['new_queued']} new)")
    print(f"  left for later ........ {plan['left_for_later']}")
    print(f"  warm-up ceiling ....... {plan['warmup_ceiling'] or 'none'}")
    print(f"  estimated time ........ {plan['hours']:.1f} hours\n")

    if args.check:
        for t, n, cls in plan["queue"][:3]:
            print(f"TO {t} [{cls}]:\n{S.render(variants, n or 'Ali')}\n{'-'*50}")
        await snd.disconnect()
        return

    if not plan["queue"]:
        print("Nothing to send right now.")
        await snd.disconnect()
        return

    if not args.yes and input(f"Send to {len(plan['queue'])} people? type YES: "
                              ).strip() != "YES":
        print("Cancelled.")
        await snd.disconnect()
        return

    if cfg.get("check_spambot_before_run", True):
        ok, text = await snd.spambot_status()
        print("  [@SpamBot] " + text.replace("\n", " ")[:200])
        if not ok:
            print("\nABORTED: this account is limited. Do not send today.")
            await snd.disconnect()
            return

    await snd.run(plan, variants, state, name, threading.Event())
    await snd.disconnect()


def main():
    p = argparse.ArgumentParser(description="Human-paced Telegram sender (CLI)")
    p.add_argument("--project", help="which project to send")
    p.add_argument("--list", action="store_true", help="list projects and exit")
    p.add_argument("--check", action="store_true", help="show plan, send nothing")
    p.add_argument("--dry-run", dest="check", action="store_true")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    args = p.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nStopped. Progress saved — rerun to carry on.")


if __name__ == "__main__":
    main()
