# 배포 · 운영

> 통합 문서(2026-09-21). 구 `decision_brief_배포운영`(8/19, 회의용)과 `BROADCAST_발송_기획`(8/6)을 합치고 구현된 부분은 현행화했다. 일일 절차는 §1, 아직 결정되지 않은 사항은 §2·§4.

---

## 1. 일일 운영 절차 (맥북)

```bash
python main.py run                 # fetch → filter → dedup (Google News 때문에 맥북에서만)
python main.py ai --days 2         # prefilter → fulltext → rank → ai-dedup → expand → translate → brief → highlights (Batches, 수 분~10분)
python main.py indicators          # 환율·지수·정책금리·국채 스냅샷
python main.py export              # data/export/*.json + 화면 HTML 생성
./deploy_web.sh                    # export + wrangler pages deploy (= 아래 한 줄)
wrangler pages deploy data/export --project-name kb-global-daily --commit-dirty=true
```

- 주 1회: `python main.py indicators-history`(6개월 주봉 백필, 맥북). 필요 시 `korean-fi` · `personnel` · `backfill-country` 태깅.
- 서버 동기화(`sync_to_server.sh`, `deploy_web.sh` 내 rsync)는 Oracle Cloud SSH 22번 타임아웃으로 **2026-08-24부터 비활성**. 복구되면 `deploy_web.sh`의 주석 블록 해제.
- 평가: `python main.py eval --mode prefilter|ranker` (`eval/`). 품질 회귀: `python -m unittest discover -s tests -v`.
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
관련 미결: 채널 확정(#18), 다국어 확대(#16), 업데이트 주기(#17), 해외법인 접근 인증(#19) — `../STATUS.md` §5.

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

| 결정 | 결론 | 담당 | 기한 |
|---|---|---|---|
| 1. 주 대상 독자 | | | |
| 2. 배포 채널 | | | |
| 3. 운영 인프라 | | | |
