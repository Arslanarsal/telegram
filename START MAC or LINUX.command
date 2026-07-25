#!/bin/bash
cd "$(dirname "$0")" || exit 1

echo "=========================================="
echo "   Telegram Sender - starting up"
echo "=========================================="
echo

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  echo "Python is not installed on this computer."
  echo
  echo "Please install it once from https://www.python.org/downloads/"
  echo "then run this file again."
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ ! -x "venv/bin/python" ]; then
  echo "First time setup - this takes about a minute. Please wait..."
  "$PY" -m venv venv || {
    echo "Could not create the environment."
    echo "On Ubuntu/Debian you may need:  sudo apt install python3-venv python3-tk"
    read -r -p "Press Enter to close..."
    exit 1
  }
  ./venv/bin/python -m pip install --upgrade pip --quiet
  ./venv/bin/python -m pip install --quiet -r requirements.txt || {
    echo "Could not download the needed files. Check your internet and try again."
    read -r -p "Press Enter to close..."
    exit 1
  }
  echo "Setup finished."
  echo
fi

if ! ./venv/bin/python -c "import tkinter" >/dev/null 2>&1; then
  echo "This computer is missing the window library (tkinter)."
  echo "On Ubuntu/Debian run once:   sudo apt install python3-tk"
  echo "On macOS: reinstall Python from python.org (it includes it)."
  read -r -p "Press Enter to close..."
  exit 1
fi

# Quietly grab the latest version if this folder came from git.
# Never blocks startup: no git or no internet just means we carry on.
if [ -d ".git" ] && command -v git >/dev/null 2>&1; then
  echo "Checking for updates..."
  if git pull --quiet 2>/dev/null; then
    ./venv/bin/python -m pip install --quiet -r requirements.txt 2>/dev/null
  fi
fi

echo "Opening the window..."
./venv/bin/python app.py || {
  echo
  echo "Something went wrong. Please send this text to your developer."
  read -r -p "Press Enter to close..."
}
