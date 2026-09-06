# Cross-Agent Handoff: Claude Usage Tracker, continue from 2026-09-06 13:30 UTC

## Outcome requested

Finish the probe redesign: read the 5-tick burst validation row, pick the measured tick count, then get the Sonnet/Opus/Fable rotation, confirm-by-rerun and per-model prompt sizing built (on the Dave account on gs) and scheduled. Merge the notify-me PR once Codex quota returns.

## Relevant repository

- `~/code/claude-usage-tracker` (private, branch `build`, Python stdlib, `python3 -m unittest discover -s tests`, 109 tests). Same checkout on `ssh gs` at `~/claude-usage-tracker`, which is the cron working tree; keep them in sync with `git pull --rebase --autostash origin build`.
- `~/code/alldonesites` (public site repo `all-done-sites-platform`, Cloudflare Pages). Public page: https://alldonesites.com/claude-usage-tracker/

## Required files to read

- `docs/spike-2026-09.md` (rulings, newest at the bottom: dollar invariant, meter weight, output weight 1.8, probe modes)
- `tracker/probe.py` module docstring (skip / burst / settle modes)
- `bin/probe.sh`, `bin/daily.sh`, `data/prices.json` (`class_weight`, `meter_weight`)
- `~/notes/vault/Projects/Claude Usage Tracker.md` and `... - Tasks.md` (current state and open tasks)
- Memory: `feedback_delegate_to_dave_account_on_gs.md`

## Background

- A 1-tick probe reading is too noisy (490,713 vs 272,631 tokens per 1% on identical prompts). The 09:39 5-tick Sonnet run gave 467,779 with post-alignment spans of 5, 9, 10, 9, 10 prompts: the first span after alignment looks like meter catch-up, later spans are steady within one prompt. Jonathan's hypothesis that the short span was the start fraction is wrong: the probe already discards prompts before the first tick.
- The meter weights output tokens about 1.8x their list price (output-heavy Fable run 12:06 UTC: $0.589 list per 1% vs $1.081 cache-write-heavy; $1.004 per 1% once weighted). Carried as `class_weight` in `data/prices.json` (commit e5a5786). Per-model `meter_weight` stays 1.0 for Sonnet, Opus and Fable.
- Probe modes `--skip N`, `--burst K --expect-tokens-per-pct X`, `--settle S` merged as c75b002 (built by a headless Claude on gs under Dave's account). Burst fires only with an expectation and only once per measured tick.
- **Running when this handoff was written:** a Sonnet validation on Dave, `--ticks 5 --burst 4 --expect-tokens-per-pct 468000`, launched about 13:20 UTC, log `gs:~/claude-usage-tracker/out/probe-sonnet-burst.log`, pid 1555669. Expected done about 14:10 UTC. It appends to `history/probes.jsonl` on gs but does NOT commit; commit and push the row (`git -c user.name=probe -c user.email=probe@gs commit`).
- gs cron: probe `30 3,15 * * *` (bin/probe.sh, Sonnet, still the old flags: 5 ticks, no skip, no burst), publisher `30 5 * * *` (bin/daily.sh, waits up to 600 s on `.cron.lock`).
- Notify-me email feature: site PR #41 (branch `notify-me`, head 07c8837), KV `NOTIFY_KV`, Pages secrets set, gs daily.sh posts a change once. Blocked: Codex quota exhausted on 2026-09-06 (exit 40). No verdict, no merge. Production endpoints answer 405 until merged, which the daily job treats as a retryable failure.
- Decisions from the brainstorm with Jonathan (2026-09-06): rotate Sonnet, Opus, Fable every 12 h (each model every 36 h); measured tick count decided by the validation run's span spread (2 ticks if spans stay within ±1 prompt, else 3), always with skip 1; a reading more than 15% off the median of the last four triggers an immediate rerun on the same model, two agreeing readings publish, a lone outlier is dropped, Jonathan is notified either way; prose payload for all, plus one output-mode Fable run a month to re-measure the 1.8; per-model prompt size so every tick is about 10 prompts (Fable prose was 3 to 4 prompts per tick, so ±30% per tick). Idea to test for free: start a run at Dave's window reset so the meter is exactly 0.0 and the alignment partial disappears.

## Constraints

- Never print secret values; never merge the public site repo without a Codex CLEAN (`~/bin/codex-verdict.sh --pr N --wait 1500 --report-every 600`, exit 0 only, `@codex review` comment after each push). Site repo is public: no AI attribution in commits or PR bodies.
- Delegate builds to a headless Claude on gs under Dave's account in tmux (recipe in the memory file above and `~/.claude/references/on-demand.md`), never while a probe is running on Dave: wrap the launch in `flock ~/claude-usage-tracker/.cron.lock`. Tell agents the surname is Avis, no sub-agents, no live probes from inside the agent.
- Reply style: caveman lite. Vault/memory stop-gate after every milestone; vault lint must show 0 new findings on the tracker notes.
- Never hand-roll a wait with foreground sleep; use Monitor or background bash on an explicit pid.

## Files allowed to change

`tracker/probe.py`, `tracker/publish.py`, `tracker/detect.py`, `bin/probe.sh`, `bin/daily.sh`, `data/prices.json`, `docs/spike-2026-09.md`, tests, `.prompts/`, the vault project notes.

## Files not allowed to change

`history/probes.jsonl` by hand (only probe rows); `website/` in the site repo except through a reviewed PR.

## Acceptance criteria

1. Validation row read: post-skip span spread reported, measured tick count chosen and recorded in `docs/spike-2026-09.md`.
2. `bin/probe.sh` rotates models by the last probed model in `history/probes.jsonl`, passes `--skip 1 --ticks N --burst K --expect-tokens-per-pct <last published rate for that model>` and a per-model payload size (add `--payload-words` or equivalent so each prompt is about a tenth of a tick for that model).
3. Drift confirm: publisher or probe wrapper reruns once on a >15% deviation and notifies Jonathan (reuse the notify path in daily.sh or a plain email); a lone outlier does not publish.
4. Monthly output-mode Fable run scheduled (cron on gs), row tagged `payload: output`, excluded from the invariant median.
5. Page republished and numbers sane (Sonnet about 290M tokens per window at the 97% cache-read split).
6. Notify-me PR #41 merged via ship-to-main once Codex is clean; `NOTIFY_TOKEN_SECRET` and `NOTIFY_SEND_SECRET` pasted into the secrets store by hand (Jonathan).

## Verification commands

```
python3 -m unittest discover -s tests
ssh gs 'tail -3 ~/claude-usage-tracker/out/probe-sonnet-burst.log'
ssh gs 'cd ~/claude-usage-tracker && python3 -c "import json;r=json.loads(open(\"history/probes.jsonl\").readlines()[-1]);print(r[\"model\"],r[\"prompts\"],r[\"tokens_per_pct\"],[x[\"five_hour\"] for x in r[\"readings\"]])"'
ssh gs 'crontab -l | grep claude-usage'
cd ~/notes/vault && python3 _Agent_System/vault_lint.py . --baseline _Agent_System/lint-baseline.json | grep "Claude Usage"
```

## Unresolved questions

- Whether the first-span anomaly repeats (validation run answers it).
- Input and cache-read class weights are unmeasured (assumed 1.0); Opus is derived, never probed, until the rotation starts.
- Codex quota: when it returns. Jonathan may instead choose to merge #41 on the agent's self-review; that is his call, not the agent's.

## Completion summary

(fill in when done)
