# 배포 · 운영

> 통합 문서(2026-09-21). 전체 단계별 로직과 실패 처리는 `pipeline_process.md`를 참조한다. 일일 절차는 §1, 아직 결정되지 않은 사항은 §2·§4.

---

## 1. 일일 운영 절차 (맥북)

```bash
python main.py run                 # fetch → filter → dedup (Google News 때문에 맥북에서만)
python main.py ai --days 2         # prefilter → fulltext → rank → ai-dedup → expand → translate → brief → highlights (Batches, 수 분~10분)
python main.py indicators          # 환율·지수·정책금리·국채 스냅샷
python main.py export              # data/export/*.json + 화면 HTML 생성
python main.py quality             # 국가·소스 수율, 본문률, 점수 집중, 근거 강도 경보(무료)
python3 eval/display_audit.py      # 표시 감사(읽기 전용 리포트) — 요약 없음·오래된 기사·오묶음·관련링크 무관 등. deploy_web.sh가 export 직후 자동 실행
./deploy_web.sh                    # export + wrangler pages deploy (= 아래 한 줄)
wrangler pages deploy data/export --project-name kb-global-daily --commit-dirty=true
```

- 주 1회: `python main.py indicators-history`(6개월 주봉 백필, 맥북). 필요 시 `korean-fi` · `personnel` · `backfill-country` 태깅.
- 서버 동기화(`sync_to_server.sh`, `deploy_web.sh` 내 rsync)는 Oracle Cloud SSH 22번 타임아웃으로 **2026-08-24부터 비활성**. 복구되면 `deploy_web.sh`의 주석 블록 해제.
- 평가: `python main.py eval --mode prefilter|ranker` (`eval/`). 품질 회귀: `python -m unittest discover -s tests -v`.
- 품질 경보: `python main.py quality --strict`. 결과는 `data/quality/latest.{json,md}`이며 배포 스크립트가 자동 실행한다. 경고는 기록만 하고 `critical`만 배포를 중단한다. 개선 과제 상태는 `docs/news_quality_roadmap.md` 참조.
- **AI 중복판정 신뢰성**: 저장분 소급은 먼저 `python3 main.py dedup-repair --days 1`로 dry-run한 뒤 `--apply --days 1`로 백업·적용한다. 특정 그룹만 고치려면 `--rep-id ARTICLE_ID`를 여러 번 지정할 수 있다. 전체 무범위 적용은 과거 그룹을 대량 변경하므로 피한다. 새 프롬프트 실측은 `python3 eval/eval_dedup_guard.py --labeled-live`(라벨 70쌍) 또는 `--live --days 8`(DB 무변경).
- 물량 병목 주의: `PREFILTER_LIMIT=1600`, `RANK_LIMIT=700`(2026-09-08 상향). 초과분은 `--days 2` 창 밖으로 밀려 영구 미처리되므로 수집량이 늘면 재점검.

## 2. 결정 필요 — 주 대상 독자와 채널

독자가 정해져야 언어·채널·콘텐츠 형태가 따라온다. (회의 후 결정 기록란은 비어 있음)

| 안 | 주 독자 | 언어 | 자연스러운 채널 |
|---|---|---|---|
| 1-A | 한국 본사 임직원 | 한국어 | 이메일 / 카카오 / 텔레그램 |
| 1-B | 해외 거점 현지 간부 | 영어 | 텔레그램 / 왓츠앱 |
| 1-C | 양쪽 | 한·영 | 채널 이원화 (UI는 이미 한·영 토글 지원) |

| 채널 | 건당 단가 | 월 비용(200명·매일) | 규제·제약 | 자동화 |
|---|---|---|---|---|
| 이메일 | ~0원 | 0~2만원 | 느슨 | 쉬움 |
| 텔레그램 | 무료 | 0원 | 거의 없음 | 쉬움(봇 API) |
| 카카오 브랜드메시지 | 23~25원 | 5~14만원 + 대행사 기본료 | 마케팅 동의·야간 제한 | 대행사 API |
| 왓츠앱 Business | 국가별 $0.01~0.12 | 상이 | 템플릿 승인·옵트인 | BSP API |
| Zalo(베트남) | — | — | OA + 사업자 인증 + ZNS 템플릿 사전 승인(수일~수주), 토큰 갱신 필요 | 어려움 |

