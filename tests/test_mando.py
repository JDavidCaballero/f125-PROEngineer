"""Botón del mando para hablar: el juego manda los botones por UDP (evento BUTN) y el programa aprende cuál es el tuyo."""
import json, queue, socket, tempfile, threading, time, types, unittest
from pathlib import Path

import helpers  # noqa: F401  (añade la raíz del proyecto al path)
import f1_parse as P
import voice as V

R3 = 1 << 14          # bit de ejemplo; el programa no lo da por hecho: lo aprende


def butn(mask):
    return P.HEADER.pack(2025, 25, 1, 26, 1, 3, 7, 1.0, 1, 1, 0, 255) + b"BUTN" + mask.to_bytes(4, "little") + bytes(8)


class Parse(unittest.TestCase):
    def test_button_mask(self):
        self.assertEqual(V.button_status(butn(R3 | 1)), R3 | 1)
        self.assertEqual(P.event(butn(R3))["buttons"], R3)

    def test_other_packets_are_ignored(self):
        other = P.HEADER.pack(2025, 25, 1, 26, 1, 3, 7, 1.0, 1, 1, 0, 255) + b"SCAR" + bytes(12)
        self.assertIsNone(V.button_status(other))
        self.assertIsNone(V.button_status(b"\x00" * 40))


class Push(unittest.TestCase):
    def test_press_and_release_are_two_enters(self):
        lines = types.SimpleNamespace(q=queue.Queue())
        pad = V.PadButton(R3, lines)
        for st in (0, R3, R3 | 1, R3, 1, 0):                 # pulsar R3, otro botón a la vez, soltar
            pad.update(st)
        self.assertEqual(lines.q.qsize(), 2)

    def test_config_mask_wins(self):
        self.assertEqual(V.load_pad({"voice": {"ptt_button_mask": R3}}), R3)


class Learn(unittest.TestCase):
    def test_learns_the_button_pressed_three_times(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "mando.json"
            args = types.SimpleNamespace(port=port, out=out, wait_s=10)
            t = threading.Thread(target=V.cmd_boton, args=(args, {"network": {"port": port}}), daemon=True)
            t.start()
            time.sleep(0.3)
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for _ in range(3):
                for st in (R3, 0):
                    tx.sendto(butn(st), ("127.0.0.1", port))
                    time.sleep(0.05)
            tx.close()
            t.join(5)
            got = json.loads(out.read_text())
            self.assertEqual(got["mask"], R3)
            self.assertTrue(got["suelta"])


if __name__ == "__main__":
    unittest.main()
