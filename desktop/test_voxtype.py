import tempfile
import unittest
from pathlib import Path

from wristle import Voxtype, parse_transcript


class ParseTranscriptTest(unittest.TestCase):
    def test_drops_progress_lines_and_keeps_multiline_text(self):
        out = (
            'Loading audio file: "x.wav"\n'
            "Audio format: 16000 Hz, 1 channel(s), Int\n"
            "Processing 176000 samples (11.00s)...\n"
            "\n"
            "Hello there.\n"
            "Second line.\n"
        )
        self.assertEqual(parse_transcript(out), "Hello there.\nSecond line.")

    def test_falls_back_to_whole_output(self):
        self.assertEqual(parse_transcript("just text\n"), "just text")

    def test_drops_preamble_without_marker(self):
        out = 'Loading audio file: "x.wav"\nAudio format: 16000 Hz\n\njust text\n'
        self.assertEqual(parse_transcript(out), "just text")


class VoxtypeTest(unittest.TestCase):
    def test_transcribes_and_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = tmp / "fake-voxtype"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "print('Processing 1 samples (0.00s)...')\n"
                "print()\n"
                "print('hello from fake')\n"
            )
            fake.chmod(0o755)
            typed = tmp / "typed.txt"

            ok = Voxtype(str(fake), f"tee {typed}").run(b"\0\0" * 16000)

            self.assertTrue(ok)
            self.assertEqual(typed.read_text(), "hello from fake")


if __name__ == "__main__":
    unittest.main()
