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

echo "▶ [1/3] 최신 화면 생성 (export)..."
cd "$DIR"
.venv/bin/python main.py export

echo "▶ [2/3] 화면 파일 업로드 (rsync)..."
rsync -avz --delete \
    -e "ssh -i $SSH_KEY -p 22" \
    data/export/ \
    "$REMOTE:~/glb-one-teams/data/export/"

echo "▶ [3/3] 웹서버(포트 $PORT) 재기동..."
ssh -i "$SSH_KEY" "$REMOTE" \
    "pkill -f 'http.server $PORT' 2>/dev/null; sleep 1; \
     cd ~/glb-one-teams/data/export && \
     nohup python3 -m http.server $PORT >/tmp/glbweb.log 2>&1 & echo '  서버 기동됨'"

echo ""
echo "✓ 완료 →  http://168.107.56.139:$PORT/brief.html"
echo "   접속 안 되면 포트 $PORT 가 오라클 보안목록 + 인스턴스 방화벽에 열려 있는지 확인:"
echo "     서버에서: sudo iptables -I INPUT -p tcp --dport $PORT -j ACCEPT"
echo "     오라클 콘솔: VCN > 보안목록 > 수신규칙에 $PORT/TCP 추가"
