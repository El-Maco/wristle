#!/usr/bin/env python3
"""
Wristle desktop listener
========================

Receives microphone audio from the Wristle Wear OS app over your local Wi-Fi
and feeds it to a push-to-talk dictation app (Hex, Handy, ...), which does
the transcription locally as usual.

How it works:
  1. The watch finds this PC with a UDP broadcast (port 47821).
  2. The watch connects over TCP (port 47820) and streams 16 kHz mono PCM.
  3. While you talk, this script holds the dictation app's shortcut and plays
     the watch audio into a virtual audio cable that the app uses as its mic.
  4. When you stop, the shortcut is released and the app pastes at your cursor.

With --voxtype, steps 3-4 are replaced: the audio is transcribed by the
`voxtype` CLI and typed with `wtype`. No virtual cable, hotkey or dictation
app is needed.

Nothing leaves your network. Run with --help for options.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import json
import os
import platform
import secrets
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

VERSION = "0.1.0"
PROTOCOL = 2      # watch hello "v"; 2 = HMAC challenge-response for paired watches
TCP_PORT = 47820
UDP_PORT = 47821
WATCH_RATE = 16000
DISCOVER_MAGIC = b"WRISTLE?"
REPLY_MAGIC = b"WRISTLE!"

# Frame types (1 byte) followed by a 4-byte big-endian payload length.
F_HELLO = b"H"   # watch -> pc, JSON {"id", "name", "v"}
F_AUTH = b"U"    # watch -> pc, hex HMAC proof answering an "auth" status (empty = no key)
F_START = b"S"   # watch -> pc, dictation begins
F_AUDIO = b"A"   # watch -> pc, PCM16LE mono 16 kHz
F_END = b"E"     # watch -> pc, finish and paste
F_CANCEL = b"C"  # watch -> pc, discard
F_PING = b"P"    # watch -> pc
F_PONG = b"K"    # pc -> watch
F_STATUS = b"O"  # pc -> watch, JSON {"state": "auth"|"ok"|"pending"|"denied"|"busy"|"done", ...}
F_QUIT = b"Q"    # localhost only: ask a running listener to exit (--stop)

SYSTEM = platform.system()  # "Windows", "Darwin", "Linux"

DEFAULTS = {
    "Windows": {"hotkey": "ctrl+space", "device": "CABLE Input"},
    "Darwin": {"hotkey": "alt", "device": "BlackHole"},
    "Linux": {"hotkey": "ctrl+space", "device": "pulse"},  # with PULSE_SINK=wristle
}.get(SYSTEM, {"hotkey": "ctrl+space", "device": ""})


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def config_dir() -> Path:
    if SYSTEM == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif SYSTEM == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "wristle"
    d.mkdir(parents=True, exist_ok=True)
    return d


def auth_proof(key_hex: str, nonce: str, device_id: str) -> str:
    """What a paired watch sends back for a nonce; the watch computes the same."""
    msg = f"wristle-auth|{nonce}|{device_id}".encode("utf-8")
    return hmac.new(bytes.fromhex(key_hex), msg, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Pairing store
# --------------------------------------------------------------------------- #

class PairedWatches:
    """device id -> {"name", "paired_at", "key"}. The key is a shared secret, so keep this file private."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def key(self, device_id: str) -> str | None:
        """The watch's secret, or None if it isn't paired (entries from before keys existed count as unpaired)."""
        with self.lock:
            return self.data.get(device_id, {}).get("key")

    def add(self, device_id: str, name: str, key: str):
        with self.lock:
            self.data[device_id] = {"name": name, "paired_at": time.strftime("%Y-%m-%d %H:%M:%S"), "key": key}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #

class LinearResampler:
    """Streaming linear-interpolation resampler (plenty for speech recognition)."""

    def __init__(self, src_rate: int, dst_rate: int):
        self.step = src_rate / dst_rate
        self.pos = 0.0      # read position; index 0 == previous chunk's last sample
        self.prev = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        if self.step == 1.0:
            return x
        ext = np.concatenate(([self.prev], x)).astype(np.float32)
        t = np.arange(self.pos, x.size, self.step)
        out = np.interp(t, np.arange(ext.size), ext).astype(np.float32)
        last = t[-1] if t.size else self.pos - self.step
        self.pos = last + self.step - x.size
        self.prev = float(x[-1])
        return out


