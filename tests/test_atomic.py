import json
import tempfile
import threading
import unittest
from pathlib import Path

from tracker.publish import write_json


class AtomicWriteTests(unittest.TestCase):
    def test_concurrent_writers_never_collide_on_a_shared_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "state.json")
            errors = []

            def writer(n):
                try:
                    for _ in range(40):
                        write_json(path, {"n": n, "pad": "x" * 200_000})
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertIn(json.loads(path.read_text())["n"], range(4))
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["state.json"])