카카오는 매일 자동 push하려면 유료 브랜드메시지 외 대안이 없다(무료 채널 소식은 수동 발행). 권장 순서는 **Telegram으로 먼저 파이프라인 검증 → Zalo/왓츠앱은 승인 절차를 병행 준비**.
관련 미결: 채널 확정(#18), 업데이트 주기(#17), 해외법인 접근 인증(#19) — `../STATUS.md` §5.

## 3. 브로드캐스트 (Telegram) — 현재 구현

`broadcaster.py` / `python main.py broadcast [--date D] [--cc CC] [--type daily|weekly] [--dry-run]`

- 한 거점의 하루치 `country_briefings` 행(summary·issues·keywords·key_stat·`source_articles`의 링크)이 곧 한 메시지다 — 별도 조인 불필요.
- `broadcast_targets`(거점×채널→대상)와 `broadcast_log`(UNIQUE `(cc,date,type,channel)`로 멱등, 재실행해도 중복 발송 없음) 테이블 구현됨. 시크릿은 `.env`의 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`(DB에 넣지 않음).
- 신선도 정책: 오늘자 브리핑이 없으면 스킵하거나 최신 일자를 라벨로 명시(빈·오래된 메시지 발송 방지).
- 이력: 2026-08-06~19 시범 발송(11개 거점) 기록이 있다. 9/17 기준 정기 발송은 미가동·채널 확정 대기로 정리돼 있으니 **현재 가동 여부는 확인 필요**.
- 후속: 채널 추상화(`send(target_id, text)`)로 Zalo·카카오·Slack 어댑터 확장, `run` 파이프라인 끝에 `broadcast` 연결.

## 4. 결정 필요 — 어디서 자동으로 돌릴 것인가

**병목**: Google News RSS가 클라우드/데이터센터 IP를 503으로 차단하고, 피드의 상당수(2026-08 기준 118피드 중 76개)가 Google News 의존이라 수집이 맥북(가정용 IP)에 묶여 있다. Oracle Cloud는 SSH 타임아웃 이슈.

| 안 | 방식 | 비용 | 무인 | 난이도 |
|---|---|---|---|---|
| A | 맥북 상시 자동화(launchd/cron + `caffeinate`) | 0원 | △(맥북이 켜져 있어야) | 낮음 |
| B | 클라우드 + 레지덴셜 프록시 | 월 수만원 | ✅ | 높음(프록시 IP도 차단 위험) |
| C | Google News → 매체 native RSS 전환 | 0원 | ✅(D와 결합) | 중간, 일회성. 소형 매체는 native RSS 부재 가능 |
| D | GitHub Actions 무료 cron | 0원 | ✅(C 필수 — Actions도 데이터센터 IP) | 낮음 |

권장 로드맵: **단기 A**(수일 내, 정기 자동화 과제 즉시 해소) → **중기 C+D**(맥북 의존 제거) → B는 백업 옵션. 브리프가 매일 05:00 KST 생성으로 확정돼 자동화는 사실상 필수.

**A안 구현 완료(2026-10-06)**: `scripts/daily_pipeline.sh`가 매일 01:00 KST에 `main.py run` → `main.py ai --days 2` → `main.py indicators` → `deploy_web.sh` 순서로 실행. launchd(`com.glbteam.dailypipeline`)가 `caffeinate -s`로 맥북 절전을 막고 트리거한다.

- 설치: `./scripts/install_launchd.sh` (plist를 `~/Library/LaunchAgents/`에 설치하고 `launchctl load`)
- 수동 전체 실행: `./scripts/daily_pipeline.sh`
- 중복 실행 방지: `data/logs/.pipeline.pid` — 전날 실행(배치 큐 지연 등)이 안 끝났으면 건너뜀
- 단계별 실패 시 즉시 중단하고 `scripts/tg_notify.py`로 Telegram 알림 시도(실패해도 파이프라인 자체는 계속 중단 상태로 로그만 남김)
- 백업: 실행마다 `data/backups/news.db.daily-*` 생성, 최근 5개만 보관
- 로그: `data/logs/pipeline-*.log`(파이프라인 전체 출력, 14일 보관), `data/logs/launchd.out/.err`(launchd 자체 로그)
- **알려진 제약(2026-10-06)**: 이 맥북 네트워크에서 Telegram API(`api.telegram.org`)가 TLS Client Hello 직후 연결 리셋 — SNI 기반 차단으로 추정(구글 등 일반 인터넷은 정상). `tg_notify.py`는 그대로 둬서 네트워크가 풀리면 바로 동작하지만, **지금은 실패 알림이 안 가므로 `data/logs/pipeline-*.log`를 직접 확인해야 한다.**

| 결정 | 결론 | 담당 | 기한 |
|---|---|---|---|
| 1. 주 대상 독자 | | | |
| 2. 배포 채널 | | | |
| 3. 운영 인프라 | A안(맥북 launchd) 구현 완료, 가동은 10/6부터 | | |
