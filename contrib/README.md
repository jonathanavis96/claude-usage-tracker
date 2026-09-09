# Contribute a meter sample

`sample.py` reads your own Claude Code usage meter and token counts from your own
transcripts, prints exactly what it would send, and posts one sample to
[alldonesites.com/claude-usage-tracker](https://alldonesites.com/claude-usage-tracker)
only when you say so. It spends none of your allowance: it sends no prompts to
any model. Python 3, standard library only, one file, no install.

The point: your tokens since the window started, over your meter percent right
now, is tokens per 1% under your real mix of models. Enough people on each plan
give the page a measured figure for Pro and Max 5x rather than an assumed one,
and a second sample from you a week later gives the five-hour-windows-per-week
ratio for your plan too.

## What it sends

| Field | Source |
|---|---|
| `contributor_id` | random UUID made on the first run, stored in `~/.claude-usage-contrib.json` |
| `plan` | the usage endpoint if it exposes it, else `--plan pro|max5|max20` asked once and stored in the same file |
| `plan_source` | `endpoint`, `stored` or `flag`, so the server knows which |
| `ts` | your machine's clock, UTC |
| `five_hour` | `utilization` percent and `resets_at`, from `https://api.anthropic.com/api/oauth/usage` with your existing Claude Code login (the same call Claude Code makes for its own `/usage`) |
| `seven_day` | same |
| `tokens_since_five_hour_reset` | per model and class (`input`, `output`, `cache_read`, `cache_write`), summed from the `usage` field of assistant turns in `~/.claude/projects/**/*.jsonl` since the current five-hour window started. Counts only. |
| `tokens_since_seven_day_reset` | same, since the current seven-day window started |
| `client_version` | the script's version string |

Never sent: prompt text, file paths, project names, session ids, account ids,
email, or your OAuth token (it is used for the one usage call and nothing
else). The body is under 2 KB. `tests/test_contrib_sample.py` asserts, against
a fake transcript full of paths, session ids, a prompt and an email address,
that none of it reaches the serialised body.

## Path 1: one-off, from your own Claude Code

Paste this into Claude Code on your machine:

> Run `python3 -c "$(curl -fsSL https://raw.githubusercontent.com/jonathanavis96/claude-usage-tracker/main/contrib/sample.py)" --print`, show me the JSON it prints, explain each field, and post it only if I say yes.

The script prints the body, a one-line explanation of each field, and the
same body again as one compact line starting `CUT1:`. Then it asks
`Send? [y/N]`. Two ways to send, pick one:

1. **Type `approve` (or yes).** Claude runs the script again with `--yes`, which
   posts the body from your machine and prints the HTTP status. The response
   carries `me_url`, your personal page (`/claude-usage-tracker/me/<contributor id>`),
   which draws your own tokens per 1% against the fleet median.
2. **Paste the `CUT1:` line** into the box on the tracker page. The page decodes
   it (it is url-safe base64 of the minified JSON, nothing more), shows the
   fields again, and posts it. Nothing on your machine talks to the site at all.

Both carry the same body to the same endpoint. Nothing is left behind except
`~/.claude-usage-contrib.json` holding your contributor id and plan.

If the usage endpoint does not expose your plan, the first run stops and asks
for `--plan pro`, `--plan max5` or `--plan max20`. It is stored, so later runs
never need it again.

Useful flags:

```
--print      show the body and ask (default on the first run)
--dry-run    never send
--compact    print only the CUT1: line, never send
--yes        send without asking (for cron)
--endpoint   where to post (default https://alldonesites.com/api/contribute)
--claude-dir your Claude Code config dir, if not $CLAUDE_CONFIG_DIR or ~/.claude
--id-file    where to keep the contributor id (default ~/.claude-usage-contrib.json)
```

## Path 2: continuous, every 30 minutes

A continuous contributor's personal page becomes a line rather than a point,
so "did my meter change, or everyone's?" is answerable from your own page.
Paste this into Claude Code:

> Clone `https://github.com/jonathanavis96/claude-usage-tracker` into `~/claude-usage-tracker`, read `contrib/README.md`, then run `python3 ~/claude-usage-tracker/contrib/sample.py --print` and show me the JSON it prints with an explanation of each field. Do not send anything and do not install anything until I type `approve`. When I do, install the 30-minute schedule from the README for my operating system and show me the line you added.

The one-off prompt above is the spec's wording verbatim; this continuous
prompt is written to the spec's description of it (clone, read this README,
print once, wait for `approve`, only then schedule).

### Linux (cron)

```
*/30 * * * * python3 $HOME/claude-usage-tracker/contrib/sample.py --yes >> $HOME/.claude-usage-contrib.log 2>&1
```

Add it with `crontab -e`. Cron does not read your shell profile: if Claude
Code uses a non-default config dir, add `CLAUDE_CONFIG_DIR=/path/to/dir` on a
line above, or pass `--claude-dir`.

### macOS (launchd)

Save as `~/Library/LaunchAgents/com.alldonesites.claude-usage-contrib.plist`,
replacing `YOURNAME`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.alldonesites.claude-usage-contrib</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOURNAME/claude-usage-tracker/contrib/sample.py</string>
    <string>--yes</string>
  </array>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/YOURNAME/.claude-usage-contrib.log</string>
  <key>StandardErrorPath</key><string>/Users/YOURNAME/.claude-usage-contrib.log</string>
</dict>
</plist>
```

Then:

```
launchctl load ~/Library/LaunchAgents/com.alldonesites.claude-usage-contrib.plist
```

On macOS Claude Code keeps its login in the keychain rather than a file; the
script reads it with `security find-generic-password`, the same way Claude
Code does. The first launchd run may prompt for keychain access once.

### Uninstall

Linux:

```
crontab -l | grep -v 'contrib/sample.py' | crontab -
```

macOS:

```
launchctl unload ~/Library/LaunchAgents/com.alldonesites.claude-usage-contrib.plist && rm ~/Library/LaunchAgents/com.alldonesites.claude-usage-contrib.plist
```

Either, to forget the contributor id as well (your personal page stops
accruing and a later run starts a fresh id):

```
rm ~/.claude-usage-contrib.json ~/.claude-usage-contrib.log
```

## Check for yourself

Nothing here asks you to trust a description. The whole client is
[`contrib/sample.py`](sample.py): about 300 lines, standard library only,
no imports from the rest of this repo. Read it, or paste it to your own
Claude and ask what leaves the machine. The only outbound calls are the one
GET to the usage endpoint with your token and, only after you approve, one
POST of the printed body with a 10 second timeout and no retries.
`--dry-run` runs everything but the POST, and the test file patches
`urlopen` to fail so a dry run that opened a socket would fail the suite.
