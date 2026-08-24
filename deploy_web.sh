#!/usr/bin/env bash
# 맥북에서 정적 화면(data/export)을 오라클 서버에 올리고 웹서버로 띄운다.
#   사용법: ./deploy_web.sh [PORT]      (기본 8080)
# 화면은 자기완결 HTML(데이터 임베드)이라 서버는 파일만 서빙하면 된다.
# 주의: 접속하려면 오라클 보안목록(Ingress)과 인스턴스 방화벽에 해당 포트가 열려 있어야 함.
set -e

SSH_KEY="$HOME/workspace/ssh-key-2026-06-25-4.key"
REMOTE="ubuntu@168.107.56.139"
PORT="${1:-8080}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "▶ [1/2] 최신 화면 생성 (export)..."
cd "$DIR"
.venv/bin/python main.py export

# ── Oracle Cloud 동기화 임시 비활성화 (SSH 22번 포트 타임아웃, 2026-08-24) ──
# 복구되면 아래 두 블록 주석 해제.
# echo "▶ [2/4] 화면 파일 업로드 (rsync)..."
# rsync -avz --delete \
#     -e "ssh -i $SSH_KEY -p 22" \
#     data/export/ \
#     "$REMOTE:~/glb-one-teams/data/export/"
#
# echo "▶ [3/4] 웹서버(포트 $PORT) 재기동..."
# ssh -i "$SSH_KEY" "$REMOTE" bash -s <<REMOTE_EOF
# pkill -f 'http.server $PORT' 2>/dev/null || true
# sleep 1
# cd ~/glb-one-teams/data/export
# nohup python3 -m http.server $PORT >/tmp/glbweb.log 2>&1 &
# echo '  서버 기동됨'
# REMOTE_EOF

echo "▶ [2/2] Cloudflare Pages 배포..."
wrangler pages deploy data/export --project-name kb-global-daily --commit-dirty=true 2>&1 | tail -3

echo ""
echo "✓ 완료 (Oracle 동기화는 스킵됨 — SSH 복구 후 deploy_web.sh 주석 해제 필요)"
echo "   Cloudflare    →  https://kb-global-daily.pages.dev"
