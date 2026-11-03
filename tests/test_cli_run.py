import json
import unittest
from pathlib import Path

from tracker.cli_run import build_argv, parse_result, run_prompt

RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 4321,
    "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 5, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 12000, "output_tokens": 80},
    "modelUsage": {"claude-sonnet-5": {"inputTokens": 5, "outputTokens": 80, "cacheReadInputTokens": 12000, "cacheCreationInputTokens": 300, "costUSD": 0.0123}}})

class CliTests(unittest.TestCase):
    def test_parse_prefers_model_usage_block(self):
        u = parse_result(RESULT, "claude-sonnet-5")
        self.assertEqual((u.input, u.output, u.cache_read, u.cache_write), (5, 80, 12000, 300))
        self.assertEqual(u.model, "claude-sonnet-5")
        self.assertAlmostEqual(u.cost_usd, 0.0123)
        self.assertAlmostEqual(u.duration_s, 4.321)
        self.assertEqual(u.total, 12385)

    def test_parse_picks_hinted_model_not_first_entry(self):
        d = json.loads(RESULT)
        d["modelUsage"] = {"claude-haiku-4-5-20251001": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": 0.0001},
                           **d["modelUsage"]}
        u = parse_result(json.dumps(d), "claude-sonnet-5")
        self.assertEqual(u.model, "claude-sonnet-5")
        self.assertEqual(u.total, 12385)

    def test_parse_falls_back_to_usage_block(self):
        d = json.loads(RESULT)
        del d["modelUsage"]
        u = parse_result(json.dumps(d), "claude-sonnet-5")
        self.assertEqual(u.cache_read, 12000)

    def test_error_result_raises(self):
        d = json.loads(RESULT)
        d["is_error"] = True
        with self.assertRaises(RuntimeError):
            parse_result(json.dumps(d), "claude-sonnet-5")

    def test_argv_and_env(self):
        self.assertEqual(build_argv("claude-opus-5", "low"),
                         ["claude", "-p", "--model", "claude-opus-5", "--effort", "low", "--output-format", "json", "--restricted"])
        seen = {}
        def runner(argv, prompt, env):
            seen.update(argv=argv, prompt=prompt, env=env)
            return RESULT
        u = run_prompt("say hi", "claude-sonnet-5", "low", Path("/x/.claude-dave"), runner=runner)
        self.assertEqual(seen["env"]["CLAUDE_CONFIG_DIR"], "/x/.claude-dave")
        self.assertEqual(seen["prompt"], "say hi")
        self.assertEqual(u.output, 80)


class ModelMismatchTests(unittest.TestCase):
    def test_missing_hinted_model_raises_rather_than_measuring_another(self):
        d = json.loads(RESULT)
        d["modelUsage"] = {"claude-haiku-4-5-20251001": d["modelUsage"]["claude-sonnet-5"]}
        with self.assertRaises(RuntimeError) as e:
            parse_result(json.dumps(d), "claude-opus-5")
        self.assertIn("claude-opus-5", str(e.exception))

    def test_a_newer_minor_version_is_not_the_model_asked_for(self):
        # Asked for claude-opus-5, served by claude-opus-5-5: a different model whose id merely
        # starts with the one asked for. Accepting it would measure the substitute.
        d = json.loads(RESULT)
        d["modelUsage"] = {"claude-opus-5-5": d["modelUsage"]["claude-sonnet-5"]}
        with self.assertRaises(RuntimeError):
            parse_result(json.dumps(d), "claude-opus-5")

    def test_exact_id_wins_over_a_longer_one_listed_first(self):
        d = json.loads(RESULT)
        block = d["modelUsage"]["claude-sonnet-5"]
        d["modelUsage"] = {"claude-sonnet-5-5": {**block, "outputTokens": 999}, "claude-sonnet-5": block}
        self.assertEqual(parse_result(json.dumps(d), "claude-sonnet-5").output, 80)

    def test_a_dated_snapshot_of_the_model_asked_for_still_counts(self):
        d = json.loads(RESULT)
        d["modelUsage"] = {"claude-sonnet-5-20260901": d["modelUsage"]["claude-sonnet-5"]}
        u = parse_result(json.dumps(d), "claude-sonnet-5")
        self.assertEqual((u.model, u.output), ("claude-sonnet-5-20260901", 80))

    def test_the_1m_context_marker_is_the_same_model(self):
        # Review round 1: the old prefix match accepted "claude-opus-5-5[1m]"; the exact-or-dated
        # rule refused it, failing every run on a model the CLI reports with the marker.
        d = json.loads(RESULT)
        block = d["modelUsage"]["claude-sonnet-5"]
        d["modelUsage"] = {"claude-opus-5-5[1m]": block}
        self.assertEqual(parse_result(json.dumps(d), "claude-opus-5-5").output, 80)
        d["modelUsage"] = {"claude-opus-5-5-20261001[1m]": block}
        self.assertEqual(parse_result(json.dumps(d), "claude-opus-5-5").output, 80)
        with self.assertRaises(RuntimeError):  # still another model when asked for Opus 5
            parse_result(json.dumps(d), "claude-opus-5")

    def test_every_key_of_the_model_asked_for_is_summed(self):
        # Code review round 10: a run reported under its dated id and its [1m] variant at once
        # was measured on whichever came first, dropping the other's tokens.
        d = json.loads(RESULT)
        block = d["modelUsage"]["claude-sonnet-5"]
        d["modelUsage"] = {"claude-haiku-4-5": {**block, "outputTokens": 999},
                           "claude-opus-5-5-20261001": block, "claude-opus-5-5[1m]": block}
        u = parse_result(json.dumps(d), "claude-opus-5-5")
        self.assertEqual(u.output, 160)
        self.assertEqual(u.input, 2 * int(block.get("inputTokens") or 0))

    def test_an_aliased_older_model_is_not_the_one_asked_for(self):
        # turns.MODEL_ALIASES prices claude-fable-5 as claude-fable-5-1; a probe of 5.1 served
        # by 5 is still the wrong model.
        d = json.loads(RESULT)
        d["modelUsage"] = {"claude-fable-5": d["modelUsage"]["claude-sonnet-5"]}
        with self.assertRaises(RuntimeError):
            parse_result(json.dumps(d), "claude-fable-5-1")
