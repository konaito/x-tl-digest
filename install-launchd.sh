#!/bin/sh
# launchd に登録し直す。plist を変えたときもこれを実行する。
# 登録するジョブは2本ある。
#   送信の回:     launchd/x-tl-digest.plist          ラベルは $LABEL
#   取得だけの回: launchd/x-tl-digest-collect.plist  ラベルは $LABEL-collect
# plist の __LABEL__ を $LABEL に、__ROOT__ をこのスクリプトの置き場所に、__HOME__ をホームディレクトリに置き換えて登録する。
# $LABEL の既定は local.x-tl-digest で、環境変数 LABEL で変えられる。
set -eu
LABEL="${LABEL:-local.x-tl-digest}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="gui/$(id -u)"
mkdir -p "$ROOT/logs"

install_job() {
  src="$ROOT/launchd/$1.plist"
  label="$2"
  dst="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  sed -e "s#__LABEL__#$LABEL#g" -e "s#__ROOT__#$ROOT#g" -e "s#__HOME__#$HOME#g" "$src" > "$dst"
  plutil -lint "$dst"
  launchctl bootstrap "$DOMAIN" "$dst"
}

install_job x-tl-digest "$LABEL"
install_job x-tl-digest-collect "$LABEL-collect"

# 2本とも登録できたかを表示する。片方が見つからなくても、もう片方は表示してから失敗で終わる。
status=0
for label in "$LABEL" "$LABEL-collect"; do
  echo "== $label"
  launchctl print "$DOMAIN/$label" | grep -E 'state|path =' || {
    echo "登録を確かめられない: $label" >&2
    status=1
  }
done
exit "$status"
