const appState = { currentView: 'overview' };

// Backend base URL. Local dev hits the API directly on :8000; anywhere else
// (a real deployment) defaults to a same-origin /api path proxied by Caddy,
// which sidesteps CORS entirely instead of needing a second public origin.
// Override by setting window.SIGNALLAB_API_BASE before this script loads.
const API_BASE = window.SIGNALLAB_API_BASE || (
  ['localhost', '127.0.0.1'].includes(window.location.hostname) ? 'http://127.0.0.1:8000' : '/api'
);

// This is a single-tenant local paper-trading demo with no signup flow, so the
// UI transparently registers/logs in one fixed local account instead of
// showing a login screen. It carries no real trading privileges.
const DEMO_EMAIL = 'demo@signallab.dev';
const DEMO_PASSWORD = 'paper-trading-demo-only';

let authToken = null;
try { authToken = localStorage.getItem('sl_token'); } catch (err) { authToken = null; }

// Set by refreshHealth() from GET /health's licensed_data_configured field.
// Drives the data-source labels below -- there is no per-backtest signal for
// "this result used real vs. seeded candles", so this is the best available
// proxy: once a vendor is configured, /backtests fails closed (no seed
// fallback) for any symbol that hasn't actually been synced.
let licensedDataConfigured = false;

function showView(view) {
  document.querySelectorAll('.view').forEach(el => el.classList.toggle('active-view', el.id === view));
  document.querySelectorAll('[data-view]').forEach(el => el.classList.toggle('active', el.dataset.view === view && el.classList.contains('nav-item')));
  const titles = { overview: 'Good morning, trader', validator: 'Validate before you trust', journal: 'Your trading record', risk: 'Safety before speed' };
  document.getElementById('page-title').textContent = titles[view] || titles.overview;
  appState.currentView = view;
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (view === 'journal') loadTrades();
}

document.querySelectorAll('[data-view]').forEach(el => el.addEventListener('click', () => showView(el.dataset.view)));

function toast(message) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => el.classList.remove('show'), 2800);
}

function formatSigned(value) {
  const num = Number(value) || 0;
  if (num === 0) return '₹0.00';
  return `${num > 0 ? '+' : '-'}₹${Math.abs(num).toFixed(2)}`;
}

