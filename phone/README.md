# Install the watch app through the phone

The phone sits between the PC and the watch:

```
PC ──Tailscale──▶ phone:15555/15556 ──Wi-Fi/hotspot──▶ watch ADB
```

The phone only forwards TCP. The real `adb` still runs on the PC, so pairing,
installing and updating work as usual.

## Prerequisites

- The watch and phone are on the same Wi-Fi, **or** the watch is on the
  phone's hotspot.
- Tailscale is running on the phone and the PC.
- The watch has **Developer options → ADB debugging** and **Wireless
  debugging** enabled.

## Phone setup (Termux)

Install Termux from F-Droid, then:

```sh
pkg update
pkg install python
termux-wake-lock
```

Copy `adb_relay.py` to the phone with Taildrop:

```sh
tailscale file cp phone/adb_relay.py marcuss-z-fold7:
```

Then in Termux:

```sh
termux-setup-storage
cp ~/storage/downloads/adb_relay.py ~
```

If Taildrop is not enabled, serve the file from the PC instead:

```sh
cd phone
python3 -m http.server 8765 --bind 100.77.42.78
```

and in Termux:

```sh
curl -O http://100.77.42.78:8765/adb_relay.py
```

## Find the watch ports

On the watch: **Settings → Developer options → Wireless debugging**.

- The main screen shows the **connection** IP and port.
- **Pair new device** shows a separate **pairing** port and a 6-digit code.

Keep the Pair new device screen open while pairing.

## Run the relay

In Termux (replace the phone's Tailscale IP, watch IP and ports):

```sh
python adb_relay.py \
  --watch 192.168.43.55 \
  --pair-port 41000 \
  --connect-port 37000 \
  --bind 100.81.149.78
```

Binding to the Tailscale IP keeps the relay off the local Wi-Fi. Use
`--bind 0.0.0.0` only if you need LAN access too.

## Install from the PC

```sh
desktop/install-watch-via-phone.sh \
  --phone 100.81.149.78 \
  --code 123456
```

The code is only needed the first time. If the watch's ports changed, stop the
relay, update `--pair-port`/`--connect-port`, and run it again.

Manual equivalent:

```sh
adb pair 100.81.149.78:15556 123456
adb connect 100.81.149.78:15555
adb -s 100.81.149.78:15555 install -r watch/build/wristle-0.1.0.apk
```

## Troubleshooting

| Symptom | Check |
|---|---|
| `cannot reach <watch>` in Termux | Watch and phone must share Wi-Fi/hotspot; watch Wi-Fi is on; no AP isolation. |
| `failed to connect` on PC | The relay is running, the Tailscale IP is right, and port 15555 is not blocked. |
| `device offline` | Watch screen off or Wireless debugging turned off; reconnect. |
| Pairing works, install says unauthorized | Accept the debugging prompt on the watch, then re-run. |
| Termux dies in the background | Run `termux-wake-lock` and disable battery optimization for Termux. |
| Ports keep changing | Re-read them on the watch after every Wireless debugging toggle/reboot. |

## Updating after the first install

**Desktop-only fixes** (Voxtype, listener, typing) need no watch install at
all. Edit or pull `desktop/wristle.py`, then restart the listener:

```sh
desktop/start.sh --voxtype
```

**Upstream watch APK updates** are signed with the same upstream key, so they
install over the existing app. Keep the relay running, re-read the watch's
current connection port, restart the relay with it, then:

```sh
desktop/install-watch-via-phone.sh --phone 100.81.149.78
```

No `--code` is needed unless the watch's ADB keys were cleared.

**Locally built watch fixes** use a different signing key. The first local
build must replace the upstream app:

```sh
desktop/install-watch-via-phone.sh \
  --phone 100.81.149.78 \
  --apk watch/build/wristle-<version>.apk \
  --uninstall
```

That wipes the app's data, so open Wristle and re-pair it once. Later local
builds signed with the same `~/.wristle/release.keystore` install with `-r` and
keep their data and pairing.

Each time Wireless debugging is toggled or the watch reboots, the watch's
connection port changes. Stop the Termux relay, restart it with the new
`--connect-port`, and run the install script again.