class DeviceSink:
    """Keeps an output stream open on the virtual cable and plays queued samples."""

    def __init__(self, device_query: str):
        import sounddevice as sd

        self.sd = sd
        index = find_output_device(device_query)
        info = sd.query_devices(index)
        self.name = f"{info['name']} ({sd.query_hostapis(info['hostapi'])['name']})"
        self.rate = int(info["default_samplerate"])
        self.channels = max(1, min(2, int(info["max_output_channels"])))
        self.lock = threading.Lock()
        self.chunks: collections.deque[np.ndarray] = collections.deque()
        self.queued = 0
        self.stream = sd.OutputStream(
            device=index,
            samplerate=self.rate,
            channels=self.channels,
            dtype="float32",
            latency="low",
            callback=self._callback,
        )
        self.stream.start()

    def _callback(self, outdata, frames, _time, _status):
        out = np.zeros(frames, dtype=np.float32)
        filled = 0
        with self.lock:
            while filled < frames and self.chunks:
                chunk = self.chunks[0]
                n = min(chunk.size, frames - filled)
                out[filled:filled + n] = chunk[:n]
                if n == chunk.size:
                    self.chunks.popleft()
                else:
                    self.chunks[0] = chunk[n:]
                filled += n
            self.queued -= filled
        outdata[:] = out[:, None]

    def write(self, samples: np.ndarray):
        if samples.size:
            with self.lock:
                self.chunks.append(samples)
                self.queued += samples.size

    def queued_seconds(self) -> float:
        with self.lock:
            return self.queued / self.rate

    def clear(self):
        with self.lock:
            self.chunks.clear()
            self.queued = 0

    def close(self):
        self.stream.stop()
        self.stream.close()


class NullSink:
    """No audio device: drains at real time so timing behaves like the real thing."""

    def __init__(self, rate: int = 48000):
        self.name = "null (no audio device)"
        self.rate = rate
        self.lock = threading.Lock()
        self.queued = 0
        self.played: list[np.ndarray] = []
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        last = time.monotonic()
        while True:
            time.sleep(0.01)
            now = time.monotonic()
            with self.lock:
                self.queued = max(0, self.queued - int((now - last) * self.rate))
            last = now

    def write(self, samples: np.ndarray):
        with self.lock:
            self.queued += samples.size
            self.played.append(samples)

    def queued_seconds(self) -> float:
        with self.lock:
            return self.queued / self.rate

    def clear(self):
        with self.lock:
            self.queued = 0
            self.played = []

    def close(self):
        pass


def parse_transcript(stdout: str) -> str:
    """Take the transcript out of `voxtype transcribe` stdout.

    voxtype prints progress lines, a blank line, then the text. Keep everything
    after the last "Processing ... samples ..." line so multiline text survives.
    """
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Processing ") and line.rstrip().endswith("..."):
            return "\n".join(lines[i + 1:]).strip()
    # Older/newer voxtype output without the marker: drop its known preamble.
    keep = [line for line in lines
            if not line.startswith(("Loading audio file:", "Audio format:", "Processing "))]
    return "\n".join(keep).strip()


class Voxtype:
    """Transcribe raw 16 kHz PCM with the voxtype CLI, then type the text."""

    def __init__(self, binary: str = "voxtype", type_cmd: str = "wtype -", timeout: int = 120):
        self.binary = binary
        self.type_cmd = shlex.split(type_cmd)
        self.timeout = timeout

    def run(self, pcm: bytes) -> bool:
        if not pcm:
            return False
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(WATCH_RATE)
                w.writeframes(pcm)
            proc = subprocess.run(
                [self.binary, "-q", "transcribe", path],
                capture_output=True, text=True, timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError, wave.Error) as e:
            log(f"! voxtype failed: {e}")
            return False
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if proc.returncode != 0:
            log(f"! voxtype exited {proc.returncode}: {proc.stderr.strip()[-300:]}")
            return False
        text = parse_transcript(proc.stdout)
        if not text:
            log("! voxtype returned no text")
            return False
        try:
            subprocess.run(self.type_cmd, input=text, text=True, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"! typing failed: {e}")
            return False
        log(f"  typed {len(text)} characters")
        return True


HOSTAPI_PREFERENCE = ["Windows WASAPI", "MME", "Windows DirectSound", "Core Audio",
                      "PulseAudio", "ALSA", "JACK Audio Connection Kit"]


