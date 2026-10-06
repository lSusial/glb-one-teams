#!/usr/bin/env bash
# 일일 파이프라인 launchd 작업을 설치/갱신한다. 매일 01:00 KST에 수집→AI 분석→지표→배포를 실행.
#   ./scripts/install_launchd.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$DIR/scripts/com.glbteam.dailypipeline.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.glbteam.dailypipeline.plist"
LABEL="com.glbteam.dailypipeline"

mkdir -p "$DIR/data/logs"
chmod +x "$DIR/scripts/daily_pipeline.sh"

sed "s|__REPO_DIR__|$DIR|g" "$PLIST_SRC" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "설치 완료: $PLIST_DST"
echo "매일 01:00(KST)에 $DIR/scripts/daily_pipeline.sh 실행"
echo
echo "확인:   launchctl list | grep $LABEL"
echo "즉시 실행 테스트:  launchctl start $LABEL"
echo "제거:   launchctl unload $PLIST_DST && rm $PLIST_DST"
echo "로그:   tail -f $DIR/data/logs/pipeline-*.log"
