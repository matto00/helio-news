#!/usr/bin/env bash
# Re-suspend after a timer-triggered news run — but ONLY if the machine woke up
# *for* this run. If you were already using the box when the timer fired, this
# does nothing and leaves you alone.
#
# How it knows: systemd-suspend.service stays active for the whole suspend and
# exits on resume, so its ActiveExitTimestamp is "when we last woke up". If that
# was moments ago, the RTC alarm woke us for this job → go back to sleep. If the
# machine never suspended this boot (or resumed long ago), someone is using it.
#
# Invoked from news.service as `ExecStopPost=+…` — the `+` runs it as root, so
# `systemctl suspend` needs no polkit/active-session. ExecStopPost (not Post)
# so a failed run still re-suspends instead of idling all day at ~70 W.
set -euo pipefail

RESUME_WINDOW=${RESUME_WINDOW:-900}   # seconds; 15 min

resumed_at=$(systemctl show systemd-suspend.service -p ActiveExitTimestamp --value 2>/dev/null || true)

if [ -z "${resumed_at}" ]; then
  echo "news: no suspend/resume this boot — machine is in use, staying awake."
  exit 0
fi

resumed_epoch=$(date -d "${resumed_at}" +%s 2>/dev/null || echo 0)
if [ "${resumed_epoch}" -le 0 ]; then
  echo "news: could not parse resume time (${resumed_at}) — staying awake."
  exit 0
fi

since=$(( $(date +%s) - resumed_epoch ))

if [ "${since}" -lt "${RESUME_WINDOW}" ]; then
  echo "news: resumed ${since}s ago → woke for this run; suspending again."
  systemctl suspend
else
  echo "news: resumed ${since}s ago → machine in use; staying awake."
fi