def find_output_device(query: str) -> int:
    import sounddevice as sd

    devices = sd.query_devices()
    if query.isdigit():
        return int(query)
    matches = []
    for i, d in enumerate(devices):
        if d["max_output_channels"] > 0 and query.lower() in d["name"].lower():
            api = sd.query_hostapis(d["hostapi"])["name"]
            if api == "Windows WDM-KS":
                continue
            rank = HOSTAPI_PREFERENCE.index(api) if api in HOSTAPI_PREFERENCE else 99
            matches.append((rank, i))
    if not matches:
        raise SystemExit(
            f"No output device matching '{query}'. Install the virtual cable "
            f"(see README) or run with --list-devices and pass --device."
        )
    return sorted(matches)[0][1]


def list_devices():
    import sounddevice as sd

    print("Output devices (pass a name fragment or the number to --device):")
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            api = sd.query_hostapis(d["hostapi"])["name"]
            print(f"  [{i:>3}] {d['name']}  ({api}, {int(d['default_samplerate'])} Hz)")


# --------------------------------------------------------------------------- #
# Hotkey
# --------------------------------------------------------------------------- #

KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "option": "alt", "opt": "alt",
    "right_option": "alt_r", "ropt": "alt_r", "command": "cmd", "win": "cmd",
    "super": "cmd", "return": "enter", "esc": "esc",
}


