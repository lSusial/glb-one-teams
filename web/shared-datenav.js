// web/shared-datenav.js — 날짜(전일자) 선택기 (공용)
// 백엔드는 archive/YYYY-MM-DD/*.json + archive/dates.json 로 날짜별 스냅샷을 보관하고,
// 각 화면은 ?date= 쿼리를 읽어 해당 아카이브를 불러온다. 이 스크립트는 헤더의
// #datenav 컨테이너에 날짜 드롭다운을 그려서, 화면에서 직접 과거 날짜로 전환하게 한다.
// (2026-09 UI 리디자인 때 빠졌던 날짜 선택 UI 복구 — 백엔드는 그대로였음)
(function () {
  function init() {
    var host = document.getElementById('datenav');
    if (!host) return;
    var P = new URLSearchParams(location.search);
    var EN = P.get('lang') === 'en';
    var cur = P.get('date'); // 없으면 최신
    fetch('archive/dates.json')
      .then(function (r) { return r.json(); })
      .then(function (dates) {
        if (!Array.isArray(dates) || !dates.length) return;
        var latest = dates[0]; // dates.json은 최신순
        var sel = document.createElement('select');
        sel.className = 'dnav-sel';
        sel.setAttribute('aria-label', EN ? 'Select date' : '날짜 선택');
        dates.forEach(function (d, i) {
          var o = document.createElement('option');
          o.value = d;
          o.textContent = d + (i === 0 ? (EN ? '  (latest)' : '  (최신)') : '');
          if ((cur || latest) === d) o.selected = true;
          sel.appendChild(o);
        });
        sel.addEventListener('change', function () {
          var np = new URLSearchParams(location.search);
          if (sel.value === latest) np.delete('date'); else np.set('date', sel.value);
          location.search = np.toString(); // 리로드
        });
        host.appendChild(sel);
      })
      .catch(function () { /* 아카이브 미제공(오프라인 등) → 선택기 미표시 */ });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
