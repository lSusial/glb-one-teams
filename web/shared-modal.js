// web/shared-modal.js — 기사 상세 모달 공용 컴포넌트 (2026-08-27)
// 브리프(brief.html)·현지언론(countries.html)·모니터링(topics.html) 공통 사용.
// 각 페이지는 이 파일을 <script src="shared-modal.js"></script>로 불러온 뒤
// openArticleModal(article)을 호출하기만 하면 된다 — 모달 DOM·스타일은 최초
// 호출 시 이 스크립트가 알아서 만든다(페이지마다 중복 구현 금지).
//
// article 필드(전부 선택):
//   t                         제목
//   k / k_en                  KB 시사점(kb_implication/kb_implication_en도 허용)
//   q / q_en                  요약(기본)
//   expanded_summary / _en    있으면 q보다 우선 사용(10~20줄 다출처 종합용 — 아직 데이터 없음, 폴백 구조만)
//   rl / source_links         관련 기사 배열 [{t,u,src}] — 없으면 u 1건으로 폴백
//   u, src, d                 원문 링크·출처·날짜
//   c                         주제 배지(공백구분 ui키: economy/finance/digital/esg/risk/geo/reg/deal/incident)
//   category                  단일 카테고리 라벨(c가 없을 때, 브리프 핵심용)
(function () {
  const EN = new URLSearchParams(location.search).get('lang') === 'en';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const TAG_KO = { economy: '경제', finance: '금융', digital: '디지털', esg: 'ESG', risk: '리스크', geo: '지정학',
                    reg: '규제', deal: '거래·투자', incident: '사건사고' };
  const TAG_EN = { economy: 'Economy', finance: 'Banking', digital: 'Digital', esg: 'ESG', risk: 'Risk', geo: 'Geopolitics',
                    reg: 'Regulation', deal: 'Deals', incident: 'Incidents' };
  const TAG_COLOR = { economy: '#2b5f9e', finance: '#2f7d4f', digital: '#6a3fb5', esg: '#3a8a6a', risk: '#b23b3b', geo: '#7a5230',
                       reg: '#b23b3b', deal: '#2f7d4f', incident: '#7a5230',
                       // 브리프 "오늘의 글로벌 핵심"(daily_highlights.category, 단일값·한/영 라벨 그대로 표시)용
                       '금리': '#2b5f9e', 'FX': '#1f7a6c', '규제': '#b23b3b', '시장': '#2f7d4f', '디지털': '#6a3fb5', '지정학': '#7a5230',
                       'Rates': '#2b5f9e', 'Markets': '#2f7d4f', 'Regulation': '#b23b3b', 'Digital': '#6a3fb5', 'Geopolitics': '#7a5230' };

  function ensureDom() {
    if (!document.getElementById('sharedModalStyle')) {
      const style = document.createElement('style');
      style.id = 'sharedModalStyle';
      style.textContent = `
        #sharedArtOv{position:fixed;inset:0;background:rgba(20,18,15,.55);display:none;align-items:center;justify-content:center;padding:20px;z-index:9999}
        #sharedArtOv.on{display:flex}
        .sh-modal{background:#fff;border-radius:14px;max-width:640px;width:100%;max-height:86vh;overflow:auto;padding:22px 24px;position:relative;
          font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Malgun Gothic","Apple SD Gothic Neo",sans-serif;color:#2b2926;font-size:14px}
        .sh-modal .sh-x{position:absolute;top:14px;right:16px;cursor:pointer;color:#aaa;font-size:22px;line-height:1;border:none;background:none}
        .sh-modal .sh-meta{font-size:11px;color:#7a746c;margin-bottom:6px}
        .sh-modal .sh-cat{display:inline-block;font-size:10.5px;font-weight:800;color:#fff;border-radius:20px;padding:2px 9px;margin-right:5px}
        .sh-modal h2{font-size:17px;margin:8px 0 8px;line-height:1.4}
        .sh-modal .sh-imp{background:#fff8e6;border-left:3px solid #FFBC00;padding:8px 12px;border-radius:0 8px 8px 0;font-size:12.5px;margin:10px 0 14px;line-height:1.5}
        .sh-modal .sh-lb{font-size:11px;font-weight:800;color:#7a746c;margin:14px 0 6px}
        .sh-modal .sh-sum{font-size:13px;line-height:1.7;color:#2a2c32}
        .sh-modal .sh-lk{display:block;border:1px solid #e8e4dd;border-radius:8px;padding:9px 11px;margin-top:7px;text-decoration:none;color:inherit}
        .sh-modal .sh-lk .sh-lks{font-size:10.5px;color:#7a746c}
        .sh-modal .sh-lk .sh-lkt{font-size:12.5px;font-weight:600;margin-top:1px;color:#2b2926}
        .sh-empty{color:#7a746c;font-size:12px}
        @media(max-width:520px){ .sh-modal{padding:16px} }
      `;
      document.head.appendChild(style);
    }
    if (document.getElementById('sharedArtOv')) return;
    const ov = document.createElement('div');
    ov.id = 'sharedArtOv';
    ov.innerHTML = '<div class="sh-modal" id="sharedArtModal"></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', e => { if (e.target === ov) window.closeArticleModal(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') window.closeArticleModal(); });
  }

  function tagBadges(cstr) {
    return (cstr || '').split(/\s+/).filter(Boolean).map(k => {
      const label = EN ? TAG_EN[k] : TAG_KO[k];
      if (!label) return '';
      return `<span class="sh-cat" style="background:${TAG_COLOR[k] || '#7a746c'}">${esc(label)}</span>`;
    }).join('');
  }

  window.openArticleModal = function (article) {
    ensureDom();
    const a = article || {};
    const kb = EN ? (a.k_en || a.k || a.kb_implication_en || a.kb_implication)
                  : (a.k || a.k_en || a.kb_implication || a.kb_implication_en);
    const summary = EN ? (a.expanded_summary_en || a.expanded_summary || a.q_en || a.q)
                        : (a.expanded_summary || a.expanded_summary_en || a.q || a.q_en);
    const relRaw = (a.rl && a.rl.length) ? a.rl : ((a.source_links && a.source_links.length) ? a.source_links : null);
    const rel = relRaw || (a.u ? [{ t: a.t, u: a.u, src: a.src }] : []);
    const links = rel.map(r => `<a class="sh-lk" href="${esc(r.u)}" target="_blank" rel="noopener">
        ${r.src ? `<div class="sh-lks">${esc(r.src)}</div>` : ''}<div class="sh-lkt">${esc(r.t || r.title || r.u)}</div></a>`).join('');
    const catBadges = a.c ? tagBadges(a.c)
      : (a.category ? `<span class="sh-cat" style="background:${TAG_COLOR[a.category] || '#7a746c'}">${esc(a.category)}</span>` : '');
    const meta = [a.src, a.d].filter(Boolean).map(esc).join(' · ');

    document.getElementById('sharedArtModal').innerHTML = `
      <button class="sh-x" onclick="closeArticleModal()" aria-label="Close">×</button>
      ${meta ? `<div class="sh-meta">${meta}</div>` : ''}
      ${catBadges}
      <h2>${esc(a.t || '')}</h2>
      ${kb ? `<div class="sh-imp">💡 ${EN ? 'KB Implication' : 'KB 시사점'} · ${esc(kb)}</div>` : ''}
      <div class="sh-lb">${EN ? 'Summary' : '요약'}</div>
      <div class="sh-sum">${summary ? esc(summary) : `<span class="sh-empty">${EN ? '(No summary available.)' : '(요약 정보가 없습니다.)'}</span>`}</div>
      <div class="sh-lb">${EN ? 'Related Articles' : '관련 기사 링크'}</div>
      ${links || `<div class="sh-empty">${EN ? 'No related articles.' : '관련 기사가 없습니다.'}</div>`}
    `;
    document.getElementById('sharedArtOv').classList.add('on');
  };

  window.closeArticleModal = function () {
    const ov = document.getElementById('sharedArtOv');
    if (ov) ov.classList.remove('on');
  };
})();
