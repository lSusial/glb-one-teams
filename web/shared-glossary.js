/* shared-glossary.js — 전문용어 클릭 시 정의·배경 팝오버 (피드백 10, 2026-09-12)
 * 사용: 이미 esc()된 텍스트를 window.glossarize(html) 로 감싸면 알려진 용어에
 *       .gl-term 스팬이 붙는다. 클릭하면 정의 팝오버. LLM 없이 정적 사전.
 * 확장: 아래 GLOSSARY 에 항목만 추가하면 됨(terms=본문 표기형, ko/en=정의).
 */
(function () {
  const EN = new URLSearchParams(location.search).get('lang') === 'en';

  // key: {terms:[본문에 등장하는 표기형들], ko, en}
  const GLOSSARY = {
    interest_rate: { terms: ['기준금리', '정책금리', '금리', 'interest rate', 'policy rate', 'rate cut', 'rate hike'],
      ko: '중앙은행이 정하는 돈의 값. 올리면 대출·투자가 위축되고 통화가치가 오르는 경향, 내리면 반대.', en: 'The price of money set by a central bank. Hikes cool borrowing and tend to strengthen the currency; cuts do the opposite.' },
    exchange_rate: { terms: ['환율', 'exchange rate', 'FX', 'foreign exchange'],
      ko: '두 통화의 교환 비율. 자국 통화 약세는 수출에 유리하나 수입물가·외화부채 부담을 키운다.', en: 'The price of one currency in another. A weaker home currency helps exports but raises import prices and FX-debt burden.' },
    inflation: { terms: ['인플레이션', '물가', 'inflation', 'CPI'],
      ko: '물가가 지속적으로 오르는 현상. 높으면 중앙은행이 금리를 올려 대응한다.', en: 'A sustained rise in the general price level; high readings usually prompt central banks to raise rates.' },
    gdp: { terms: ['GDP', '국내총생산', '경제성장'],
      ko: '한 나라가 일정 기간 생산한 부가가치의 총합. 경제 규모·성장세의 기본 지표.', en: "The total value added a country produces; the baseline gauge of an economy's size and growth." },
    monetary_policy: { terms: ['통화정책', 'monetary policy'],
      ko: '중앙은행이 금리·통화량으로 물가와 경기를 조절하는 정책.', en: 'How a central bank manages inflation and growth through interest rates and money supply.' },
    central_bank: { terms: ['중앙은행', 'central bank'],
      ko: '통화 발행과 금리·물가·금융안정을 책임지는 기관(한국은행, Fed 등).', en: 'The institution responsible for issuing currency and steering rates, inflation and financial stability.' },
    gov_bond: { terms: ['국채', '채권', 'government bond', 'treasury', 'sovereign bond', 'yield'],
      ko: '정부가 발행하는 차용증서. 금리(수익률)가 오르면 가격은 내리고, 국가 조달비용을 뜻한다.', en: "Government debt securities; when the yield rises, the price falls. It reflects a state's borrowing cost." },
    qe: { terms: ['양적완화', 'quantitative easing', 'QE'],
      ko: '중앙은행이 국채 등을 대량 매입해 시중에 돈을 푸는 비전통적 완화책.', en: 'An unconventional easing tool where a central bank buys assets to inject money into the system.' },
    geopolitics: { terms: ['지정학', 'geopolitics', 'geopolitical'],
      ko: '지리·국가 간 힘의 관계가 경제·시장에 미치는 위험(전쟁·분쟁·제재 등).', en: 'Risks to markets from the geographic balance of power between states — war, conflict, sanctions.' },
    sanctions: { terms: ['제재', 'sanctions', 'sanction'],
      ko: '특정 국가·기업·개인과의 거래를 제한하는 조치. 금융기관은 위반 시 큰 벌금·거래차단 위험.', en: 'Measures restricting dealings with a target country, firm or person; breaches expose banks to heavy fines and cutoff.' },
    ofac: { terms: ['OFAC'],
      ko: '미국 재무부 해외자산통제국. 미국 제재 명단(SDN)을 관리하며 글로벌 금융에 광범위한 영향.', en: "The US Treasury's Office of Foreign Assets Control, which runs US sanctions lists (SDN) with broad global reach." },
    fatf: { terms: ['FATF'],
      ko: '자금세탁·테러자금 방지 국제기구. 회원국 규제 표준을 정하고 취약국을 그레이/블랙리스트로 지정.', en: 'The global anti-money-laundering standard-setter; it grey/black-lists jurisdictions with weak controls.' },
    esg: { terms: ['ESG'],
      ko: '환경·사회·지배구조 요소. 투자·여신 심사와 공시규제의 핵심 축으로 부상.', en: 'Environmental, social and governance factors — now central to investment screening and disclosure rules.' },
    green_bond: { terms: ['그린본드', 'green bond', '녹색채권'],
      ko: '친환경 프로젝트 자금 조달용으로 발행되는 채권.', en: 'A bond whose proceeds are earmarked for environmentally beneficial projects.' },
    fintech: { terms: ['핀테크', 'fintech'],
      ko: '금융과 기술의 결합(간편결제·디지털뱅킹·블록체인 등).', en: 'Technology-driven financial services — payments, digital banking, blockchain and more.' },
    npl: { terms: ['부실채권', 'NPL', 'non-performing loan'],
      ko: '이자·원금 회수가 어려운 대출. 비율이 높으면 은행 건전성이 나빠진다.', en: 'Loans unlikely to be repaid; a high NPL ratio signals weakening bank asset quality.' },
    liquidity: { terms: ['유동성', 'liquidity'],
      ko: '자산을 손실 없이 빠르게 현금화할 수 있는 정도, 또는 시중 자금 사정.', en: 'How readily assets convert to cash without loss — or the overall availability of funds in a market.' },
    basel: { terms: ['바젤', 'Basel', 'BIS 자기자본비율', 'capital adequacy', 'capital ratio'],
      ko: '은행의 자기자본 규제 국제기준(바젤 III 등). 손실 흡수 능력을 자본비율로 요구한다.', en: 'International bank capital rules (Basel III); they require capital buffers to absorb losses.' },
    stress_test: { terms: ['스트레스 테스트', 'stress test'],
      ko: '위기 시나리오에서 은행이 버틸 수 있는지 점검하는 감독 평가.', en: 'A supervisory check of whether a bank can withstand adverse scenarios.' },
    aml: { terms: ['자금세탁방지', 'AML', 'money laundering', 'anti-money laundering'],
      ko: '범죄수익 세탁을 막기 위한 규제·절차. 위반 시 금융기관 제재·평판 리스크.', en: 'Rules and controls to prevent laundering criminal proceeds; breaches bring penalties and reputational risk.' },
    credit_rating: { terms: ['신용등급', '국가신용등급', 'credit rating', 'sovereign rating'],
      ko: '차입자(국가·기업)의 상환능력 평가 등급. 강등되면 조달비용이 오른다.', en: "A grade of a borrower's ability to repay; a downgrade raises its cost of funding." },
    current_account: { terms: ['경상수지', 'current account', '무역수지', 'trade balance'],
      ko: '한 나라의 대외 거래 수지(무역·소득 등). 적자가 크면 통화 약세 압력.', en: "A country's external balance on trade and income; large deficits pressure the currency lower." },
    tariff: { terms: ['관세', 'tariff', 'tariffs'],
      ko: '수입품에 부과하는 세금. 무역흐름·물가·기업 공급망에 직접 영향.', en: 'A tax on imports that directly affects trade flows, prices and corporate supply chains.' },
    fed: { terms: ['연준', 'Fed', 'Federal Reserve', 'FOMC'],
      ko: '미국 중앙은행. 세계 기축통화 금리를 정해 글로벌 자금흐름을 좌우한다.', en: "The US central bank; its rate decisions on the world's reserve currency drive global capital flows." },
    boj: { terms: ['BOJ', 'Bank of Japan', '일본은행'],
      ko: '일본 중앙은행. 초완화 정책과 엔화 향방으로 아시아 시장에 큰 영향.', en: "Japan's central bank; its ultra-easy policy and the yen's path heavily influence Asian markets." },
    ecb: { terms: ['ECB', 'European Central Bank'],
      ko: '유럽중앙은행. 유로존 금리·통화정책을 담당.', en: 'The European Central Bank, in charge of euro-area rates and monetary policy.' },
    default_risk: { terms: ['디폴트', 'default', '채무불이행'],
      ko: '차입자가 원리금을 갚지 못하는 상태.', en: 'When a borrower fails to meet debt repayments.' },
    ipo: { terms: ['IPO', '기업공개', '상장'],
      ko: '기업이 주식을 증시에 처음 공개·상장해 자금을 조달하는 것.', en: 'An initial public offering — a company listing shares publicly to raise capital.' },
    ma: { terms: ['M&A', '인수합병'],
      ko: '기업의 인수·합병. 시장 재편과 경쟁구도 변화의 신호.', en: 'Mergers and acquisitions — a signal of market consolidation and shifting competition.' },
    repo: { terms: ['레포금리', 'repo rate', '역레포', 'reverse repo'],
      ko: '중앙은행이 단기 유동성을 조절하는 환매조건부 거래 금리(정책금리 역할).', en: "A repurchase-agreement rate a central bank uses to steer short-term liquidity (a policy-rate lever)." },
  };

  // 매칭 준비
  const FORM2KEY = new Map();
  const forms = [];
  for (const [k, v] of Object.entries(GLOSSARY)) {
    for (const t of v.terms) { FORM2KEY.set(t.toLowerCase(), k); forms.push(t); }
  }
  forms.sort((a, b) => b.length - a.length);
  const isAscii = (s) => /^[\x00-\x7F]+$/.test(s);
  const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pat = forms.map((f) => isAscii(f) ? `(?<![A-Za-z0-9])${escRe(f)}(?![A-Za-z0-9])` : escRe(f)).join('|');
  const RE = new RegExp(pat, 'gi');

  window.glossarize = function (escapedHtml) {
    if (!escapedHtml) return escapedHtml;
    const done = new Set();          // 용어당 첫 등장만 표시(과밀 방지)
    return escapedHtml.replace(RE, (m) => {
      const key = FORM2KEY.get(m.toLowerCase());
      if (!key || done.has(key)) return m;
      done.add(key);
      return `<span class="gl-term" data-k="${key}" role="button" tabindex="0">${m}</span>`;
    });
  };

  // 스타일 1회 주입
  function ensureStyle() {
    if (document.getElementById('glStyle')) return;
    const s = document.createElement('style');
    s.id = 'glStyle';
    s.textContent = `
      .gl-term{border-bottom:1px dashed var(--gold,#c8991a);cursor:help}
      .gl-pop{position:fixed;z-index:10001;max-width:min(300px,88vw);background:#fff;color:#2b2926;border:1px solid #e8e4dd;border-radius:10px;box-shadow:0 8px 28px rgba(20,18,15,.20);padding:11px 13px;font-size:12.5px;line-height:1.55}
      .gl-pop .glt{font-weight:700;margin-bottom:3px;font-size:13px;color:#151515}
    `;
    document.head.appendChild(s);
  }

  function closeGl() { const p = document.getElementById('glPop'); if (p) p.remove(); }

  function showGl(el) {
    ensureStyle();
    closeGl();
    const key = el.getAttribute('data-k');
    const g = GLOSSARY[key];
    if (!g) return;
    const pop = document.createElement('div');
    pop.id = 'glPop';
    pop.className = 'gl-pop';
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    pop.innerHTML = `<div class="glt">${esc(el.textContent)}</div><div>${esc(EN ? g.en : g.ko)}</div>`;
    document.body.appendChild(pop);
    const r = el.getBoundingClientRect();
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    let left = Math.min(Math.max(8, r.left), window.innerWidth - pw - 8);
    let top = r.bottom + 6;
    if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest ? e.target.closest('.gl-term') : null;
    if (el) { e.stopPropagation(); showGl(el); }
    else if (!(e.target.closest && e.target.closest('.gl-pop'))) closeGl();
  });
  window.addEventListener('scroll', closeGl, true);
})();
