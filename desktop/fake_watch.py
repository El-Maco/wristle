#!/usr/bin/env python3
"""
Pretend to be the watch: streams a WAV file to the listener in real time.
Handy for testing the PC side without the watch.

    python fake_watch.py speech.wav            # find the listener on the LAN
    python fake_watch.py speech.wav --host 127.0.0.1

Each run pairs as a new watch. After pairing it prints --id/--key to reuse.
"""

import argparse
import hashlib
import hmac
import json
import socket
import struct
import time
import uuid
import wave

import numpy as np

TCP_PORT = 47820
UDP_PORT = 47821


def discover(timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    for target in ("255.255.255.255", "127.0.0.1"):
        try:
            s.sendto(b"WRISTLE?", (target, UDP_PORT))
        except OSError:
            pass
    data, addr = s.recvfrom(256)
    _, port, name = data.decode().split(" ", 2)
    return addr[0], int(port), name


def load_16k_mono(path):
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit("WAV must be 16-bit PCM")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        t = np.arange(0, len(x), rate / 16000)
        x = np.interp(t, np.arange(len(x)), x)
    return x.astype("<i2").tobytes()


def send(sock, ftype, payload=b""):
    sock.sendall(ftype + struct.pack(">I", len(payload)) + payload)


def recv_frame(sock):
    hdr = b""
    while len(hdr) < 5:
        c = sock.recv(5 - len(hdr))
        if not c:
            raise ConnectionError("closed")
        hdr += c
    n = struct.unpack(">I", hdr[1:])[0]
    body = b""
    while len(body) < n:
        c = sock.recv(n - len(body))
        if not c:
            raise ConnectionError("closed")
        body += c
    return hdr[:1], body


def auth_proof(key_hex, nonce, device_id):
    msg = f"wristle-auth|{nonce}|{device_id}".encode()
    return hmac.new(bytes.fromhex(key_hex), msg, hashlib.sha256).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("wav")
    p.add_argument("--host")
    p.add_argument("--port", type=int, default=TCP_PORT)
    p.add_argument("--id", default="fake-" + uuid.uuid4().hex[:12])
    p.add_argument("--key", default="", help="pairing key printed by an earlier run with the same --id")
    p.add_argument("--cancel", action="store_true", help="cancel instead of finishing")
    a = p.parse_args()

    host, port = a.host, a.port
    if not host:
        host, port, name = discover()
        print(f"found {name} at {host}:{port}")
    pcm = load_16k_mono(a.wav)

    sock = socket.create_connection((host, port))
    send(sock, b"H", json.dumps({"id": a.id, "name": "Fake watch", "v": 2}).encode())
    while True:
        t, body = recv_frame(sock)
        if t == b"O":
            st = json.loads(body)
            print("status:", st)
            if st["state"] == "auth":
                proof = auth_proof(a.key, st["nonce"], a.id) if a.key else ""
                send(sock, b"U", proof.encode())
            if st["state"] == "ok":
                if "key" in st:
                    print(f"paired; reuse with: --id {a.id} --key {st['key']}")
                break
            if st["state"] == "denied":
                return

    send(sock, b"S")
    chunk = 640  # 20 ms
    t0 = time.monotonic()
    for i in range(0, len(pcm), chunk):
        send(sock, b"A", pcm[i:i + chunk])
        target = t0 + (i + chunk) / 32000
        time.sleep(max(0, target - time.monotonic()))
    send(sock, b"C" if a.cancel else b"E")
    sent = time.monotonic()
    while True:
        t, body = recv_frame(sock)
        if t == b"O":
            print("status:", json.loads(body), f"(+{time.monotonic() - sent:.2f}s after end)")
            break
    sock.close()


if __name__ == "__main__":
    main()
