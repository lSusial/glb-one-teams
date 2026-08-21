"""
GNews API 커버리지 테스트 — KB 11개국 로컬 매체가 GNews에 잡히는지 검증.

목적: Google News RSS 스크래핑을 GNews API(gnews.io)로 전환 가능한지 판단.
      특히 신흥국(VN/MM/ID/KH) 로컬 매체 커버리지가 관건.

사용법:
  export GNEWS_API_KEY=발급받은_키
  python eval/gnews_coverage_test.py

동작:
  1) 국가별 top-headlines(country=xx) 호출 → 반환된 source 도메인 수집
  2) 국가별 search(금융 키워드 + 국가) 호출 → 도메인 수집
  3) sources.yaml의 KB 지정 로컬 매체가 결과에 등장하는지 대조
  4) 커버리지 리포트 출력 (국가별 잡힌 매체 / 놓친 매체 / 총 기사량)

무료 플랜 제약: 100 req/일, 요청당 10건, 12h 지연 — 테스트엔 충분.
"""
from __future__ import annotations
import os, sys, json, time, ssl, urllib.parse, urllib.request

API_KEY = os.environ.get("GNEWS_API_KEY", "").strip()
BASE = "https://gnews.io/api/v4"

# macOS 등에서 CA 미설치 시 SSL 검증 실패 방지 — certifi 번들 사용.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

# KB 거점 → GNews 국가코드(ISO2, 소문자). GNews는 country 파라미터 지원.
# GLOBAL 제외, 실거점 11개.
KB_COUNTRIES = {
    "gb": "영국", "us": "미국", "hk": "홍콩", "cn": "중국", "jp": "일본",
    "sg": "싱가포르", "in": "인도", "vn": "베트남", "mm": "미얀마",
    "id": "인도네시아", "kh": "캄보디아",
}

# 각 국가에서 "반드시 잡히길 원하는" KB 지정 로컬 매체 도메인 (sources.yaml 기준)
KB_LOCAL = {
    "gb": ["telegraph.co.uk", "cityam.com", "news.sky.com"],
    "us": ["cnbc.com", "washingtonpost.com", "politico.com"],
    "hk": ["scmp.com", "hongkongfp.com", "rthk.hk"],
    "cn": ["globaltimes.cn", "chinadaily.com.cn", "caixinglobal.com", "xinhuanet.com"],
    "jp": ["japantimes.co.jp", "kyodonews.net", "asahi.com", "mainichi.jp", "nhk.or.jp"],
    "sg": ["channelnewsasia.com", "straitstimes.com", "businesstimes.com.sg"],
    "in": ["timesofindia.indiatimes.com", "hindustantimes.com", "thehindu.com",
           "economictimes.indiatimes.com", "livemint.com", "business-standard.com"],
    "vn": ["vnexpress.net", "vietnamnews.vn", "vir.com.vn", "vietnamplus.vn", "vtv.vn"],
    "mm": ["irrawaddy.com", "myanmar-now.org", "mizzima.com", "elevenmyanmar.com", "dvb.no"],
    "id": ["antaranews.com", "thejakartapost.com", "bisnis.com", "kompas.com",
           "tempo.co", "detik.com"],
    "kh": ["phnompenhpost.com", "khmertimeskh.com", "cambodianess.com", "kiripost.com"],
}

# 국가별 검색 키워드 (KB 관심: 금융·경제·은행)
SEARCH_TERMS = "bank OR economy OR finance OR central bank"


def _get(path: str, params: dict) -> dict:
    params = {**params, "apikey": API_KEY}
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "kb-gnews-test/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
        return json.loads(r.read().decode())


def _domains(articles: list) -> list[str]:
    out = []
    for a in articles:
        src = (a.get("source") or {})
        u = src.get("url") or a.get("url") or ""
        host = urllib.parse.urlparse(u).netloc.replace("www.", "")
        if host:
            out.append(host)
    return out


def _match(want: str, got: set[str]) -> bool:
    # want 도메인이 got 중 하나에 포함되면 매칭 (서브도메인 대응)
    return any(want in g or g.endswith(want) or want.endswith(g) for g in got)


def main():
    if not API_KEY:
        print("ERROR: GNEWS_API_KEY 환경변수가 필요합니다.")
        print("  https://gnews.io/register 에서 무료 키 발급 후:")
        print("  export GNEWS_API_KEY=your_key")
        sys.exit(1)

    print("=" * 66)
    print("GNews API 커버리지 테스트 — KB 11개국")
    print("=" * 66)

    grand = {"hit": 0, "miss": 0}
    summary = []

    for cc, kor in KB_COUNTRIES.items():
        got_domains: set[str] = set()
        total_articles = 0
        errors = []

        # 1) top-headlines by country
        for attempt in range(3):
            try:
                r = _get("top-headlines", {"country": cc, "lang": "en", "max": 10})
                arts = r.get("articles", [])
                total_articles += r.get("totalArticles", len(arts))
                got_domains.update(_domains(arts))
                break
            except Exception as e:
                errors.append(f"headlines: {e}")
                time.sleep(2 * (attempt + 1))

        # 2) search by country + finance terms
        for attempt in range(3):
            try:
                r = _get("search", {"q": SEARCH_TERMS, "country": cc, "lang": "en", "max": 10})
                arts = r.get("articles", [])
                got_domains.update(_domains(arts))
                break
            except Exception as e:
                errors.append(f"search: {e}")
                time.sleep(2 * (attempt + 1))

        # 3) KB 지정 로컬 매체 대조
        want = KB_LOCAL.get(cc, [])
        hits = [w for w in want if _match(w, got_domains)]
        miss = [w for w in want if not _match(w, got_domains)]
        grand["hit"] += len(hits)
        grand["miss"] += len(miss)

        pct = int(100 * len(hits) / len(want)) if want else 0
        summary.append((cc, kor, len(hits), len(want), pct, miss, sorted(got_domains), errors))

        print(f"\n[{cc.upper()}] {kor}  — KB매체 {len(hits)}/{len(want)} 커버 ({pct}%)")
        print(f"   GNews가 반환한 도메인({len(got_domains)}): {', '.join(sorted(got_domains)) or '(없음)'}")
        if miss:
            print(f"   ❌ 놓친 KB매체: {', '.join(miss)}")
        if errors:
            print(f"   ⚠ 오류: {errors}")
        time.sleep(1)  # rate 완화

    # 종합
    print("\n" + "=" * 66)
    print("종합 커버리지")
    print("=" * 66)
    for cc, kor, h, w, pct, miss, _, _ in summary:
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        print(f"  {cc.upper():>2} {kor:<6} {bar} {h}/{w} ({pct}%)")
    tot = grand["hit"] + grand["miss"]
    print(f"\n  전체 KB 로컬매체 커버리지: {grand['hit']}/{tot} "
          f"({int(100*grand['hit']/tot) if tot else 0}%)")
    print("\n  판정 기준:")
    print("   - 80%+ : GNews 전면 전환 유력")
    print("   - 50~80%: 하이브리드(메이저 GNews + 로컬 Google News 유지)")
    print("   - <50% : GNews 부적합, 현 구조 유지 또는 프록시")

    # JSON 저장
    out = [{"cc": cc, "country": kor, "hit": h, "want": w, "pct": pct,
            "missed": miss, "returned": doms}
           for cc, kor, h, w, pct, miss, doms, _ in summary]
    with open("eval/gnews_coverage_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n결과 저장: eval/gnews_coverage_result.json")


if __name__ == "__main__":
    main()
