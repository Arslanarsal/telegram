#!/bin/bash
cd "$(dirname "$0")" || exit 1

echo "=========================================="
echo "   Getting the latest version"
echo "=========================================="
echo

if [ ! -d ".git" ]; then
  echo "This folder was not set up for updates."
  echo "Please ask your developer for a fresh copy."
  read -r -p "Press Enter to close..."
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "Git is not installed."
  echo "On Ubuntu/Debian:  sudo apt install git"
  echo "On macOS:          xcode-select --install"
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Your message, your lists and your login are NOT touched by this."
echo

if ! git pull; then
  echo
  echo "Could not get the update. Check your internet connection."
  read -r -p "Press Enter to close..."
  exit 1
fi

echo
echo "Checking for new requirements..."
[ -x "venv/bin/python" ] && ./venv/bin/python -m pip install --quiet -r requirements.txt

echo
echo "=========================================="
echo "   Up to date. Start the sender as usual."
echo "=========================================="
read -r -p "Press Enter to close..."
