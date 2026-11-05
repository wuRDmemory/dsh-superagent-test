#!/bin/bash
# Daily Feishu document digest wrapper.
# Put this on a cron schedule, e.g.:
#   0 9 * * * /path/to/feishu-doc-collector/run_daily.sh >> /path/to/feishu-doc-collector/cron.log 2>&1
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

# lark-cli must be on PATH. Override with LARK_CLI=/path/to/lark-cli.
LARK="${LARK_CLI:-lark-cli}"
export FEISHU_DAILY_CONFIG="${FEISHU_DAILY_CONFIG:-$DIR/config.json}"
export FEISHU_CHAT_CACHE="${FEISHU_CHAT_CACHE:-$DIR/chats.json}"
export TZ="${TZ:-Asia/Shanghai}"

# This deployment's lark-cli is normally locked to the bot identity. The scan
# needs the user identity, so switch to "off" for the duration and restore the
# previous/default mode afterwards. Set FEISHU_DAILY_MANAGE_STRICT_MODE=0 if
# your lark-cli is not using strict mode.
RESTORE_MODE="${FEISHU_DAILY_RESTORE_MODE:-bot}"
if [ "${FEISHU_DAILY_MANAGE_STRICT_MODE:-1}" = "1" ]; then
  "$LARK" config strict-mode off >/dev/null 2>&1 || true
fi
restore() {
  if [ "${FEISHU_DAILY_MANAGE_STRICT_MODE:-1}" = "1" ]; then
    for _ in 1 2 3 4 5; do
      "$LARK" config strict-mode "$RESTORE_MODE" >/dev/null 2>&1 && return 0
      sleep 2
    done
  fi
  return 0
}
trap restore EXIT INT TERM

python3 "$DIR/daily_docs.py" "$@"
rc=$?
restore
exit $rc