class MacKeys:
    """
    Posts keys through Quartz on macOS. Modifiers go out as flagsChanged events
    (what dictation apps' event taps read for modifier-only shortcuts like Option), with
    the cumulative modifier flags carried on every event.
    """

    # name: (virtual keycode, flag bits incl. device-specific side bit)
    MODIFIERS = {
        "alt": (58, 0x80000 | 0x20), "alt_l": (58, 0x80000 | 0x20), "alt_r": (61, 0x80000 | 0x40),
        "ctrl": (59, 0x40000 | 0x01), "ctrl_l": (59, 0x40000 | 0x01), "ctrl_r": (62, 0x40000 | 0x2000),
        "shift": (56, 0x20000 | 0x02), "shift_l": (56, 0x20000 | 0x02), "shift_r": (60, 0x20000 | 0x04),
        "cmd": (55, 0x100000 | 0x08), "cmd_l": (55, 0x100000 | 0x08), "cmd_r": (54, 0x100000 | 0x10),
        "fn": (63, 0x800000),
    }
    NAMED = {"space": 49, "esc": 53, "enter": 36, "tab": 48, "f13": 105, "f14": 107,
             "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90}
    NON_COALESCED = 0x100

    def __init__(self, names: list[str]):
        import Quartz  # installed with pynput on macOS (pyobjc-framework-Quartz)

        self.Q = Quartz
        self.flags = 0
        self.steps = []
        for n in names:
            if n in self.MODIFIERS:
                code, bits = self.MODIFIERS[n]
                self.steps.append((code, bits))
            elif n in self.NAMED:
                self.steps.append((self.NAMED[n], 0))
            elif len(n) == 1:
                from pynput.keyboard._darwin import get_unicode_to_keycode_map
                code = get_unicode_to_keycode_map().get(n)
                if code is None:
                    raise SystemExit(f"Can't map key '{n}' on this keyboard layout")
                self.steps.append((code, 0))
            else:
                raise SystemExit(f"Unknown key '{n}'")

    def _post(self, code: int, down: bool, modifier: bool):
        Q = self.Q
        ev = Q.CGEventCreateKeyboardEvent(None, code, down)
        if modifier:
            Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
        Q.CGEventSetFlags(ev, self.flags | self.NON_COALESCED)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    def press(self):
        for code, bits in self.steps:
            if bits:
                self.flags |= bits
            self._post(code, True, bool(bits))
            time.sleep(0.02)

    def release(self):
        for code, bits in reversed(self.steps):
            if bits:
                self.flags &= ~bits
            self._post(code, False, bool(bits))
            time.sleep(0.02)

    def tap_escape(self):
        self._post(53, True, False)
        self._post(53, False, False)


class Hotkey:
    def __init__(self, spec: str, enabled: bool = True):
        self.spec = spec
        self.enabled = enabled
        self.held = False
        self.keys = []
        self.esc = None
        self.mac = None
        if enabled and SYSTEM == "Darwin":
            names = [KEY_ALIASES.get(p, p) for p in spec.lower().replace(" ", "").split("+")]
            self.mac = MacKeys(names)
        elif enabled:
            from pynput.keyboard import Controller, Key, KeyCode

            self.kb = Controller()
            self.esc = Key.esc
            for part in spec.lower().replace(" ", "").split("+"):
                name = KEY_ALIASES.get(part, part)
                if hasattr(Key, name):
                    self.keys.append(getattr(Key, name))
                elif len(name) == 1:
                    self.keys.append(KeyCode.from_char(name))
                else:
                    raise SystemExit(f"Unknown key '{part}' in hotkey '{spec}'")

    def press(self):
        if not self.enabled:
            return
        self.held = True
        if self.mac:
            return self.mac.press()
        for k in self.keys:
            self.kb.press(k)
            time.sleep(0.02)

    def release(self):
        if not self.enabled:
            return
        self.held = False
        if self.mac:
            return self.mac.release()
        for k in reversed(self.keys):
            self.kb.release(k)
            time.sleep(0.02)

    def release_if_held(self):
        """On exit: never leave the shortcut (e.g. Ctrl) stuck down."""
        if self.held:
            try:
                self.release()
            except Exception:
                pass

    def tap_escape(self):
        if not self.enabled:
            return
        if self.mac:
            return self.mac.tap_escape()
        self.kb.press(self.esc)
        self.kb.release(self.esc)


# --------------------------------------------------------------------------- #
# Dictation session (one at a time)
# --------------------------------------------------------------------------- #

class Dictation:
    def __init__(self, sink, hotkey: Hotkey, start_delay: float, tail: float,
                 gain: float, save_dir: Path | None, transcriber: Voxtype | None = None):
        self.sink = sink
        self.hotkey = hotkey
        self.start_delay = start_delay
        self.tail = tail
        self.gain = gain
        self.save_dir = save_dir
        self.transcriber = transcriber
        self.lock = threading.Lock()
        self.owner = None
        self.resampler = None
        self.raw: list[bytes] = []
        self.started_at = 0.0

    def start(self, owner) -> bool:
        with self.lock:
            if self.owner is not None:
                return False
            self.owner = owner
            self.raw = []
            self.started_at = time.monotonic()
            if self.transcriber is None:
                self.resampler = LinearResampler(WATCH_RATE, self.sink.rate)
                self.sink.clear()
        if self.transcriber is None:
            self.hotkey.press()
            # Give the dictation app a moment to open its microphone before the speech arrives.
            self.sink.write(np.zeros(int(self.start_delay * self.sink.rate), dtype=np.float32))
        log("● dictation started")
        return True

    def audio(self, owner, pcm: bytes):
        if self.owner is not owner:
            return
        if self.transcriber is not None:
            self.raw.append(pcm)
            return
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if self.gain != 1.0:
            x = np.clip(x * self.gain, -1.0, 1.0)
        if self.save_dir:
            self.raw.append(pcm)
        self.sink.write(self.resampler.process(x))

    def save_wav(self):
        if not (self.save_dir and self.raw):
            return
        path = self.save_dir / time.strftime("watch-%Y%m%d-%H%M%S.wav")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(WATCH_RATE)
            w.writeframes(b"".join(self.raw))
        log(f"  saved {path}")

    def finish(self, owner, cancel: bool = False):
        if self.owner is not owner:
            return
        ok = True
        if self.transcriber is not None:
            if not cancel:
                ok = self.transcriber.run(b"".join(self.raw))
        elif not cancel:
            deadline = time.monotonic() + 15
            while self.sink.queued_seconds() > 0.005 and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(self.tail)
        else:
            self.sink.clear()
            self.hotkey.tap_escape()
            time.sleep(0.05)
        if self.transcriber is None:
            self.hotkey.release()
        seconds = time.monotonic() - self.started_at
        self.save_wav()
        outcome = "cancelled" if cancel else ("sent" if ok else "failed")
        log(f"■ dictation {outcome} ({seconds:.1f}s)")
        with self.lock:
            self.owner = None


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #

def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return bytes(buf)


class WatchConnection:
    def __init__(self, server: "Server", sock: socket.socket, addr):
        self.server = server
        self.sock = sock
        self.addr = addr
        self.send_lock = threading.Lock()
        self.device_id = None
        self.name = "watch"
        self.approved = False
        self.in_session = False
        self.nonce = None   # outstanding auth challenge
        self.code = None    # pairing code while waiting for approval

    def send(self, ftype: bytes, payload: bytes = b""):
        with self.send_lock:
            self.sock.sendall(ftype + struct.pack(">I", len(payload)) + payload)

    def status(self, state: str, **extra):
        try:
            self.send(F_STATUS, json.dumps({"state": state, "host": self.server.name, **extra}).encode())
        except OSError:
            pass

    def run(self):
        dictation = self.server.dictation
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(60)  # watch pings every 10 s
            while True:
                header = recv_exact(self.sock, 5)
                ftype, length = header[:1], struct.unpack(">I", header[1:])[0]
                # Audio frames are 640 bytes; unpaired peers only get room for a hello.
                if length > (65536 if self.approved else 4096):
                    raise ConnectionError("frame too large")
                payload = recv_exact(self.sock, length) if length else b""

                if ftype == F_QUIT and self.addr[0] == "127.0.0.1":
                    log("Stop requested, exiting")
                    dictation.hotkey.release_if_held()
                    os._exit(0)
                if ftype == F_HELLO:
                    if self.device_id is not None:
                        continue  # one hello per connection
                    hello = json.loads(payload.decode("utf-8"))
                    self.device_id = str(hello.get("id", ""))[:64]
                    self.name = str(hello.get("name", "watch"))[:64]
                    if not self.device_id:
                        raise ConnectionError("hello without id")
                    if int(hello.get("v", 1)) < PROTOCOL:
                        log(f"{self.name} runs an older Wristle watch app: update it on the watch")
                        return
                    if self.server.paired.key(self.device_id):
                        self.nonce = secrets.token_hex(16)
                        self.status("auth", nonce=self.nonce)
                    else:
                        self.server.request_pairing(self)
                elif ftype == F_AUTH:
                    if self.nonce is None:
                        continue
                    nonce, self.nonce = self.nonce, None
                    key = self.server.paired.key(self.device_id)
                    if key and hmac.compare_digest(payload, auth_proof(key, nonce, self.device_id).encode()):
                        self.approved = True
                        self.status("ok")
                        log(f"↔ {self.name} connected from {self.addr[0]}")
                    else:
                        log(f"! {self.name} at {self.addr[0]} failed authentication, asking to pair again")
                        self.server.request_pairing(self)
                elif ftype == F_PING:
                    self.send(F_PONG)
                elif not self.approved:
                    continue
                elif ftype == F_START:
                    if dictation.start(self):
                        self.in_session = True
                    else:
                        self.status("busy")
                elif ftype == F_AUDIO:
                    if self.in_session:
                        dictation.audio(self, payload)
                elif ftype in (F_END, F_CANCEL):
                    if self.in_session:
                        self.in_session = False
                        dictation.finish(self, cancel=(ftype == F_CANCEL))
                        self.status("done")
        except (ConnectionError, OSError, ValueError) as e:
            if self.approved:
                log(f"↔ {self.name} disconnected ({e})")
        finally:
            if self.in_session:
                # Connection dropped mid-dictation: keep what was said.
                dictation.finish(self, cancel=False)
            self.server.forget(self)
            try:
                self.sock.close()
            except OSError:
                pass


MAX_CONNECTIONS = 8
MAX_PENDING = 3


class Server:
    def __init__(self, dictation: Dictation, paired: PairedWatches, port: int, auto_pair: bool):
        self.dictation = dictation
        self.paired = paired
        self.port = port
        self.auto_pair = auto_pair
        self.pending: list[WatchConnection] = []
        self.pending_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.background = False
        self.name = socket.gethostname()  # shown on the watch
        self.udp = make_socket(socket.SOCK_DGRAM, "", UDP_PORT)
        self.tcp = make_socket(socket.SOCK_STREAM, "0.0.0.0", port)
        self.tcp.listen(4)

    def request_pairing(self, conn: WatchConnection):
        if self.auto_pair:
            self.approve(conn)
            return
        # A fresh random code per request, sent to this connection only: a device that
        # races your watch can't choose an id whose code matches what your watch shows.
        conn.code = f"{secrets.randbelow(10000):04d}"
        with self.pending_lock:
            if len(self.pending) >= MAX_PENDING:
                log(f"! Too many pairing requests, ignoring '{conn.name}' at {conn.addr[0]}")
                return
            self.pending.append(conn)
            first = len(self.pending) == 1
        conn.status("pending", code=conn.code)
        if first:
            self.prompt(conn)

    def prompt(self, conn: WatchConnection):
        if self.background:
            log(f"New watch '{conn.name}' wants to pair (code {conn.code}). "
                f"Pairing needs the listener window: run stop.bat, then start.bat.")
            return
        print(
            f"\n  New watch '{conn.name}' at {conn.addr[0]} wants to pair.\n"
            f"  Code on the watch should be {conn.code}.\n"
            f"  Type y + Enter to allow, n + Enter to deny: ",
            end="", flush=True,
        )

    def approve(self, conn: WatchConnection):
        key = secrets.token_hex(32)
        self.paired.add(conn.device_id, conn.name, key)
        conn.approved = True
        conn.status("ok", key=key)
        log(f"✓ {conn.name} paired")

    def answer_pairing(self, yes: bool):
        with self.pending_lock:
            if not self.pending:
                return
            conn = self.pending.pop(0)
            following = self.pending[0] if self.pending else None
        if yes:
            self.approve(conn)
        else:
            conn.status("denied")
            log(f"✗ {conn.name} denied")
            try:
                conn.sock.close()
            except OSError:
                pass
        if following:
            self.prompt(following)

    def forget(self, conn: WatchConnection):
        with self.pending_lock:
            if conn in self.pending:
                was_first = self.pending[0] is conn
                self.pending.remove(conn)
                following = self.pending[0] if was_first and self.pending else None
            else:
                following = None
        if following:
            self.prompt(following)

    def serve_discovery(self):
        udp = self.udp
        reply = REPLY_MAGIC + f" {self.port} {self.name}".encode()
        while True:
            try:
                data, addr = udp.recvfrom(256)
                if data.startswith(DISCOVER_MAGIC):
                    udp.sendto(reply, addr)
            except OSError:
                time.sleep(0.5)

    def serve_tcp(self):
        srv = self.tcp
        while True:
            sock, addr = srv.accept()
            if not self.slots.acquire(blocking=False):
                sock.close()  # too many open connections
                continue
            conn = WatchConnection(self, sock, addr)
            threading.Thread(target=self._run, args=(conn,), daemon=True).start()

    def _run(self, conn: WatchConnection):
        try:
            conn.run()
        finally:
            self.slots.release()


def make_socket(kind, host: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, kind)
    if SYSTEM == "Windows":
        # On Windows SO_REUSEADDR would let a second listener share the port.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except OSError:
        s.close()
        raise SystemExit(f"Port {port} is in use: the listener is probably already running "
                         f"(stop it with --stop or stop.bat).")
    return s


def stop_running(port: int):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(F_QUIT + struct.pack(">I", 0))
            time.sleep(0.3)
        print("Stopped the running listener.")
    except OSError:
        print("No listener is running.")


def local_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(description="Wristle: use your Wear OS watch as a push-to-talk mic for a local dictation app.")
    p.add_argument("--device", default=DEFAULTS["device"],
                   help=f"virtual cable output device name fragment or number (default: '{DEFAULTS['device']}')")
    p.add_argument("--hotkey", default=DEFAULTS["hotkey"],
                   help=f"the dictation app's push-to-talk shortcut to hold, e.g. alt, ctrl+space, alt_r (default: {DEFAULTS['hotkey']})")
    p.add_argument("--port", type=int, default=TCP_PORT, help="TCP port (default 47820)")
    p.add_argument("--name", help="name the watch shows for this PC (default: the computer name); "
                                  "changing it means pairing the watch again")
    p.add_argument("--start-delay", type=float, default=0.15,
                   help="seconds of silence after pressing the shortcut, so the app's mic is open (default 0.15)")
    p.add_argument("--tail", type=float, default=0.35,
                   help="seconds to keep holding after the last audio (default 0.35)")
    p.add_argument("--gain", type=float, default=1.0, help="volume multiplier for watch audio (default 1.0)")
    p.add_argument("--save-wav", metavar="DIR", help="also save each dictation as a WAV here (for debugging)")
    p.add_argument("--auto-pair", action="store_true", help="pair new watches without asking (not recommended)")
    p.add_argument("--list-devices", action="store_true", help="list audio output devices and exit")
    p.add_argument("--test-hotkey", action="store_true",
                   help="hold the shortcut for 4 s (speak into your normal mic) to check the dictation app reacts, then exit")
    p.add_argument("--dry-run", action="store_true", help="no audio device and no key presses (protocol testing)")
    p.add_argument("--voxtype", action="store_true",
                   help="transcribe each dictation with the voxtype CLI and type it with wtype "
                        "(no virtual cable or hotkey)")
    p.add_argument("--voxtype-bin", default="voxtype", help="voxtype executable (default: voxtype)")
    p.add_argument("--type-cmd", default="wtype -",
                   help="command that types text from stdin (default: 'wtype -')")
    p.add_argument("--background", action="store_true",
                   help="run without a console; log to listener.log in the settings folder")
    p.add_argument("--stop", action="store_true", help="stop a listener that is already running, then exit")
    args = p.parse_args()

    if args.stop:
        stop_running(args.port)
        return

    if args.background:
        log_path = config_dir() / "listener.log"
        try:
            if log_path.stat().st_size > 1_000_000:
                log_path.replace(log_path.with_suffix(".old.log"))
        except OSError:
            pass
        f = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f

    if args.list_devices:
        if args.voxtype:
            raise SystemExit("--list-devices is for the virtual-cable mode; --voxtype needs no audio device")
        list_devices()
        return

    if args.voxtype and args.test_hotkey:
        raise SystemExit("--test-hotkey is for the hotkey mode; drop one of the two flags")

    if args.test_hotkey:
        hk = Hotkey(args.hotkey)
        print(f"Click into a text box. Holding '{args.hotkey}' in 3 seconds for 4 seconds — say something.")
        time.sleep(3)
        hk.press()
        print("Holding… speak now")
        time.sleep(4)
        hk.release()
        print("Released. If your dictation app started and stopped recording, the shortcut works.")
        return

    if SYSTEM == "Linux" and args.device == "pulse":
        os.environ.setdefault("PULSE_SINK", "wristle")  # the null sink from the README

    transcriber = None
    sink = None
    if args.voxtype:
        transcriber = Voxtype(args.voxtype_bin, args.type_cmd)
        sink = NullSink(rate=WATCH_RATE)
    else:
        for attempt in range(30 if args.background else 1):
            try:
                sink = NullSink() if args.dry_run else DeviceSink(args.device)
                break
            except (Exception, SystemExit) as e:  # audio may not be ready yet right after login
                if attempt == (29 if args.background else 0):
                    log(f"Audio device error: {e}")
                    raise
                time.sleep(2)
                try:
                    # PortAudio only lists devices when it starts, so restart it to see new ones.
                    import sounddevice as sd
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass
    hotkey = Hotkey(args.hotkey, enabled=not (args.dry_run or args.voxtype))
    save_dir = Path(args.save_wav) if args.save_wav else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
    dictation = Dictation(sink, hotkey, args.start_delay, args.tail, args.gain, save_dir, transcriber)
    paired = PairedWatches(config_dir() / "paired_watches.json")
    server = Server(dictation, paired, args.port, args.auto_pair)
    server.background = args.background
    if args.name:
        server.name = args.name.strip()[:40]

    threading.Thread(target=server.serve_discovery, daemon=True).start()
    threading.Thread(target=server.serve_tcp, daemon=True).start()

    print(f"Wristle listener {VERSION}")
    if transcriber:
        print(f"  output    : {transcriber.binary} → {' '.join(transcriber.type_cmd)}")
    else:
        print(f"  audio out : {sink.name}  →  choose this cable as your dictation app's microphone")
        print(f"  shortcut  : {args.hotkey} (the app must use hold / push-to-talk mode)")
    print(f"  listening : {', '.join(local_ips()) or 'all interfaces'} : {args.port} (discovery on UDP {UDP_PORT})")
    print("  Open Wristle on your watch. Ctrl+C to quit.\n")

    try:
        while args.background:
            time.sleep(3600)
        while True:
            try:
                line = input()
            except (EOFError, RuntimeError, OSError):
                while True:
                    time.sleep(3600)
            answer = line.strip().lower()
            if answer in ("y", "yes"):
                server.answer_pairing(True)
            elif answer in ("n", "no"):
                server.answer_pairing(False)
    except KeyboardInterrupt:
        print("\nBye")
    finally:
        hotkey.release_if_held()
        sink.close()


if __name__ == "__main__":
    main()
