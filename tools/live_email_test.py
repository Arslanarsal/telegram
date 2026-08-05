#!/usr/bin/env python3
"""Send one real email, to prove the settings work before the client tries.

    ./venv/bin/python tools/live_email_test.py you@gmail.com xxxxxxxxxxxxxxxx

Optionally add a different address to send TO as a third argument.
Nothing is saved and no project data is touched.
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
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    addr, pwd = sys.argv[1], sys.argv[2].replace(" ", "")
    to = sys.argv[3] if len(sys.argv) > 3 else addr

    guess = M.guess_smtp(addr)
    if guess:
        print(f"Provider recognised: {guess[0]} port {guess[1]}")
    else:
        print("Provider not recognised — you will have to type the mail "
              "server by hand in Email settings.")
        return 1
    if M.needs_app_password(addr):
        print("This provider needs an App Password, not the account password.")
    print(f"Sending a test to {to} …\n")

    cfg = dict(S.DEFAULT_CONFIG, email_address=addr, email_password=pwd,
               email_from_name="Test", email_smtp_host=guess[0],
               email_smtp_port=guess[1], email_use_tls=guess[2])

    es = M.EmailSender(cfg, lambda k, **d: None)
    ok, message = asyncio.run(es.test_connection(to))
    print(("WORKED\n\n" if ok else "DID NOT WORK\n\n") + message)
    print("\n(This is the exact wording the client would see.)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
