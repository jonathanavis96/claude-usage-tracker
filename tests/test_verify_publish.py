import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools import verify_publish as vp

# The rebuilt document, as the publisher would write it at the published file's generated_at.
REBUILT = {
    "generated_at": "2026-09-23T11:30:31.325972+00:00",
    "last_change": {"date": "2026-09-14", "direction": "down", "percent": 19, "provisional": False},
    "rates": {"claude-opus-5-5": {"tokens_per_pct": 812345.6, "n": 12}},
    "weekly_windows": {"passive": {"by_window": [{"window_ending": "2026-09-23T10:00:00+00:00", "ratio": 3.7}]}},
}
# Fixture 1: the published file is the rebuild, written an instant later.
EQUAL = json.loads(json.dumps(REBUILT)) | {"generated_at": "2026-09-23T11:30:31.999999+00:00"}
# Fixture 2: the same, with one published figure changed.
CHANGED = json.loads(json.dumps(EQUAL))
CHANGED["rates"]["claude-opus-5-5"]["tokens_per_pct"] = 812345.7


def run(published: dict) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "claude-usage.json"
        path.write_text(json.dumps(published), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(vp, "rebuild", return_value=REBUILT) as rebuild, redirect_stdout(out):
            rc = vp.main(["--published", str(path)])
        # The rebuild runs at the published file's own clock, not the checker's.
        assert rebuild.call_args.args[1].isoformat() == published["generated_at"]
        return rc, out.getvalue()


class VerifyPublishTest(unittest.TestCase):
    def test_equal_documents_pass(self):
        rc, out = run(EQUAL)
        self.assertEqual(rc, 0)
        self.assertIn("identical", out)

    def test_one_changed_figure_fails_and_is_named(self):
        rc, out = run(CHANGED)
        self.assertEqual(rc, 1)
        self.assertIn("rates.claude-opus-5-5.tokens_per_pct: published 812345.7, rebuilt 812345.6", out)
        self.assertIn("1 difference(s)", out)

    def test_diff_reports_missing_keys_list_lengths_and_type_changes(self):
        published = {"a": 1, "b": [1, 2], "c": True, "generated_at": "x"}
        rebuilt = {"b": [1], "c": 1, "d": None, "generated_at": "y"}
        lines = vp.diff(published, rebuilt)
        self.assertEqual(lines, [
            "a: published 1, missing from rebuild",
            "b: published 2 items, rebuilt 1",
            "c: published true, rebuilt 1",
            "d: missing from published, rebuilt null",
        ])

    def test_integer_and_float_forms_of_one_number_are_equal(self):
        self.assertEqual(vp.diff({"x": 3}, {"x": 3.0}), [])

    def test_publisher_argv_matches_the_cron_line(self):
        daily = (Path(__file__).resolve().parent.parent / "bin/daily.sh").read_text(encoding="utf-8")
        cron = daily.split("python3 -m tracker.publish", 1)[1].split("rc=$?", 1)[0]
        cron_flags = {w for w in cron.split() if w.startswith("--")}
        argv = vp.publisher_argv(Path("/r"), Path("/t/prices.json"), Path("/t/out.json"))
        self.assertLessEqual(cron_flags, set(argv))


if __name__ == "__main__":
    unittest.main()
