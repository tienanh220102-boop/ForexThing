// ForexThing Dashboard — đọc dữ liệu trạng thái do bot commit vào repo
// và hiển thị lên GitHub Pages. Không có backend: fetch trực tiếp các file
// JSON/log nằm cùng repo (Pages deploy toàn bộ repo qua static.yml).

const FILES = {
  signals: 'last_signals.json',
  goldPa: 'gold_pa_state.json',
  decisionsLog: 'data/decisions.log',
};

const REFRESH_MS = 5 * 60 * 1000;

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

function renderGoldPa(state) {
  const signals = (state.signals || []).slice(-15).reverse();
  if (!signals.length) {
    document.getElementById('gold-pa').innerHTML = '<span class="muted">Chưa có setup nào được kích hoạt.</span>';
    return;
  }

  document.getElementById('gold-pa').innerHTML = `
    <table>
      <thead><tr>
        <th>Ngày</th><th>Phiên</th><th>Setup</th><th>Hướng</th><th>⭐</th>
        <th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>Kết quả</th><th>Pips</th>
      </tr></thead>
      <tbody>${signals.map(s => `
        <tr>
          <td>${esc(s.date || '—')}</td>
          <td>${esc(s.session || '—')}</td>
          <td>${esc(s.setup || '—')}</td>
          <td>${dirPill(s.dir)}</td>
          <td>${'★'.repeat(s.stars || 0)}</td>
          <td>${fmtPrice(s.entry)}</td>
          <td>${fmtPrice(s.sl)}</td>
          <td>${fmtPrice(s.tp1)}</td>
          <td>${fmtPrice(s.tp2)}</td>
          <td>${s.outcome ? `<span class="pill ${s.correct ? 'win' : 'loss'}">${esc(s.outcome)}</span>` : '<span class="pill open">ĐANG MỞ</span>'}</td>
          <td>${fmtPips(s.pips)}</td>
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
  const badge = document.getElementById('last-update');
  if (m) {
    const t = new Date(m[1].replace(' ', 'T') + 'Z');
    const ageMin = Math.round((Date.now() - t.getTime()) / 60000);
    badge.textContent = `Dữ liệu lúc ${t.toLocaleString('vi-VN')} (${ageMin} phút trước)`;
    badge.classList.toggle('stale', ageMin > 60);
  } else {
    badge.textContent = 'Không xác định được thời điểm cập nhật';
  }
}

// ---------- main ----------

async function load() {
  const tasks = [
    fetchRaw(FILES.signals).then(t => {
      const j = JSON.parse(t);
      renderStats(j.results || []);
      renderActiveSignals(j);
      renderResults(j.results || []);
    }).catch(e => {
      document.getElementById('stats').innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
    }),

    fetchRaw(FILES.goldPa).then(t => {
      // file có thể chứa log text phía trước JSON — cắt từ dấu { đầu tiên
      const start = t.indexOf('{');
      renderGoldPa(JSON.parse(t.slice(start)));
    }).catch(e => {
      document.getElementById('gold-pa').innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
    }),

    fetchRaw(FILES.decisionsLog).then(renderDecisionsLog).catch(e => {
      document.getElementById('decisions-log').textContent = e.message;
    }),
  ];
  await Promise.allSettled(tasks);
}

load();
setInterval(load, REFRESH_MS);
