"""Run one `claude -p` prompt and return its token usage."""
from __future__ import annotations
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class RunUsage:
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    cost_usd: float | None
    duration_s: float

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def build_argv(model: str, effort: str) -> list[str]:
    return ["claude", "-p", "--model", model, "--effort", effort, "--output-format", "json", "--restricted"]


def parse_result(stdout: str, model_hint: str) -> RunUsage:
    d = json.loads(stdout)
    if d.get("is_error"):
        raise RuntimeError(f"claude -p returned an error result: {d.get('result') or d.get('subtype')}")
    mu = d.get("modelUsage") or {}
    if mu:
        # Prefer the entry for the model we asked for; a haiku sidecar can appear first.
        model = next((k for k in mu if k == model_hint or k.startswith(model_hint)), next(iter(mu)))
        m = mu[model]
        return RunUsage(model, int(m.get("inputTokens") or 0), int(m.get("outputTokens") or 0),
                        int(m.get("cacheReadInputTokens") or 0), int(m.get("cacheCreationInputTokens") or 0),
                        m.get("costUSD", d.get("total_cost_usd")), (d.get("duration_ms") or 0) / 1000)
    u = d.get("usage") or {}
    return RunUsage(model_hint, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                    int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0),
                    d.get("total_cost_usd"), (d.get("duration_ms") or 0) / 1000)


def _default_runner(argv: list[str], prompt: str, env: dict) -> str:
    p = subprocess.run(argv, input=prompt, capture_output=True, text=True, env=env, timeout=900)
    if p.returncode != 0:
        raise RuntimeError(f"claude exited {p.returncode}: {p.stderr[-500:]}")
    return p.stdout


def run_prompt(prompt: str, model: str, effort: str, config_dir: Path | None,
               runner: Callable[[list[str], str, dict], str] | None = None) -> RunUsage:
    env = dict(os.environ)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env.pop("ANTHROPIC_API_KEY", None)  # must bill the subscription, never an API key
    out = (runner or _default_runner)(build_argv(model, effort), prompt, env)
    return parse_result(out, model)
