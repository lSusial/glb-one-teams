# GLB News RSS — 메신저 발송(Broadcast) 기획

> 작성: 2026-08-06 세션 | 대상 경로: `/prototype/`
> 관련 문서: [`STATUS.md`](STATUS.md)(파이프라인 현황), [`archive/`](archive/)(구 실행계획)
>
> 본 문서는 **매일 생성되는 거점별 브리핑을 Telegram / Zalo 등 메신저로 자동 발송**하기 위한
> 기획·데이터 설계 정리본입니다. 이번 세션에서는 코드 구현 없이 "무엇을 준비/관리해야 하는가"까지 확정했습니다.

---

## 1. 목표

매일 파이프라인이 생성하는 **거점별 데일리 브리핑**을 Telegram, Zalo 등으로 자동 전송한다.

- 내용: 뉴스 + 키워드 + 링크
- 형태: 간단한 메시지
- 주기: 매일 1회 (기존 06:00 KST 파이프라인 종료 후 이어서 발송)

### 이번 세션에서 확정한 결정

| 항목 | 결정 | 비고 |
|---|---|---|
| 메시지 내용 | **거점별 핵심 브리핑** | 요약·이슈·키워드 중심. 링크는 브리핑에 포함된 기사 사용 |
| 발송 단위 | **거점별 분리 발송** | 국가(cc)별로 각각 메시지 전송 |
| 채널 | 미확정 (데이터 설계 우선) | Telegram이 자연스러운 1순위 (아래 4장) |
| 수신처 | 미확정 | 봇 토큰 + 거점→채팅방 매핑 정해지면 테스트 가능 |

---

## 2. 핵심 발견 — 보낼 콘텐츠 데이터는 이미 다 있다

거점별 분리 발송에 맞게, **`country_briefings` 테이블의 한 행(row)이 곧 한 거점의 하루치 메시지**다.
새로운 수집·분석·가공은 필요 없고, 이 행을 읽어 **포맷만** 하면 된다.

### `country_briefings` 행에 담긴 것 (한 거점 / 하루)

| 컬럼 | 내용 | 메시지에서의 역할 |
|---|---|---|
| `cc` | 거점 코드 (VN, KH, CN…) | 어느 거점 메시지인지 |
| `briefing_date` / `briefing_type` | 날짜 / daily·weekly | 발송 대상 식별 |
| `summary` | AI 요약(문단) | 메시지 본문 상단 |
| `issues` | `[{category, title, detail}]` (3~5개) | 핵심 이슈 목록 |
| `keywords` | `["키워드1", …]` (배열) | 키워드 해시태그/나열 |
| `key_stat` | `{value, label}` | 오늘의 숫자(선택) |
| `source_articles` | **`[{title, link, source, score}]`** | **뉴스 링크 (별도 join 불필요)** |
| `article_count` | 기사 수 | 참고 지표 |

> **중요:** 원했던 "링크"는 `source_articles`에 이미 `{제목, 링크, 출처, 점수}` 형태로 들어 있어
> `articles_raw`와 별도로 join할 필요가 없다. 브리핑 한 행이 자족적(self-contained)이다.

### 실제 예시 (2026-06-21 CN 브리핑)

- `summary`: "…위안화 국제화 가속, 미중 무역 협상 이행, 바이오텍·배터리 지정학 리스크…"
- `issues`: 판다본드 확대, 대미 콩 수입 2,500만 톤, 바이오텍 디커플링 …
- `keywords`: `["판다본드","위안화국제화","미중무역이행","바이오텍디커플링", …]`
- `key_stat`: `{"value":"25GWh+","label":"중국 ESS 기업 해외 수주"}`
- `source_articles`(6건): 각 `{title, link(google news/원문), source, score:4}`

---

## 3. 새로 관리해야 할 데이터 (발송 레이어)

콘텐츠는 관리할 게 없고, **발송을 위해 새로 관리할 데이터는 세 가지**다.

### ① 수신처 매핑 (제일 중요)

거점별로 따로 보내므로 "어느 거점 → 어디로"가 필요하다. 채널까지 합치면 `(거점, 채널) → 대상 ID`.

