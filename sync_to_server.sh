#!/usr/bin/env bash
# 맥북에서 수집 후 오라클 서버로 DB 동기화
# 사용법: ./sync_to_server.sh [--collect]
#   --collect  수집 파이프라인 먼저 실행 후 동기화
#   (인수 없음) DB만 동기화

set -e

SSH_KEY="$HOME/workspace/ssh-key-2026-06-25-4.key"
REMOTE="ubuntu@168.107.56.139"
REMOTE_DIR="~/glb-one-teams/data"
LOCAL_DB="$(dirname "$0")/data/news.db"

if [ "$1" = "--collect" ]; then
    echo "▶ 수집 파이프라인 실행..."
    cd "$(dirname "$0")"
    .venv/bin/python main.py run
    echo ""
fi

echo "▶ DB 동기화 중: $LOCAL_DB → $REMOTE:$REMOTE_DIR"
rsync -avz --progress \
    -e "ssh -i $SSH_KEY -p 22" \
    "$LOCAL_DB" \
    "$REMOTE:$REMOTE_DIR/"

echo "✓ 동기화 완료"
