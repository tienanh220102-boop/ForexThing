// ForexThing Dashboard — đọc dữ liệu trạng thái do bot commit vào repo
// và hiển thị lên GitHub Pages. Không có backend: fetch trực tiếp các file
// JSON/log nằm cùng repo (Pages deploy toàn bộ repo qua static.yml).

const FILES = {
  signals: 'last_signals.json',
  goldPa: 'gold_pa_state.json',
  decisionsLog: 'data/decisions.log',
  prices: 'price_history.json',
  scenarios: 'scenario_state.json',
};

const REFRESH_MS = 5 * 60 * 1000;
const ACTIVE_PAIRS = ['XAU/USD', 'EUR/USD', 'USD/JPY', 'WTI/USD'];

// chart cần dữ liệu từ 3 file fetch song song nên gom về đây
const data = { signals: null, goldPa: null, prices: null };
let chart = null;
let currentSymbol = 'XAU/USD';

// ---------- helpers ----------

async function fetchRaw(path) {
  // cache-bust vì GitHub Pages/CDN có thể giữ bản cũ
  const res = await fetch(`${path}?t=${Date.now()}`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.text();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fmtPrice(x) {
  if (x == null || isNaN(x)) return '—';
  return Number(x) >= 100 ? Number(x).toFixed(2) : Number(x).toFixed(5);
}

function fmtPips(x) {
  if (x == null || isNaN(x)) return '—';
  const v = Number(x);
  const cls = v >= 0 ? 'pos' : 'neg';
  return `<span class="${cls}">${v > 0 ? '+' : ''}${v.toFixed(1)}</span>`;
}

function fmtEpoch(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('vi-VN', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

function timeAgo(ts) {
  const mins = Math.round((Date.now() / 1000 - ts) / 60);
  if (mins < 60) return `${mins} phút trước`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours} giờ trước`;
  return `${Math.floor(hours / 24)} ngày trước`;
}

function dirPill(dir) {
  const d = String(dir).toUpperCase();
  return `<span class="pill ${d === 'BUY' ? 'buy' : 'sell'}">${d}</span>`;
}

function outcomePill(r) {
  if (r.correct === true) return '<span class="pill win">WIN</span>';
  if (r.correct === false) return '<span class="pill loss">LOSS</span>';
  return '<span class="pill open">ĐANG MỞ</span>';
}

function pct(n, d) {
  return d > 0 ? `${(100 * n / d).toFixed(1)}%` : '—';
}

// ---------- render từng khu vực ----------

function renderStats(results) {
  const closed = results.filter(r => r.correct === true || r.correct === false);
  const wins = closed.filter(r => r.correct).length;
  const recent = closed.slice(-20);
  const recentWins = recent.filter(r => r.correct).length;

  const byRegime = {};
  for (const r of closed) {
    const k = r.regime || 'N/A';
    byRegime[k] = byRegime[k] || { n: 0, w: 0 };
    byRegime[k].n++;
    if (r.correct) byRegime[k].w++;
  }

  const cells = [
    { value: closed.length, label: 'Lệnh đã đóng' },
    { value: pct(wins, closed.length), label: `Win rate tổng (${wins}/${closed.length})` },
    { value: pct(recentWins, recent.length), label: `Win rate 20 lệnh gần nhất` },
    ...Object.entries(byRegime).map(([k, v]) => ({
      value: pct(v.w, v.n), label: `Regime ${k} (${v.w}/${v.n})`,
    })),
  ];

  document.getElementById('stats').innerHTML = cells.map(c =>
    `<div class="stat"><div class="value">${c.value}</div><div class="label">${esc(c.label)}</div></div>`
  ).join('');
}

function renderActiveSignals(signalsJson) {
  // Các key dạng "XAU/USD|SELL": epoch — thời điểm gửi tín hiệu gần nhất của cặp|hướng
  const rows = Object.entries(signalsJson)
    .filter(([k, v]) => k.includes('|') && typeof v === 'number')
    .map(([k, ts]) => {
      const [sym, dir] = k.split('|');
      return { sym, dir, ts };
    })
    .sort((a, b) => b.ts - a.ts);

  if (!rows.length) {
    document.getElementById('active-signals').innerHTML = '<span class="muted">Chưa có tín hiệu nào.</span>';
    return;
  }

  document.getElementById('active-signals').innerHTML = `
    <table>
      <thead><tr><th>Cặp</th><th>Hướng</th><th>Thời điểm gửi</th><th>Cách đây</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td><b>${esc(r.sym)}</b></td>
          <td>${dirPill(r.dir)}</td>
          <td>${fmtEpoch(r.ts)}</td>
          <td class="muted">${timeAgo(r.ts)}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

function renderResults(results) {
  const recent = results.slice(-20).reverse();
  if (!recent.length) {
    document.getElementById('results').innerHTML = '<span class="muted">Chưa có kết quả nào.</span>';
    return;
  }

  document.getElementById('results').innerHTML = `
    <table>
      <thead><tr>
        <th>Ngày</th><th>Cặp</th><th>Hướng</th><th>Regime</th>
        <th>Kết quả</th><th>Pips</th><th>Entry</th><th>Conf</th>
      </tr></thead>
      <tbody>${recent.map(r => `
        <tr>
          <td>${esc(r.date || '—')}</td>
          <td><b>${esc(r.sym)}</b></td>
          <td>${dirPill(r.signal)}</td>
          <td><span class="pill regime">${esc(r.regime || '—')}</span></td>
          <td>${outcomePill(r)}</td>
          <td>${fmtPips(r.pips)}</td>
          <td>${fmtPrice(r.entry)}</td>
          <td>${r.conf != null ? esc(r.conf) + '%' : '—'}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

// ---------- PA: hiệu quả (chỉ hiển thị số bot đã tính trong state.pa_report) ----------

// Nhãn cấp phải trùng pa_report.TIER_LABELS (Telegram) — dashboard không tự suy cấp.
const PA_TIER_LABELS = { 1: 'Cấp 1 (mạnh)', 2: 'Cấp 2 (trung bình)', 3: 'Cấp 3 (dò đường)' };
const PA_TIER_KEYS = ['1', '2', '3', 'none'];
const PA_SPRT_LABELS = {
  CHUA_DU: 'Chưa đủ dữ liệu',
  CONFIRM_EDGE: 'Có bằng chứng có edge',
  REJECT_EDGE: 'Có bằng chứng KHÔNG có edge',
};

function paTierLabel(t) {
  return PA_TIER_LABELS[t] || 'Chưa phân cấp';
}

function isNum(x) {
  return typeof x === 'number' && isFinite(x);
}

function fmtNum(x) {
  return isNum(x) ? String(x) : '—';
}

function fmtSigned(x, digits = 2) {
  if (!isNum(x)) return '—';
  return `${x > 0 ? '+' : ''}${x.toFixed(digits)}`;
}

function fmtR(x, signed = true) {
  if (!isNum(x)) return '—';
  const cls = x > 1e-9 ? 'pos' : x < -1e-9 ? 'neg' : '';
  return `<span class="${cls}">${signed ? fmtSigned(x) : x.toFixed(2)}R</span>`;
}

function fmtWinRate(s) {
  return isNum(s.win_rate) ? `${Math.round(s.win_rate * 100)}%` : '—';
}

function paStatsEmpty(s) {
  // cùng điều kiện "Không có lệnh nào" của pa_report._block / _tier_lines
  return !s || (!s.issued && !s.n && !s.no_trade && !s.unscored);
}

function renderPaPeriodCard(title, period) {
  const s = period && period.stats;
  const head = `<h3>${esc(title)}</h3>` + (period && period.label ? `<div class="muted">${esc(period.label)}</div>` : '');
  if (!s) return `<div class="pa-period">${head}<span class="muted">Chưa có số liệu.</span></div>`;
  if (paStatsEmpty(s)) return `<div class="pa-period">${head}<span class="muted">Không có lệnh nào.</span></div>`;

  const other = isNum(s.n) && isNum(s.tp) && isNum(s.sl) ? s.n - s.tp - s.sl : 0;
  const rows = [
    ['Phát / khớp', `${esc(fmtNum(s.issued))} / ${esc(fmtNum(s.filled))}`],
    ['Không khớp/huỷ', esc(fmtNum(s.no_trade)) + (s.unscored ? ` · không chấm ${esc(fmtNum(s.unscored))}` : '')],
    ['TP / SL', `${esc(fmtNum(s.tp))} / ${esc(fmtNum(s.sl))}` + (other > 0 ? ` · ${esc(other)} khác` : '')],
    ['Thắng / tổng', `${esc(fmtNum(s.wins))}/${esc(fmtNum(s.n))} (${esc(fmtWinRate(s))})`],
    ['Tổng R', fmtR(s.total_r)],
    ['TB R/lệnh', s.n ? fmtR(s.avg_r) : '—'],
    ['Sụt giảm tối đa', s.n ? fmtR(s.max_dd_r, false) : '—'],
    ['Tốt nhất / tệ nhất', `${fmtR(s.best_r)} / ${fmtR(s.worst_r)}`],
  ];

  const tiers = (period && period.tiers) || {};
  const tierRows = PA_TIER_KEYS
    .filter(k => tiers[k] && !paStatsEmpty(tiers[k]))
    .map(k => {
      const t = tiers[k];
      return `<tr><td>${esc(paTierLabel(Number(k)))}</td><td>${esc(fmtNum(t.issued))}</td><td>${esc(fmtNum(t.filled))}</td>`
        + `<td>${esc(fmtNum(t.wins))}/${esc(fmtNum(t.n))} (${esc(fmtWinRate(t))})</td><td>${fmtR(t.total_r)}</td></tr>`;
    }).join('');
  const tierTable = tierRows ? `<div class="table-wrap"><table class="pa-tier">
      <thead><tr><th>Cấp</th><th>Phát</th><th>Khớp</th><th>Thắng/tổng</th><th>Tổng R</th></tr></thead>
      <tbody>${tierRows}</tbody></table></div>` : '';

  return `<div class="pa-period">${head}
    <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>${tierTable}</div>`;
}

function renderPaReport(report) {
  if (!report || typeof report !== 'object') {
    return '<div class="pa-report"><h3>Hiệu quả PA PAPER</h3>'
      + '<span class="muted">Chưa có số liệu - sẽ có sau lượt chạy kế tiếp.</span></div>';
  }
  const sprt = report.sprt || {};
  const verdict = PA_SPRT_LABELS[sprt.verdict] || (sprt.verdict ? String(sprt.verdict) : 'Chưa có');
  const updated = isNum(report.generated_at)
    ? new Date(report.generated_at * 1000).toLocaleString('vi-VN', { timeZone: 'Asia/Ho_Chi_Minh' }) + ' (giờ VN)'
    : '—';
  return `<div class="pa-report"><h3>Hiệu quả PA PAPER</h3>
    <div class="pa-periods">
      ${renderPaPeriodCard('Tuần', report.week)}
      ${renderPaPeriodCard('Tháng', report.month)}
      ${renderPaPeriodCard('Từ đầu', report.all)}
    </div>
    <p class="pa-sprt">SPRT: <b>${esc(verdict)}</b> · n = ${esc(fmtNum(sprt.n))} · LLR = ${esc(fmtSigned(sprt.llr))}
      (ngưỡng bác ${esc(fmtSigned(sprt.lower))} / xác nhận ${esc(fmtSigned(sprt.upper))})</p>
    <p class="muted">Số liệu do bot tính, trùng báo cáo Telegram · R đã trừ chi phí · cập nhật lúc ${esc(updated)}</p></div>`;
}

function renderGoldPa(state) {
  const target = document.getElementById('gold-pa');
  const legacyCount = (state.legacy?.state?.signals || (state.ledger_version === 2 ? [] : state.signals) || []).length;
  const calendar = state.calendar_health || {}, portfolio = state.portfolio || {}, study = state.forward_review || {};
  const calendarStatus = !calendar.checked_at || Date.now()/1000-calendar.checked_at>900 ? 'STALE / CHƯA XÁC MINH' : calendar.status;
  const observations = state.observation_summary || {};
  const notice = `<p><b>PA PAPER — chỉ mô phỏng, không giao dịch tiền thật.</b><br>
    Chốt toàn bộ tại TP1; chi phí giả lập 0,40 USD/oz/lệnh. Kiểm tra giá mới, khoảng lời sau phí và hiệu lực setup trước khi khớp.<br>
    Limit mới hết hạn theo setup và thời hạn lịch tin; lệnh hủy không tính là thua. ${legacyCount} tín hiệu cũ đã cách ly vì lỗi thời gian.</p>
    <p>Lịch tin: <b>${esc(calendarStatus)}</b> — ${esc(calendar.reason || 'Chưa có dữ liệu')}.<br>
    Kiểm tra lúc: ${calendar.checked_at ? esc(new Date(calendar.checked_at*1000).toLocaleString('vi-VN')) : '—'}.<br>
    Lệnh đang giữ chỗ: ${esc(portfolio.active ?? '—')}/2; ngân sách PA PAPER: ${esc(portfolio.reserved_risk_units ?? '—')}/2 đơn vị. Không phản ánh tài khoản broker.<br>
    Đợt đánh giá: ${esc(({COLLECTING: 'Đang thu thập', COLLECTING_EXTENDED: `Đang thu thập (đã gia hạn ${study.extensions || 0} lần vì thiếu mẫu)`, REVIEW_DUE: 'Đến hạn xem xét - tạm dừng lệnh mới'})[study.readiness] || study.readiness || 'CHƯA BẮT ĐẦU')}, đã chạy ${esc(study.days ?? 0)} ngày,
    ${esc(study.metrics?.n ?? 0)}/100 kết quả rõ. ${study.ends_at ? 'Mốc khóa: '+esc(new Date(study.ends_at*1000).toLocaleString('vi-VN'))+'.' : ''} Không tự bật tiền thật.</p>
    <details><summary>${esc(observations.count ?? 0)} ứng viên đã ghi nhận, gồm cả tín hiệu bị bỏ qua</summary>
    <p>Kết quả giả định từ lúc phát hiện, tách khỏi lệnh PAPER đã gửi; không phải bằng chứng một bộ lọc gây lỗ.</p>
    ${Object.entries(observations.groups || {}).map(([reason,g])=>`<p>${esc(reason)}: ${esc(g.candidates)} ứng viên; ${esc(g.shadow_resolved)} kết quả giả định rõ; ${Number(g.shadow_net_r).toFixed(2)}R.</p>`).join('')}</details>`;
  const report = renderPaReport(state.pa_report);
  const signals = (state.ledger_version === 2 ? state.signals || [] : []).slice(-15).reverse();
  if (!signals.length) {
    target.innerHTML = notice + report + '<span class="muted">Đang chờ tín hiệu PAPER dùng dữ liệu UTC đã kiểm tra.</span>';
    return;
  }

  target.innerHTML = notice + report + `
    <table>
      <thead><tr>
        <th>Ngày</th><th>Phiên</th><th>Setup</th><th>Hướng</th><th>⭐</th><th>Cấp</th>
        <th>Entry</th><th>SL</th><th>TP1</th><th>TP2 tham khảo</th><th>Kết quả mô phỏng</th><th>R sau chi phí</th>
      </tr></thead>
      <tbody>${signals.map(s => `
        <tr>
          <td>${esc(s.date || '—')}<br><small>${esc(s.trade_id || '')}</small></td>
          <td>${esc(s.session || '—')}</td>
          <td>${esc(s.setup || '—')}</td>
          <td>${dirPill(s.dir)}</td>
          <td>${'★'.repeat(s.stars || 0)}</td>
          <td>${esc(paTierLabel(s.tier))}</td>
          <td title="${esc(s.rule_version ? `RR sau phí: ${Number(s.net_reward_risk).toFixed(2)}; ${s.rule_version}` : 'Quy tắc gốc của lệnh')}">${fmtPrice(s.entry)}</td>
          <td>${fmtPrice(s.sl)}</td>
          <td>${fmtPrice(s.tp1)}</td>
          <td>${fmtPrice(s.tp2)}</td>
          <td>${s.outcome ? `<span class="pill ${s.correct === true ? 'win' : s.correct === false ? 'loss' : 'open'}">${esc(s.outcome)}</span>` : esc(s.status || 'PENDING')}${isNum(s.result_sent_at) ? ` <span class="tg-sent" title="Đã gửi kết quả qua Telegram lúc ${esc(fmtEpoch(s.result_sent_at))}">✉</span>` : ''}</td>
          <td>${s.net_r == null ? '—' : Number(s.net_r).toFixed(3) + 'R'}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

function renderDecisionsLog(text) {
  const lines = text.trim().split('\n').slice(-40).reverse();
  document.getElementById('decisions-log').innerHTML = lines.map(line => {
    let cls = '';
    if (/BLOCKED|NO_SIGNAL|NO_SETUP/.test(line)) cls = 'blocked';
    else if (/OUTLOOK/.test(line)) cls = 'outlook';
    else if (/Error|error|delisted/.test(line)) cls = 'error';
    else if (/SIGNAL|SENT|BUY|SELL/.test(line)) cls = 'signal';
    return `<span class="${cls}">${esc(line)}</span>`;
  }).join('\n');

  // Lấy timestamp dòng cuối cùng làm "cập nhật lần cuối"
  const m = text.trim().split('\n').pop().match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC/);
  data.logTs = m ? new Date(m[1].replace(' ', 'T') + 'Z').getTime() / 1000 : null;
}

// ---------- Bảng kịch bản PA (scenario_state.json, do scenario_board.py ghi) ----------
const SCN_TYPES = {
  quet_dao: 'Quét đầu range rồi đảo chiều', pha_that: 'Phá thật range, chạy theo',
  retest: 'Retest mốc vừa phá (BOS)', qml: 'Quasimodo (QML) sau CHoCH', ob: 'Retest vùng nến mạnh',
};
const SCN_STATUS = {
  WAITING: ['open', 'Chờ kích hoạt'], TRIGGERED: ['open', 'Đã kích hoạt'], HIT: ['win', 'Chạm mục tiêu'],
  FAILED: ['loss', 'Vô hiệu sau kích hoạt'], INVALIDATED: ['open', 'Vô hiệu trước kích hoạt'],
  NOT_TRIGGERED: ['open', 'Không xảy ra'], EXPIRED_OPEN: ['open', 'Hết giờ chưa tới đích'], AMBIGUOUS: ['open', 'Không rõ'],
};
const SCN_BOARD = { EU: 'phiên Âu', US: 'phiên Mỹ' };

function renderScenarios(state) {
  const el = document.getElementById('scenarios');
  const boards = (state.boards || []).filter(b => b.status !== 'SKIPPED');
  if (!boards.length) {
    el.innerHTML = '<span class="muted">Chưa có bảng nào — bảng đầu tiên đăng lúc 13:30 (phiên Âu) hoặc 19:30 (phiên Mỹ) giờ VN.</span>';
    return;
  }
  const dirVN = d => d === 'BUY' ? 'MUA' : d === 'SELL' ? 'BÁN' : '—';
  const latest = boards.slice(-2).reverse().map(b => {
    const r = b.range || {};
    const rows = (b.scenarios || []).map((x, i) => {
      const [cls, label] = SCN_STATUS[x.status] || ['open', x.status];
      return `<tr><td>${i === 0 && b.pick ? '⭐ ' : ''}${String.fromCharCode(65 + i)}</td>
        <td>${esc(SCN_TYPES[x.type] || x.type)}</td><td>${dirVN(x.dir)}</td>
        <td>${esc(x.score)}</td><td>${fmtPrice(x.target)}</td><td>${fmtPrice(x.stop)}</td>
        <td><span class="pill ${cls}">${esc(label)}</span></td></tr>`;
    }).join('');
    const when = new Date(b.created_at * 1000).toLocaleString('vi-VN', { timeZone: 'Asia/Ho_Chi_Minh' });
    const verdict = b.status === 'CLOSED' && b.pick
      ? ` · kết quả: giá ${({ BUY: 'tăng', SELL: 'giảm', FLAT: 'đi ngang' })[b.actual_dir] || '—'} → ${b.actual_dir === b.pick ? '✅ nghiêng đúng' : '❌ nghiêng sai'}` : '';
    return `<p><b>${esc(SCN_BOARD[b.name] || b.name)}</b> — ${esc(when)} · giá ${fmtPrice(b.price)} ·
      ${esc(r.label || 'range')} ${fmtPrice(r.lo)}–${fmtPrice(r.hi)} · nghiêng <b>${dirVN(b.pick)}</b>${verdict}</p>
      <table><thead><tr><th></th><th>Kịch bản</th><th>Hướng</th><th>Điểm</th><th>Mục tiêu</th><th>Vô hiệu</th><th>Trạng thái</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }).join('');
  const st = state.stats || { by_type: [], pick: { n: 0, right: 0 } };
  const p = st.pick || { n: 0, right: 0 };
  const statRows = (st.by_type || []).filter(s => s.n).map(s => {
    const done = s.hit + s.failed;
    return `<tr><td>${esc(SCN_TYPES[s.type] || s.type)}</td><td>${s.regime === 'TREND' ? 'có trend' : 'đi ngang'}</td>
      <td>${esc(s.n)}</td><td>${esc(s.triggered)}</td><td>${done ? `${s.hit}/${done} (${Math.round(100 * s.hit / done)}%)` : '—'}</td></tr>`;
  }).join('');
  el.innerHTML = latest + `
    <p><b>Độ trúng tích luỹ</b> — "nghiêng" đúng ${p.n ? `${p.right}/${p.n} (${Math.round(100 * p.right / p.n)}%)` : 'chưa có dữ liệu'}.
    Cần ít nhất vài chục bảng mới đáng tin.</p>
    ${statRows ? `<table><thead><tr><th>Loại kịch bản</th><th>Ngày</th><th>Lần đưa ra</th><th>Kích hoạt</th><th>Chạm mục tiêu / đã phân định</th></tr></thead><tbody>${statRows}</tbody></table>` : ''}
    <p class="muted">Chỉ là phân tích, không tạo hay thay đổi lệnh PAPER.</p>`;
}

// Mốc cập nhật = mới nhất giữa nhật ký phân tích (hệ vote đang tạm dừng nên có thể đứng yên)
// và lượt chạy bot PA (pa_report.generated_at / data_health.ts).
function renderUpdateBadge() {
  const pa = data.goldPa || {};
  const ts = [data.logTs, pa.pa_report?.generated_at, pa.data_health?.ts].filter(isNum);
  const badge = document.getElementById('last-update');
  if (ts.length) {
    const t = new Date(Math.max(...ts) * 1000);
    const ageMin = Math.round((Date.now() - t.getTime()) / 60000);
    badge.textContent = `Dữ liệu lúc ${t.toLocaleString('vi-VN')} (${ageMin} phút trước)`;
    badge.classList.toggle('stale', ageMin > 60);
  } else {
    badge.textContent = 'Không xác định được thời điểm cập nhật';
  }
}

// ---------- chart (lightweight-charts v4) ----------

function ema(values, period) {
  const k = 2 / (period + 1);
  let prev = null;
  return values.map(v => (prev = prev === null ? v : v * k + prev * (1 - k)));
}

function populateSymbolSelect() {
  const sel = document.getElementById('chart-symbol');
  if (sel.options.length || !data.prices) return;

  const syms = Object.keys(data.prices)
    .filter(s => data.prices[s] && Array.isArray(data.prices[s].bars) && data.prices[s].bars.length > 1)
    .sort((a, b) => {
      const ia = ACTIVE_PAIRS.indexOf(a), ib = ACTIVE_PAIRS.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
    });

  for (const s of syms) {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = ACTIVE_PAIRS.includes(s) ? `${s} ●` : s;
    sel.appendChild(opt);
  }
  if (!syms.includes(currentSymbol)) currentSymbol = syms[0];
  sel.value = currentSymbol;
  sel.onchange = () => { currentSymbol = sel.value; renderChart(currentSymbol); };
}

function renderChart(sym) {
  const el = document.getElementById('chart');
  if (!window.LightweightCharts) {
    el.innerHTML = '<span class="muted">Không tải được thư viện chart (CDN bị chặn?).</span>';
    return;
  }
  const stream = data.prices && data.prices[sym];
  const bars = stream && stream.data_version === 2 ? stream.bars : [];
  if (!bars || bars.length < 2) {
    el.innerHTML = '<span class="muted">Chưa có dữ liệu nến cho ' + esc(sym) + '.</span>';
    return;
  }

  if (chart) { chart.remove(); chart = null; }
  el.innerHTML = '';

  chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: { background: { color: '#ffffff' }, textColor: '#5f6368' },
    grid: { vertLines: { color: '#f1f3f4' }, horzLines: { color: '#f1f3f4' } },
    timeScale: { timeVisible: true, secondsVisible: false },
  });

  // UTC v2 retains real opens; never invent opens across market gaps.
  const candles = [];
  for (let i = 0; i < bars.length; i++) {
    candles.push({
      time: bars[i].t, open: bars[i].o,
      high: bars[i].h, low: bars[i].l, close: bars[i].c,
    });
  }

  const series = chart.addCandlestickSeries({
    upColor: '#137333', downColor: '#c5221f', borderVisible: false,
    wickUpColor: '#137333', wickDownColor: '#c5221f',
  });
  series.setData(candles);

  const emaVals = ema(candles.map(c => c.close), 20);
  const emaSeries = chart.addLineSeries({
    color: '#1a73e8', lineWidth: 1, priceLineVisible: false,
    lastValueVisible: false, crosshairMarkerVisible: false,
  });
  emaSeries.setData(candles.map((c, i) => ({ time: c.time, value: emaVals[i] })));

  // marker đặt tại nến gần nhất TRƯỚC thời điểm gửi tín hiệu
  const minT = candles[0].time;
  const snap = ts => {
    let best = null;
    for (const c of candles) { if (c.time <= ts) best = c.time; else break; }
    return best;
  };
  const markers = [];

  if (data.signals) {
    for (const [k, v] of Object.entries(data.signals.audit_status ? {} : data.signals)) {
      if (!k.startsWith(sym + '|') || typeof v !== 'number' || v < minT) continue;
      const t = snap(v);
      if (t === null) continue;
      const dir = k.split('|')[1];
      markers.push({
        time: t,
        position: dir === 'BUY' ? 'belowBar' : 'aboveBar',
        color: dir === 'BUY' ? '#137333' : '#c5221f',
        shape: dir === 'BUY' ? 'arrowUp' : 'arrowDown',
        text: 'TA ' + dir,
      });
    }
  }

  if (sym === 'XAU/USD' && data.goldPa) {
    const paSigs = (data.goldPa.ledger_version === 2 ? data.goldPa.signals || [] : []).filter(s => s.ts >= minT);
    for (const s of paSigs) {
      const t = snap(s.ts);
      if (t === null) continue;
      markers.push({
        time: t,
        position: s.dir === 'BUY' ? 'belowBar' : 'aboveBar',
        color: '#f9ab00',
        shape: s.dir === 'BUY' ? 'arrowUp' : 'arrowDown',
        text: ('PA ' + s.dir + ' ' + (s.outcome || '')).trim(),
      });
    }
    const last = paSigs[paSigs.length - 1];
    if (last) {
      const dashed = LightweightCharts.LineStyle.Dashed;
      const lines = [
        [last.entry, '#f9ab00', 'PA entry'], [last.sl, '#c5221f', 'SL'],
        [last.tp1, '#137333', 'TP1'], [last.tp2, '#137333', 'TP2'],
      ];
      for (const [price, color, title] of lines) {
        if (price == null) continue;
        series.createPriceLine({ price, color, lineWidth: 1, lineStyle: dashed, title });
      }
    }
  }

  markers.sort((a, b) => a.time - b.time);
  series.setMarkers(markers);

  // mặc định nhìn ~6 ngày gần nhất, kéo/zoom xem phần còn lại
  chart.timeScale().setVisibleLogicalRange({
    from: Math.max(0, candles.length - 150), to: candles.length,
  });
}

// ---------- main ----------

async function load() {
  const tasks = [
    fetchRaw(FILES.signals).then(t => {
      const j = JSON.parse(t);
      data.signals = j;
      renderStats(j.audit_status ? [] : j.results || []);
      renderActiveSignals(j.audit_status ? {} : j);
      renderResults(j.audit_status ? [] : j.results || []);
    }).catch(e => {
      document.getElementById('stats').innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
    }),

    fetchRaw(FILES.goldPa).then(t => {
      // file có thể chứa log text phía trước JSON — cắt từ dấu { đầu tiên
      const start = t.indexOf('{');
      data.goldPa = JSON.parse(t.slice(start));
      renderGoldPa(data.goldPa);
    }).catch(e => {
      document.getElementById('gold-pa').innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
    }),

    fetchRaw(FILES.scenarios).then(t => renderScenarios(JSON.parse(t))).catch(() => {
      document.getElementById('scenarios').innerHTML =
        '<span class="muted">Chưa có bảng nào — bảng đầu tiên đăng lúc 13:30 (phiên Âu) hoặc 19:30 (phiên Mỹ) giờ VN.</span>';
    }),

    fetchRaw(FILES.decisionsLog).then(renderDecisionsLog).catch(e => {
      document.getElementById('decisions-log').textContent = e.message;
    }),

    fetchRaw(FILES.prices).then(t => {
      data.prices = JSON.parse(t);
    }).catch(e => {
      document.getElementById('chart').innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
    }),
  ];
  await Promise.allSettled(tasks);
  renderUpdateBadge();

  if (data.prices) {
    populateSymbolSelect();
    renderChart(currentSymbol);
  }
}

load();
setInterval(load, REFRESH_MS);
