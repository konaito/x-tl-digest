#!/bin/sh
# launchd に登録し直す。plist を変えたときもこれを実行する。
# plist の __ROOT__ をこのスクリプトの置き場所に、__HOME__ をホームディレクトリに置き換えて登録する。
# ラベルは環境変数 LABEL で変えられる。
set -eu
LABEL="${LABEL:-local.x-tl-digest}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/launchd/x-tl-digest.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$ROOT/logs"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sed -e "s#__LABEL__#$LABEL#g" -e "s#__ROOT__#$ROOT#g" -e "s#__HOME__#$HOME#g" "$SRC" > "$DST"
plutil -lint "$DST"
launchctl bootstrap "gui/$(id -u)" "$DST"
launchctl print "gui/$(id -u)/$LABEL" | grep -E 'state|path ='
