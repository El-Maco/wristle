#!/usr/bin/env python3
"""
Forward the watch's ADB ports through the phone.

Run this in Termux on the Android phone, with the watch on the same Wi-Fi or
on the phone's hotspot:

    python adb_relay.py --watch 192.168.43.55 --pair-port 41000 --connect-port 37000

It listens on the phone (default ports 15556/15555) and forwards to the watch.
From the PC, over Tailscale:

    adb pair <phone-tailscale-ip>:15556 <pairing-code>
    adb connect <phone-tailscale-ip>:15555
    adb -s <phone-tailscale-ip>:15555 install -r watch/build/wristle-0.1.0.apk

Only the PC on your tailnet should reach these ports. Bind to the phone's
Tailscale IP with --bind if you do not want them exposed on the local Wi-Fi.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time

BUFSIZE = 65536


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def pump(src: socket.socket, dst: socket.socket):
    """Copy one direction until EOF, then half-close the other side."""
    try:
        while True:
            data = src.recv(BUFSIZE)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket, peer: str, target: tuple[str, int], label: str):
    try:
        upstream = socket.create_connection(target, timeout=10)
    except OSError as e:
        log(f"! {label}: cannot reach {target[0]}:{target[1]} ({e})")
        client.close()
        return

    log(f"→ {label}: {peer} → {target[0]}:{target[1]}")
    threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
    pump(upstream, client)
    for s in (client, upstream):
        try:
            s.close()
        except OSError:
            pass
    log(f"← {label}: closed {peer}")


def serve(bind: str, listen_port: int, target: tuple[str, int], label: str):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, listen_port))
    srv.listen(16)
    log(f"{label}: {bind}:{listen_port} → {target[0]}:{target[1]}")

    while True:
        try:
            client, addr = srv.accept()
        except OSError as e:
            log(f"! {label}: accept failed ({e})")
            time.sleep(0.5)
            continue
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(
            target=handle, args=(client, addr[0], target, label), daemon=True
        ).start()


def main():
    p = argparse.ArgumentParser(description="Relay the watch's wireless-debugging ports through the phone.")
    p.add_argument("--watch", required=True, help="watch IP on the phone's Wi-Fi/hotspot")
    p.add_argument("--connect-port", type=int, required=True,
                   help="watch's Wireless debugging connection port")
    p.add_argument("--pair-port", type=int,
                   help="watch's Pair new device port (optional; only needed for first pairing)")
    p.add_argument("--bind", default="0.0.0.0",
                   help="phone address to listen on (default: 0.0.0.0; use the Tailscale IP to restrict)")
    p.add_argument("--listen-connect", type=int, default=15555,
                   help="phone port for adb connect (default 15555)")
    p.add_argument("--listen-pair", type=int, default=15556,
                   help="phone port for adb pair (default 15556)")
    args = p.parse_args()

    watch = (args.watch, args.connect_port)
    threads = [
        threading.Thread(
            target=serve,
            args=(args.bind, args.listen_connect, watch, "connect"),
            daemon=True,
        )
    ]
    if args.pair_port:
        threads.append(threading.Thread(
            target=serve,
            args=(args.bind, args.listen_pair, (args.watch, args.pair_port), "pair"),
            daemon=True,
        ))
    for t in threads:
        t.start()

    print()
    print("On the PC:")
    if args.pair_port:
        print(f"  adb pair <phone-ip>:{args.listen_pair} <pairing-code>")
    print(f"  adb connect <phone-ip>:{args.listen_connect}")
    print(f"  adb -s <phone-ip>:{args.listen_connect} install -r watch/build/wristle-0.1.0.apk")
    print()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nBye")


if __name__ == "__main__":
    main()
