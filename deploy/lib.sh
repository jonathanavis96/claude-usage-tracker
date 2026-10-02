# shellcheck shell=bash
# Shared by deploy/install-schedule.sh (masterrig) and deploy/install-gs.sh (gs).
# Sourced, never run. scripts/deploy.sh may also prepend it to an installer piped over
# ssh, which is why each installer only sources it when cut_install is not yet defined.
#
# cut_install does, in order:
#   1. Preflight on the checkout the cron line will run from (--repo). It REFUSES when
#      the checkout is missing, not on main, has uncommitted changes to tracked files,
#      is mid-rebase/merge, lacks tracker/supervise.py or tracker/health.py at HEAD (the
#      code the line calls), or does not contain --expect SHA. "Contains", not "equals":
#      both hosts commit history to main on their own every 30-60 minutes. A dry run
#      prints the refusal and carries on; a real run exits 3 having changed nothing.
#      Report-only checks: behind origin/main as last fetched, the Kuma push URL file and
#      whether the host reaches Kuma, the alert env file's key names, and the host hook
#      (cut_host_checks: WhatsApp bridge on masterrig, meter units on gs).
#   2. The crontab: the merge below over the current one, and its exact `diff -u`.
#      A `crontab -l` that fails for any reason other than "no crontab for <user>"
#      refuses (exit 3) in every mode, and so does a merge that would drop any line
#      other than the tracker's own: an install never loses another job.
#   3. Dry run: `tracker.health --all` from the checkout, read-only, when it has the
#      code. Real run, only when the crontab differs: back up the current crontab to
#      $CUT_BACKUP_ROOT/<UTC stamp>-<host>/ (crontab.before, crontab.after,
#      manifest.env), check it did not change since it was read, install, read it back.
#   4. Real run: the post-deploy check. One run of the supervisor path with the host's
#      profile (real health checks, real Kuma file, throwaway lock/state/log, alerts
#      printed, command /bin/true), then `tracker.health --all`. It does not run
#      passive.sh or daily.sh: cron does within the hour, and daily.sh mails subscribers.
# A second run changes nothing and makes no backup. deploy/rollback.sh <backup dir>
# undoes one install.
#
# The report goes to stderr; stdout carries only the diff (dry run) or the full
# crontab (--print), so a caller can parse either.
#
# Exit: 0 done, 2 usage, 3 refused, 4 installed but the post-deploy check failed (or
# the read-back differed), 5 the crontab changed between read and install.

CUT_BACKUP_ROOT="${CUT_BACKUP_ROOT:-$HOME/.local/state/claude-usage-tracker/deploy-backups}"
CUT_KUMA_FILE="$HOME/.config/claude-usage-tracker/kuma-push-url"

say() { printf '%s\n' "$*" >&2; }
_short() { printf '%.12s' "$1"; }
_sha() { sha256sum | cut -c1-64; }

# cut_merge CURRENT BEGIN END PATTERN BLOCK: the crontab with the managed block
# replaced (or appended), and every non-comment line containing PATTERN dropped.
cut_merge() {
  local kept
  kept="$(printf '%s\n' "$1" | awk -v b="$2" -v e="$3" -v p="$4" '
    $0 == b {skip=1; next}
    $0 == e {skip=0; next}
    skip {next}
    index($0, p) && $0 !~ /^[[:space:]]*#/ {next}
    {print}' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')"
  printf '%s\n%s\n' "$kept" "$5" | sed '/./,$!d'
}

# cut_read_crontab CMD: the current crontab on stdout. "no crontab for <user>" (exit 1)
# is an empty crontab; any other failure (permission denied, an unreadable spool, a
# crontab that cannot reach its daemon) returns 1 with the reason on stderr, so a
# caller never mistakes an unreadable crontab for an empty one and replaces it.
cut_read_crontab() {
  local cmd="$1" out err rc ef
  ef="$(mktemp)" || { say "cannot create a temp file to read the crontab"; return 1; }
  out="$($cmd -l 2>"$ef")"; rc=$?
  err="$(cat "$ef")"; rm -f "$ef"
  if [ "$rc" = 0 ]; then printf '%s\n' "$out"; return 0; fi
  case "$err" in
    *"no crontab for"*) return 0 ;;
  esac
  say "crontab -l failed (exit $rc): ${err:-no message}"
  return 1
}

# cut_lost_lines CURRENT NEW BEGIN END PATTERN: every non-blank line of CURRENT outside
# the managed block that is not the tracker's own old line (a non-comment line holding
# PATTERN) and is missing from NEW. Empty output means NEW keeps every other job.
cut_lost_lines() {
  awk -v b="$3" -v e="$4" -v p="$5" '
    FNR == NR { have[$0] = 1; next }
    $0 == b { skip = 1; next }
    $0 == e { skip = 0; next }
    skip { next }
    $0 ~ /^[[:space:]]*$/ { next }
    index($0, p) && $0 !~ /^[[:space:]]*#/ { next }
    !($0 in have) { print }' <(printf '%s\n' "$2") <(printf '%s\n' "$1")
}

