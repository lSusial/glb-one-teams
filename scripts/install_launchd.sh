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

# launchd가 ~/Documents 안의 스크립트를 직접 실행하면 그 스크립트가 띄운 Python은 전체 디스크 접근
# 권한이 있어도 막힌다(realpath: Operation not permitted, 2026-10-22 재현). ~/Documents 밖의
# 실행기가 스크립트를 bash 인자로 넘기면 통과한다.
LAUNCHER_DIR="$HOME/Library/Application Support/glb-one-teams"
LAUNCHER="$LAUNCHER_DIR/daily_launcher.sh"
mkdir -p "$LAUNCHER_DIR"
printf '#!/bin/bash\nexec /bin/bash "%s/scripts/daily_pipeline.sh" "$@"\n' "$DIR" > "$LAUNCHER"
chmod +x "$LAUNCHER"

sed -e "s|__LAUNCHER__|$LAUNCHER|g" -e "s|__REPO_DIR__|$DIR|g" "$PLIST_SRC" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "설치 완료: $PLIST_DST"
echo "매일 01:00(KST)에 $LAUNCHER → $DIR/scripts/daily_pipeline.sh 실행"
echo
echo "확인:   launchctl list | grep $LABEL"
echo "즉시 실행 테스트:  launchctl start $LABEL"
echo "제거:   launchctl unload $PLIST_DST && rm $PLIST_DST"
echo "로그:   tail -f $DIR/data/logs/pipeline-*.log"