async function apiFetch(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  } catch (err) {
    throw new Error('Cannot reach the SignalLab API — is the backend running?');
  }
  if (res.status === 401) {
    authToken = null;
    try { localStorage.removeItem('sl_token'); } catch (err) { /* ignore */ }
    throw new Error('AUTH_EXPIRED');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (err) { /* ignore */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

async function ensureAuth() {
  if (authToken) return authToken;
  const credentials = JSON.stringify({ email: DEMO_EMAIL, password: DEMO_PASSWORD });
  let token;
  try {
    ({ access_token: token } = await apiFetch('/auth/login', { method: 'POST', body: credentials }));
  } catch (err) {
    ({ access_token: token } = await apiFetch('/auth/register', { method: 'POST', body: credentials }));
  }
  authToken = token;
  try { localStorage.setItem('sl_token', authToken); } catch (err) { /* ignore */ }
  return authToken;
}

async function getOrCreateStrategy(symbol, rules) {
  const cacheKey = `sl_strategy::${symbol}::${JSON.stringify(rules)}`;
  try {
    const cached = localStorage.getItem(cacheKey);
    if (cached) return Number(cached);
  } catch (err) { /* ignore */ }
  const strategy = await apiFetch('/strategies', { method: 'POST', body: JSON.stringify({ name: `${symbol} auto`, symbol, rules }) });
  try { localStorage.setItem(cacheKey, String(strategy.id)); } catch (err) { /* ignore */ }
  return strategy.id;
}

document.getElementById('runBacktest').addEventListener('click', async () => {
  const button = document.getElementById('runBacktest');
  const instrument = document.getElementById('instrument').value;
  const rules = {
    trend: document.getElementById('ruleTrend').checked,
    rsi: document.getElementById('ruleRsi').checked,
    volume: document.getElementById('ruleVolume').checked,
    timeframe: '1d',
    stop_atr: Number(document.getElementById('stopLoss').value),
    target_r: Number(document.getElementById('target').value),
  };
  if (!rules.trend && !rules.rsi && !rules.volume) return toast('Select at least one entry condition.');

  const originalLabel = button.innerHTML;
  button.disabled = true;
  button.innerHTML = 'Running validation…';
  try {
    await ensureAuth();
    const strategyId = await getOrCreateStrategy(instrument, rules);
    const result = await apiFetch(`/backtests/${strategyId}`, { method: 'POST' });
    const metrics = result.metrics || {};
    const historyLabel = licensedDataConfigured ? 'licensed NSE history' : 'seeded sample history';
    document.getElementById('hitRate').textContent = `${(metrics.win_rate ?? 0).toFixed(1)}%`;
    document.getElementById('hitDelta').textContent = `${metrics.trade_count ?? 0} trades · ${historyLabel}`;
    document.getElementById('netReturn').textContent = formatSigned(metrics.net_pnl);
    document.getElementById('drawdown').textContent = formatSigned(metrics.max_drawdown);
    document.getElementById('tradeCount').textContent = metrics.trade_count ?? 0;
    document.getElementById('rrRatio').textContent = rules.target_r.toFixed(2);
    document.getElementById('resultSubhead').textContent = `${instrument} · Daily · ${historyLabel}`;
    document.getElementById('contextText').textContent = (metrics.trade_count ?? 0) > 0
      ? `The live backtest engine ran ${metrics.trade_count} trades on stored candles for ${instrument} (${historyLabel}). Fill model: ${metrics.assumptions?.fill_model ?? 'n/a'}; costs ${metrics.assumptions?.costs ?? 'n/a'}. This is a historical simulation, not a forecast.`
      : `No trades triggered for this rule set over the ${historyLabel} for ${instrument}. Try relaxing an entry condition.`;
    toast(`Validation complete — ran on ${historyLabel}.`);
  } catch (err) {
    console.error(err);
    const message = String(err.message || err);
    if (message.includes('No licensed candles')) {
      toast(licensedDataConfigured
        ? `No candles synced for ${instrument} yet — call POST /data/nse/sync for it.`
        : 'No candles stored for this symbol yet — run backend/app/seed_demo_data.py.');
    } else {
      toast(message);
    }
  } finally {
    button.disabled = false;
    button.innerHTML = originalLabel;
  }
});

function renderTradeRow(trade) {
  const tagClass = trade.status === 'PAPER' ? 'green' : 'amber';
  const opened = new Date(trade.opened_at).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  const sideLabel = trade.side === 'LONG' ? 'Long' : 'Short';
  const linked = trade.strategy_id ? ' · linked strategy' : '';
  // entry_price/quantity come back as JSON strings (Decimal fields serialize
  // that way, preserving exact precision), not numbers -- wrap both in
  // Number() before display so trailing zeros from the DB's fixed scale
  // (e.g. "10.00000000") don't show up in the UI.
  return `<div class="trade-row"><span class="trade-tag ${tagClass}">${trade.status}</span><div><strong>${trade.symbol} · ${sideLabel}</strong><small>${opened}${linked}</small></div><b>₹${Number(trade.entry_price).toFixed(2)}</b><span class="trade-rule">${Number(trade.quantity)} qty</span></div>`;
}

async function loadTrades() {
  const container = document.getElementById('tradeRows');
  container.innerHTML = '<p class="disclaimer">Loading trades…</p>';
  try {
    await ensureAuth();
    const trades = await apiFetch('/trades');
    container.innerHTML = trades.length
      ? trades.map(renderTradeRow).join('')
      : '<p class="disclaimer">No paper trades yet. Add one above.</p>';
  } catch (err) {
    container.innerHTML = `<p class="disclaimer">Could not load trades: ${err.message}</p>`;
  }
}

document.getElementById('quickAddTrade').addEventListener('submit', async (event) => {
  event.preventDefault();
  const symbol = document.getElementById('tradeSymbol').value;
  const side = document.getElementById('tradeSide').value;
  const quantity = Number(document.getElementById('tradeQuantity').value);
  const entryPrice = Number(document.getElementById('tradeEntry').value);
  if (!quantity || quantity <= 0 || !entryPrice || entryPrice <= 0) return toast('Enter a quantity and entry price above zero.');
  try {
    await ensureAuth();
    await apiFetch('/trades/paper', { method: 'POST', body: JSON.stringify({ symbol, side, quantity, entry_price: entryPrice }) });
    document.getElementById('quickAddTrade').reset();
    toast('Paper trade recorded. No order was sent.');
    loadTrades();
  } catch (err) {
    toast(`Could not save trade: ${err.message}`);
  }
});

document.getElementById('refreshTrades').addEventListener('click', () => loadTrades());

async function refreshHealth() {
  const dot = document.getElementById('marketDot');
  const text = document.getElementById('marketStatusText');
  const pill = document.getElementById('dataSourcePill');
  try {
    const health = await apiFetch('/health');
    licensedDataConfigured = Boolean(health.licensed_data_configured);
    text.textContent = licensedDataConfigured ? 'licensed feed' : 'seeded sample data';
    dot.style.background = '#4eab78';
    dot.style.boxShadow = '0 0 0 3px #d8efdf';
    pill.textContent = licensedDataConfigured ? 'LIVE NSE DATA' : 'SEEDED SAMPLE DATA';
  } catch (err) {
    licensedDataConfigured = false;
    text.textContent = 'backend offline';
    dot.style.background = '#b7605b';
    dot.style.boxShadow = '0 0 0 3px #f6dcda';
    pill.textContent = 'BACKEND OFFLINE';
  }
}

refreshHealth();