- 예: `VN → 텔레그램 채널 A`, `KH → 텔레그램 채널 B`, `CN → 텔레그램 채널 C`
- 채널이 늘어나면 같은 거점이 여러 행을 가질 수 있음 (`VN + telegram`, `VN + zalo`)

### ② 채널 인증 토큰 (시크릿)

- Telegram: 봇 토큰 **1개로 전 거점 공용** (수신처만 다르게)
- Zalo: OA access token(1시간) + refresh token(3개월)
- **DB에 넣지 말고 `.env`에 보관** (시크릿 노출 방지)

### ③ 발송 로그

`(거점, 날짜, 채널)` 단위로 보냈는지 기록.

- 중복 발송 방지 (cron이 두 번 돌거나 재실행해도 안전 — 멱등성)
- 실패 재시도 / 추적
- `UNIQUE` 제약을 멱등 키로 사용

---

## 4. 제안 스키마

```sql
-- ① 수신처: 거점 × 채널 → 대상
CREATE TABLE broadcast_targets (
  cc        TEXT,                 -- VN, KH, CN ...
  channel   TEXT,                 -- telegram | zalo
  target_id TEXT,                 -- 텔레그램 chat_id / Zalo 수신자
  language  TEXT DEFAULT 'ko',    -- 거점별 수신 언어(선택)
  enabled   INTEGER DEFAULT 1,    -- 거점/채널별 on/off
  PRIMARY KEY (cc, channel)
);

-- ③ 발송 이력 (UNIQUE = 중복 방지 멱등 키)
CREATE TABLE broadcast_log (
  cc            TEXT,
  briefing_date TEXT,
  briefing_type TEXT,             -- daily | weekly
  channel       TEXT,
  status        TEXT,             -- sent | failed
  sent_at       TEXT,
  error         TEXT,
  UNIQUE (cc, briefing_date, briefing_type, channel)
);
```

```dotenv
# ② 시크릿 — .env
TELEGRAM_BOT_TOKEN=123456:ABC...
# ZALO_OA_ACCESS_TOKEN=...     (나중)
# ZALO_OA_REFRESH_TOKEN=...    (나중)
```

> 초기 MVP에서는 `broadcast_targets`를 DB 테이블 대신 `sources.yaml` 스타일의 설정 파일이나
> 간단한 dict로 시작해도 된다. 채널이 여러 개로 늘어나면 테이블로 승격.

---

## 5. 채널별 현실

| 채널 | 난이도 | 준비물 | 소요 |
|---|---|---|---|
| **Telegram** | 낮음 | @BotFather로 봇 생성 → 토큰 → `sendMessage` API | 오늘 바로 가능 |
| **Zalo** | 높음 | 공식계정(OA) + 사업자 인증 + ZNS 템플릿 **사전 승인** | 며칠~몇 주 |

### Telegram

- @BotFather에서 봇 생성 → 봇 토큰 발급
- `sendMessage` 호출로 끝. HTML/Markdown 서식 + 링크 지원, 무료
- 수신처는 채널/그룹/개인 채팅의 `chat_id`
- 봇 1개로 전 거점 공용, 거점별 `chat_id`만 다르게

### Zalo

