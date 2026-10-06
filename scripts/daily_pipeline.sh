#!/usr/bin/env bash
# 일일 전체 파이프라인: 백업 → 수집 → AI 분석 → 지표 → 배포.
# launchd(com.glbteam.dailypipeline)가 매일 호출한다. 수동 실행도 가능:
#   ./scripts/daily_pipeline.sh
# 각 단계 실패 시 즉시 멈추고 Telegram으로 실패 단계·로그 꼬리를 알린다(scripts/tg_notify.py).
set -uo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

LOG_DIR="$DIR/data/logs"
BACKUP_DIR="$DIR/data/backups"
mkdir -p "$LOG_DIR" "$BACKUP_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/pipeline-$STAMP.log"

# 전날 실행이 아직 끝나지 않았으면(배치 큐 지연 등) 겹쳐 돌리지 않고 종료한다.
# macOS 기본 bash에는 flock이 없어 PID 파일로 직접 구현(죽은 프로세스의 묵은 락은 무시).
LOCK_PID_FILE="$DIR/data/logs/.pipeline.pid"
if [ -f "$LOCK_PID_FILE" ]; then
    old_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        echo "이미 실행 중인 파이프라인(PID $old_pid)이 있어 건너뜀 ($(date))" >>"$LOG"
        exit 0
    fi
fi
echo $$ >"$LOCK_PID_FILE"
trap 'rm -f "$LOCK_PID_FILE"' EXIT

notify() {
    .venv/bin/python scripts/tg_notify.py "$1" >>"$LOG" 2>&1 || true
}

run_step() {
    local name="$1"
    shift
    echo "▶ $(date '+%H:%M:%S') $name" | tee -a "$LOG"
    if ! "$@" >>"$LOG" 2>&1; then
        echo "✗ $name 실패" | tee -a "$LOG"
        notify "[glb-one-teams] 일일 파이프라인 실패 — 단계: $name
로그: $LOG
최근 60줄:
$(tail -60 "$LOG")"
        exit 1
    fi
}

# 1) DB 백업(최근 5개만 보관 — 나머지는 자동 삭제)
cp data/news.db "$BACKUP_DIR/news.db.daily-$STAMP" 2>>"$LOG"
ls -1t "$BACKUP_DIR"/news.db.daily-* 2>/dev/null | tail -n +6 | xargs -r rm -f

# 2) 파이프라인 본체
run_step "수집(main.py run)" .venv/bin/python main.py run
run_step "AI 분석(main.py ai --days 2)" .venv/bin/python main.py ai --days 2
run_step "지표(main.py indicators)" .venv/bin/python main.py indicators

# 지표 스파크라인(6개월 주간 추세)은 주 1회만 갱신하면 충분하다(일봉이 아닌 주봉 데이터).
# 월요일(KST)에만 돌려 불필요한 yfinance 호출을 피한다. 2026-10-06: 이 백필이 빠져 있던
# 동안 9/21 이후 15일째 STALE_SPARK가 쌓였던 걸 발견 — 일일 자동화에 편입.
if [ "$(date +%u)" = "1" ]; then
    run_step "지표 히스토리(주간, main.py indicators-history)" .venv/bin/python main.py indicators-history
fi

run_step "배포(deploy_web.sh)" ./deploy_web.sh

echo "✓ $(date '+%Y-%m-%d %H:%M:%S') 전체 완료" | tee -a "$LOG"

# 오래된 파이프라인 로그 정리(최근 14일만 보관)
find "$LOG_DIR" -name 'pipeline-*.log' -mtime +14 -delete 2>/dev/null || true