CUT_REFUSALS=()
_refuse() { CUT_REFUSALS+=("$1"); say "REFUSE: $1"; }
_warn() { say "WARN: $1"; }

cut_preflight() { # REPO EXPECT
  local repo="$1" expect="$2" br dirty gd f ip behind
  export GIT_OPTIONAL_LOCKS=0   # git status must not refresh (write) the index
  say "checkout: $repo"
  if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    _refuse "no git checkout at $repo"; return
  fi
  br="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  dirty="$(git -C "$repo" status --porcelain --untracked-files=no | wc -l)"
  behind="$(git -C "$repo" rev-list --count HEAD..origin/main 2>/dev/null || echo '?')"
  say "  branch $br, HEAD $(git -C "$repo" rev-parse --short=12 HEAD 2>/dev/null), $dirty uncommitted change(s) to tracked files," \
      "$behind commit(s) behind origin/main as last fetched"
  [ "$br" = main ] || _refuse "checkout is on '$br', not main"
  [ "$dirty" = 0 ] || _refuse "checkout has $dirty uncommitted change(s) to tracked files"
  gd="$(git -C "$repo" rev-parse --absolute-git-dir)"; ip=""
  for f in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD; do [ -e "$gd/$f" ] && ip="$ip $f"; done
  [ -z "$ip" ] || _refuse "checkout has an operation in progress:$ip"
  for f in tracker/supervise.py tracker/health.py; do
    git -C "$repo" cat-file -e "HEAD:$f" 2>/dev/null || _refuse "HEAD has no $f: the checkout predates the fixes (git pull first)"
  done
  if [ -n "$expect" ]; then
    if ! git -C "$repo" cat-file -e "$expect^{commit}" 2>/dev/null; then
      _refuse "expected commit $(_short "$expect") is not in this checkout (git pull first)"
    elif ! git -C "$repo" merge-base --is-ancestor "$expect" HEAD; then
      _refuse "HEAD does not contain expected commit $(_short "$expect")"
    else
      say "  contains expected commit $(_short "$expect")"
    fi
  fi
  case "$behind" in 0|'?') ;; *) _warn "checkout is $behind commit(s) behind origin/main as last fetched" ;; esac
}

cut_report_alerting() {
  local origin code keys k
  if [ -f "$CUT_KUMA_FILE" ]; then
    origin="$(tr -d '[:space:]' < "$CUT_KUMA_FILE" | sed -E 's#^(https?://[^/?]+).*#\1#')"
    code="$(curl -sS -m 8 -o /dev/null -w '%{http_code}' "$origin/" 2>/dev/null || echo unreachable)"
    say "kuma push URL: present, sha256 $(tr -d '[:space:]' < "$CUT_KUMA_FILE" | _sha | cut -c1-12)," \
        "mode $(stat -c %a "$CUT_KUMA_FILE"), $origin answers $code"
    [ "$(stat -c %a "$CUT_KUMA_FILE")" = 600 ] || _warn "$CUT_KUMA_FILE should be mode 600"
    case "$code" in 2??|3??) ;; *) _warn "this host cannot reach Kuma at $origin" ;; esac
  else
    say "kuma push URL: absent ($CUT_KUMA_FILE): no dead-man ping; incidents go to WhatsApp/email only"
  fi
  if [ -r "$HOME/.claude-usage-notify.env" ]; then
    keys=" $(sed -n 's/^\([A-Z_][A-Z0-9_]*\)=.*/\1/p' "$HOME/.claude-usage-notify.env" | sort -u | tr '\n' ' ')"
    say "alert env ~/.claude-usage-notify.env keys:$keys"
    for k in NOTIFY_SEND_SECRET NOTIFY_ALERT_TO; do
      case "$keys" in *" $k "*) ;; *) _warn "$k is missing from ~/.claude-usage-notify.env: the email fallback cannot send" ;; esac
    done
  else
    _warn "$HOME/.claude-usage-notify.env missing: the email fallback cannot send"
  fi
}