- Zalo **Official Account(OA)** + 사업자 인증 필요
- 일방향 발송은 **ZNS(Zalo Notification Service)** — 미리 승인된 **템플릿**만 발송 가능
- access token 유효 1시간 / refresh token 3개월 → 토큰 갱신 로직 필요
- 베트남 대상이 우선이면 유의미하나, 승인 절차 때문에 즉시 시작은 어려움
- 참고: [Zalo For Developers](https://developers.zalo.me/docs), [Zalo Notification Services (ZNS) — Infobip](https://www.infobip.com/docs/zalo)

**권장 순서:** Telegram으로 먼저 파이프라인을 완성해 검증하고, Zalo는 OA·템플릿 승인을 병행 준비.

---

## 6. 아키텍처 (구현 방향)

```
기존 파이프라인 …
  python main.py brief --type daily     # 거점별 브리핑 생성 (완료 단계)
        │
        ▼
  python main.py broadcast              # ★ 신규: DB 브리핑 읽어 거점별 발송
        │
        ├─ broadcast_targets 조회 (enabled=1)
        ├─ 거점별 country_briefings 최신 행 로드
        ├─ 메시지 포맷 (요약 + 이슈 + 키워드 + 링크)
        ├─ 채널 어댑터로 전송  (TelegramSender / ZaloSender …)
        └─ broadcast_log 기록 (중복 방지 / 실패 추적)
```

- **채널 추상화:** 공통 인터페이스 `send(target_id, text) -> bool` 하나 두고
  `TelegramSender`, `ZaloSender`를 갈아끼움. 이후 KakaoTalk·Slack도 같은 틀로 확장.
- **파이프라인 연결:** `run_pipeline.sh` 끝에 `python main.py broadcast` 한 줄 추가 →
  매일 06:00 KST 자동 발송.
- **멱등성:** `broadcast_log`의 `UNIQUE` 키로 이미 보낸 건 스킵.

---

## 7. 주의점 — 브리핑 신선도(Freshness)

거점별로 브리핑 최신 날짜가 **들쭉날쭉**하다. 거점별 분리 발송 시 반드시 정책이 필요하다.

### 거점별 daily 브리핑 보유 현황 (조회 시점 스냅샷)

| 거점 | 최신 날짜 | 보유 일수 |
|---|---|---|
| US | 2026-06-21 | 12 |
| VN | 2026-06-21 | 12 |
| GLOBAL | 2026-06-21 | 12 |
| CN | 2026-06-21 | 11 |
| IN | 2026-06-21 | 11 |
| ID | 2026-06-21 | 10 |
| KH | 2026-06-21 | 10 |
| **JP** | **2026-06-17** | 9 |
| **MM** | **2026-06-16** | 2 |
| **KR** | **2026-06-05** | 3 (KR은 평소 수집 제외 대상) |

> 최신이 6/21에 몰려 있는 건 파이프라인이 6/21 이후로 돌지 않은 영향도 있음(STATUS.md의
> DB 손상 복구 / ANTHROPIC_API_KEY 401 이슈 참조).

### 필요한 발송 정책

- **오늘자 브리핑 없으면 스킵**하거나, "최신 N일자" 라벨을 메시지에 명시
- 안 그러면 빈 메시지 또는 오래된 내용을 그대로 전송하게 됨
- 거점별 `enabled` 플래그로 임시 제외 가능 (예: KR)

---

## 8. 준비물 체크리스트

"그냥 보내기"를 위한 최소 준비물:

- [ ] **ⓐ Telegram 봇 토큰 1개** — @BotFather에서 발급 → `.env`
- [ ] **ⓑ 거점 → 채팅방 매핑** — 각 거점 메시지를 보낼 `chat_id` 확보 (`broadcast_targets`)
- [ ] **ⓒ 발송 로그 테이블** — `broadcast_log` 생성 (중복 방지)

---

## 9. 다음 스텝 (미착수)

- [ ] `broadcast_targets` / `broadcast_log` 테이블 생성 (또는 설정 파일로 시작)
- [ ] `.env`에 `TELEGRAM_BOT_TOKEN` 추가
- [ ] `broadcaster.py` — 채널 추상화 + `TelegramSender` 구현
- [ ] `main.py`에 `broadcast` 서브커맨드 추가 (`--cc`, `--date`, `--type`, `--dry-run` 옵션)
- [ ] 메시지 포맷터 — 요약 + 이슈 + 키워드 + 링크 (Telegram HTML)
- [ ] 신선도 정책 반영 (오늘자 없으면 스킵 / 최신일자 라벨)
- [ ] 개인 채팅으로 `--dry-run` → 실발송 테스트
- [ ] `run_pipeline.sh`에 `python main.py broadcast` 추가
- [ ] (후속) Zalo OA 신청 + ZNS 템플릿 승인 → `ZaloSender` 추가

---

*이 문서는 2026-08-06 세션의 발송 기획 논의를 정리한 것입니다. 구현 시작 시 STATUS.md 파이프라인 섹션과 함께 갱신하세요.*
