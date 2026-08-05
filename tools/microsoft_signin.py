#!/usr/bin/env python3
"""Sign in to a Microsoft account and send one real test email.

    ./venv/bin/python tools/microsoft_signin.py designertommy@live.com

Proves the whole round trip before the client ever sees it. Nothing is saved
and no project data is touched.
"""

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("TELEGRAM_SENDER_DATA", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sender as S      # noqa: E402
import mailer as M      # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    addr = sys.argv[1]
    to = sys.argv[2] if len(sys.argv) > 2 else addr

    print(f"Signing in as {addr}\n")
    try:
        started = M.ms_start_signin({})
    except M.SignInError as e:
        print("Could not start:", e)
        return 1

    print("=" * 58)
    print(f"  1. Open   {started.get('verification_uri')}")
    print(f"  2. Enter this code:   {started['user_code']}")
    print(f"  3. Sign in as {addr} and press Accept")
    print("=" * 58)
    print("\nWaiting… (Ctrl+C to give up)\n")

    try:
        refresh = M.ms_finish_signin(started, {}, None,
                                     lambda s: None)
    except M.SignInError as e:
        print("Sign-in did not finish:\n")
        print(e)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.")
        return 1

    print("SIGNED IN. Microsoft gave us a token, no password involved.\n")

    guess = M.guess_smtp(addr) or ("smtp-mail.outlook.com", 587, True)
    cfg = dict(S.DEFAULT_CONFIG, email_address=addr, email_auth="microsoft",
               email_oauth_refresh_token=refresh, email_from_name="Test",
               email_smtp_host=guess[0], email_smtp_port=guess[1],
               email_use_tls=guess[2])

    print(f"Sending a test email to {to} …\n")
    es = M.EmailSender(cfg, lambda k, **d: None)
    ok, message = asyncio.run(es.test_connection(to))
    print(("WORKED\n\n" if ok else "DID NOT WORK\n\n") + message)

    if ok:
        print("\nRefresh token (paste into the client's config.json if you "
              "want it set up for him):\n")
        print(refresh)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