# cut_install HOSTLABEL REPO LOG EXPECT MODE BEGIN END PATTERN BLOCK PROFILE
#   MODE: dry-run | print | apply | apply-nocheck
cut_install() {
  local host="$1" repo="$2" log="$3" expect="$4" mode="$5" begin="$6" end="$7" pat="$8" block="$9" profile="${10}"
  local crontab_cmd="${CRONTAB:-crontab}" current new cur_sha new_sha b stamp prof
  read -ra prof <<< "$profile"
  local lost
  if ! current="$(cut_read_crontab "$crontab_cmd")"; then
    say "RESULT: cannot read the current crontab; refused, nothing written"; return 3
  fi
  new="$(cut_merge "$current" "$begin" "$end" "$pat" "$block")"
  lost="$(cut_lost_lines "$current" "$new" "$begin" "$end" "$pat")"
  if [ -n "$lost" ]; then
    say "RESULT: the merged crontab would drop these lines; refused, nothing written:"
    printf '%s\n' "$lost" >&2
    return 3
  fi
  if [ "$mode" = print ]; then printf '%s\n' "$new"; return 0; fi

  say "== $host: $([ "$mode" = dry-run ] && echo 'DRY RUN, nothing is changed' || echo APPLY)"
  cut_preflight "$repo" "$expect"
  cut_report_alerting
  declare -F cut_host_checks >/dev/null && cut_host_checks "$repo"

  cur_sha="$(printf '%s\n' "$current" | _sha)"; new_sha="$(printf '%s\n' "$new" | _sha)"
  say "crontab: sha256 $(_short "$cur_sha") now, $(_short "$new_sha") after"
  if [ "$cur_sha" = "$new_sha" ]; then
    say "  already up to date"
  elif [ "$mode" = dry-run ]; then
    diff -u --label "$host crontab (now)" --label "$host crontab (after install)" \
      <(printf '%s\n' "$current") <(printf '%s\n' "$new")
  fi

  if [ "$mode" = dry-run ]; then
    if [ -f "$repo/tracker/health.py" ]; then
      say "health now (read-only): python3 -B -m tracker.health $profile --all"
      ( cd "$repo" && PYTHONDONTWRITEBYTECODE=1 python3 -B -m tracker.health "${prof[@]}" --all ) >&2
      say "  ('run:' fails until the first supervised run writes its state file)"
    else
      say "health: $repo has no tracker/health.py yet; checked after the pull"
    fi
    [ "$cur_sha" = "$new_sha" ] || say "a real run would back up the crontab to $CUT_BACKUP_ROOT/<UTC stamp>-$host/ first"
    if [ "${#CUT_REFUSALS[@]}" -gt 0 ]; then say "RESULT: a real run would REFUSE (${#CUT_REFUSALS[@]} reason(s) above)"
    else say "RESULT: a real run would proceed"; fi
    return 0
  fi

  if [ "${#CUT_REFUSALS[@]}" -gt 0 ]; then
    say "RESULT: refused, nothing changed"; return 3
  fi
  mkdir -p "$(dirname "$log")"
  if [ "$cur_sha" = "$new_sha" ]; then
    echo "schedule already up to date"
  else
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    b="$CUT_BACKUP_ROOT/$stamp-$host"
    ( umask 077 && mkdir -p "$b" \
      && printf '%s\n' "$current" > "$b/crontab.before" \
      && printf '%s\n' "$new" > "$b/crontab.after" \
      && printf 'host=%s\nrepo=%s\nhead=%s\nexpect=%s\nbefore_sha=%s\nafter_sha=%s\n' "$host" "$repo" \
           "$(git -C "$repo" rev-parse HEAD)" "$expect" "$cur_sha" "$new_sha" > "$b/manifest.env" ) \
      || { say "RESULT: cannot write the backup to $b, nothing changed"; return 3; }
    say "backup: $b"
    local again
    if ! again="$(cut_read_crontab "$crontab_cmd")"; then
      say "RESULT: cannot re-read the crontab before writing; nothing written"; return 3
    fi
    if [ "$again" != "$current" ]; then
      say "RESULT: the crontab changed since it was read; nothing written. Run again."; return 5
    fi
    printf '%s\n' "$new" | $crontab_cmd - || { say "RESULT: crontab install failed"; return 4; }
    if [ "$(cut_read_crontab "$crontab_cmd" 2>/dev/null)" != "$new" ]; then
      say "RESULT: the crontab read back differs from what was written; undo with deploy/rollback.sh $b --apply"
      return 4
    fi
    echo "schedule installed"
    say "  undo: deploy/rollback.sh $b --apply"
  fi
  [ "$mode" = apply-nocheck ] && return 0
  cut_post_check "$repo" "$profile"
}

cut_post_check() { # REPO PROFILE
  local repo="$1" profile="$2" t rc=0 st prof
  read -ra prof <<< "$profile"
  say "post-deploy check: one supervisor run of /bin/true with the real health checks and Kuma file"
  t="$(mktemp -d)"
  ( cd "$repo" && /usr/bin/python3 -m tracker.supervise "${prof[@]}" --dry-run --lock "$t/smoke.lock" \
      --state "$t/state.json" --log "$t/smoke.log" -- /bin/true ) >&2 || rc=$?
  st="$(/usr/bin/python3 -c 'import json,sys
s = json.load(open(sys.argv[1]))
print(" ".join(f"{k}={s.get(k)!r}" for k in ("last_exit", "last_health", "last_ping")))' "$t/state.json" 2>&1)"
  rm -rf "$t"
  say "  supervisor exit $rc; $st"
  say "health: python3 -m tracker.health $profile --all"
  ( cd "$repo" && /usr/bin/python3 -m tracker.health "${prof[@]}" --all ) >&2 \
    || say "  not all ok yet: 'run:' clears after the first scheduled run"
  if [ "$rc" != 0 ]; then say "RESULT: post-deploy check FAILED (supervisor exit $rc)"; return 4; fi
  case "$st" in *"last_ping='failed"*) say "RESULT: post-deploy check FAILED (Kuma ping)"; return 4 ;; esac
  say "RESULT: installed and checked"
}
