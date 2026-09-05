import json
import unittest
from pathlib import Path
from tracker.cli_run import parse_result, run_prompt, build_argv

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
