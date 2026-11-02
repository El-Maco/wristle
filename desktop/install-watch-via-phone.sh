#!/usr/bin/env bash
# Install the Wristle APK on a watch through an Android phone running
# phone/adb_relay.py. Run the relay in Termux first.
set -euo pipefail

PHONE=""
CODE=""
APK="watch/build/wristle-0.1.0.apk"
PAIR_PORT=15556
CONNECT_PORT=15555
UNINSTALL=0
ADB="${ADB:-$HOME/.local/bin/adb}"

usage() {
  cat <<EOF
Usage: $0 --phone <tailscale-ip> [options]

Options:
  --phone IP          Phone's Tailscale IP (required)
  --code CODE         Watch pairing code (needed the first time)
  --apk FILE          APK to install (default: $APK)
  --pair-port PORT    Relay's pairing port on the phone (default: $PAIR_PORT)
  --connect-port PORT Relay's connect port on the phone (default: $CONNECT_PORT)
  --uninstall         Remove the existing app first (needed when the signing key changed)

Example:
  $0 --phone 100.81.149.78 --code 123456
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --phone) PHONE="$2"; shift 2 ;;
    --code) CODE="$2"; shift 2 ;;
    --apk) APK="$2"; shift 2 ;;
    --pair-port) PAIR_PORT="$2"; shift 2 ;;
    --connect-port) CONNECT_PORT="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$PHONE" ] || { usage >&2; exit 2; }
[ -f "$APK" ] || { echo "APK not found: $APK" >&2; exit 1; }
"$ADB" version >/dev/null 2>&1 || { echo "adb not found at $ADB (set ADB=...)" >&2; exit 1; }

if [ -n "$CODE" ]; then
  "$ADB" pair "$PHONE:$PAIR_PORT" "$CODE"
fi

"$ADB" connect "$PHONE:$CONNECT_PORT"
if [ "$UNINSTALL" -eq 1 ]; then
  "$ADB" -s "$PHONE:$CONNECT_PORT" uninstall io.github.unknownprocess.wristle || true
fi
"$ADB" -s "$PHONE:$CONNECT_PORT" install -r "$APK"
echo
"$ADB" devices -l | grep -F "$PHONE:$CONNECT_PORT" || true
echo "Installed. Open Wristle on the watch and approve it on the PC."
