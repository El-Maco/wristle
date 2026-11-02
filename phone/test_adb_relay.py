import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RelayTest(unittest.TestCase):
    def test_forwards_bytes(self):
        echo_port = free_port()
        relay_port = free_port()
        stop = threading.Event()

        def echo():
            with socket.socket() as srv:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("127.0.0.1", echo_port))
                srv.listen(1)
                srv.settimeout(0.2)
                while not stop.is_set():
                    try:
                        client, _ = srv.accept()
                    except socket.timeout:
                        continue
                    with client:
                        client.sendall(client.recv(1024))

        threading.Thread(target=echo, daemon=True).start()
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "adb_relay.py"),
             "--watch", "127.0.0.1", "--connect-port", str(echo_port),
             "--listen-connect", str(relay_port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", relay_port), timeout=0.2) as c:
                        c.sendall(b"hello through the phone")
                        self.assertEqual(c.recv(1024), b"hello through the phone")
                        return
                except OSError:
                    time.sleep(0.05)
            self.fail("relay did not start")
        finally:
            stop.set()
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
