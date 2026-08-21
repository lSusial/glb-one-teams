"""
GNews API 볼륨/관련성 테스트 (v2) — "출처 무관, 나라별 뉴스를 얼마나 모으나"

v1(gnews_coverage_test.py)은 sources.yaml의 특정 매체가 잡히는지 봤음(매체지정 관점).
v2는 관점을 바꿈: 매체 무관하게 각 나라 관련 뉴스를 내용 검색으로 얼마나 끌어오나.

방법:
  - country= 필터 제거. 대신 q에 나라이름 + KB 관심 키워드(금융·경제)로 내용 검색.
  - 국가당 1회 호출만(429 회피). 6초 간격 + 429 시 긴 백오프.
  - 지표: totalArticles(전체 매칭 수 = 볼륨) + 샘플 제목 5개(관련성 눈으로 판단).

사용법:
  export GNEWS_API_KEY=발급받은_키
  python3 eval/gnews_volume_test.py
"""
from __future__ import annotations
import os, sys, json, time, ssl, urllib.parse, urllib.request

API_KEY = os.environ.get("GNEWS_API_KEY", "").strip()
BASE = "https://gnews.io/api/v4"
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

# 나라별 검색어: 나라이름/지명 + KB 관심(경제·금융) 키워드.
# GNews q는 AND/OR/"구문" 연산자 지원.
KB_QUERIES = {
    "영국":     '"United Kingdom" OR "Britain" OR "London"',
    "미국":     '"United States" OR "U.S. economy" OR "Federal Reserve"',
    "홍콩":     '"Hong Kong"',
    "중국":     '"China" AND (economy OR bank OR yuan OR trade)',
    "일본":     '"Japan" AND (economy OR bank OR yen OR "Bank of Japan")',
    "싱가포르": '"Singapore"',
    "인도":     '"India" AND (economy OR bank OR rupee OR RBI)',
    "베트남":   '"Vietnam"',
    "미얀마":   '"Myanmar" OR "Burma"',
    "인도네시아":'"Indonesia"',
    "캄보디아": '"Cambodia"',
}
# 참고용: 순수 볼륨(키워드 없이 나라이름만)도 같이 보고 싶으면 True
FINANCE_NARROW = True
FINANCE = ' AND (economy OR bank OR finance OR business OR investment OR trade)'


def _get(path: str, params: dict) -> dict:
    params = {**params, "apikey": API_KEY}
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "kb-gnews-vol/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
        return json.loads(r.read().decode())


def main():
    if not API_KEY:
        print("ERROR: export GNEWS_API_KEY=... 필요 (https://gnews.io/register)")
        sys.exit(1)

    print("=" * 70)
    print("GNews 볼륨/관련성 테스트 (v2) — 출처 무관, 나라별 뉴스 검색")
    print("=" * 70)

    rows = []
    for kor, base_q in KB_QUERIES.items():
        q = base_q + (FINANCE if FINANCE_NARROW else "")
        total, samples, err = 0, [], None
        for attempt in range(4):
            try:
                r = _get("search", {"q": q, "lang": "en", "max": 10, "sortby": "publishedAt"})
                total = r.get("totalArticles", 0)
                for a in r.get("articles", [])[:5]:
                    src = (a.get("source") or {}).get("name", "?")
                    samples.append(f"[{src}] {a.get('title','')[:60]}")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    err = "429 rate limit"
                    time.sleep(10 * (attempt + 1))  # 긴 백오프
                    continue
                err = f"HTTP {e.code}"; break
            except Exception as e:
                err = str(e); break

        rows.append((kor, total, samples, err))
        flag = "✅" if total >= 100 else ("△" if total >= 20 else "❌")
        print(f"\n{flag} {kor} — 전체 매칭: {total:,}건" + (f"  ⚠{err}" if err else ""))
        for s in samples:
            print(f"     · {s}")
        time.sleep(6)  # 429 회피: 호출 간 충분한 간격

    print("\n" + "=" * 70)
    print("볼륨 요약 (전체 매칭 기사 수)")
    print("=" * 70)
    for kor, total, _, err in sorted(rows, key=lambda x: -x[1]):
        flag = "✅" if total >= 100 else ("△" if total >= 20 else "❌")
        print(f"  {flag} {kor:<6} {total:>8,}건" + (f"   ⚠{err}" if err else ""))
    print("\n  판정: ✅≥100(충분)  △20~99(빈약)  ❌<20(부적합)")
    print("  ※ 샘플 제목이 실제로 그 나라 뉴스인지 함께 확인할 것(관련성).")
    print("  ※ 무료티어는 12h 지연·max10 반환이나 totalArticles는 전체 수 반영.")

    out = [{"country": k, "total": t, "samples": s, "error": e} for k, t, s, e in rows]
    json.dump(out, open("eval/gnews_volume_result.json", "w"), ensure_ascii=False, indent=2)
    print("\n결과 저장: eval/gnews_volume_result.json")


if __name__ == "__main__":
    import urllib.error
    main()
