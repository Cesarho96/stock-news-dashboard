// Renderer safety tests: node --test tests/web
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';

const source = readFileSync(new URL('../../web/assets/dashboard.js', import.meta.url), 'utf8');

function load() {
  const sandbox = { __STOCKDASH_NO_BOOT__: true, URLSearchParams, Intl, Date };
  sandbox.window = sandbox;
  createContext(sandbox);
  runInContext(source, sandbox);
  return sandbox.StockDash;
}

const evil = '<img src=x onerror="alert(1)">';

function edition() {
  return {
    id: '2026-11-20-close', language: 'en', title: `Title ${evil}`, subtitle: 'US MARKETS', timelineLabel: 'Fri',
    intro: { greeting: 'Good evening.', headline: evil, body: 'Body', stamp: 'STAMP' },
    strip: { meta: 'meta', tiles: [{ name: 'S&P 500', mini: 'SPY', value: '$1', change: '▲ 1%', cls: 'green" onclick="x' }] },
    pages: [
      { type: 'top', number: '01', title: 'Top', cards: [
        { slot: 'lead', kicker: 'K', title: evil, paragraph: 'P', fields: [],
          sources: [{ name: 'Bad', url: 'javascript:alert(1)', time: 't' }, { name: 'Good', url: 'https://ok.example/a?b=1&c=2' }] },
      ] },
      { type: 'latest', number: '02', title: 'Latest', countLabel: '1 story', blocks: [
        { id: 'macro', title: 'Macro', stories: [{ title: 'S', fields: [{ label: 'L', text: '</script><script>alert(2)</script>' }], tickers: ['AAPL'], sources: [] }] },
      ] },
      { type: 'sectors', number: '05', title: 'Sectors', tabs: [
        { id: 'tech" onmouseover="x', label: 'Tech', perf: null, stories: [], empty: 'none' },
      ] },
      { type: 'unknown', number: '99', title: 'ignored' },
    ],
    footer: { sourcesLabel: 'Sources', sources: [{ name: 'X', url: 'data:text/html,hi' }], methodology: 'm', disclaimer: 'd' },
    qa: { status: 'ok' },
  };
}

test('escapes text and attributes from data files', () => {
  const dash = load();
  const html = dash.renderEdition(edition(), { latest: '2026-11-20-close', editions: [] }, { viewingLatest: true });
  assert.ok(!html.includes('<img'), 'raw HTML from data must not be rendered');
  assert.ok(html.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'));
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('onclick='), 'unknown class names are dropped');
  assert.ok(!html.includes('onmouseover='), 'ids are whitelisted');
  assert.ok(!html.includes('page-unknown'), 'unknown page types are skipped');
});

test('only http(s) links are clickable', () => {
  const dash = load();
  const html = dash.renderEdition(edition(), { latest: 'x', editions: [] }, {});
  assert.ok(!/href="javascript:/i.test(html));
  assert.ok(!/href="data:/i.test(html));
  assert.ok(html.includes('href="https://ok.example/a?b=1&amp;c=2"'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
  assert.equal(dash.safeUrl(' https://a.example/x '), 'https://a.example/x');
  assert.equal(dash.safeUrl('javascript:alert(1)'), null);
  assert.equal(dash.safeUrl('https://a.example/"onmouseover'), null);
});

test('timeline links to earlier editions and pluralizes', () => {
  const dash = load();
  const t = dash.UI.en;
  const one = dash.renderTimeline({ latest: 'a', editions: [{ id: 'a', label: 'Fri' }] }, 'a', t);
  assert.ok(one.includes('1 edition<'));
  const two = dash.renderTimeline({ latest: 'b', editions: [{ id: 'b', label: 'Fri' }, { id: '2026-11-19-close', label: 'Thu' }] }, 'b', t);
  assert.ok(two.includes('href="./"'));
  assert.ok(two.includes('href="?edition=2026-11-19-close"'));
  assert.ok(two.includes('2 editions'));
});

test('top-20 board renders escaped rows, safe links and chips', () => {
  const dash = load();
  const ed = edition();
  ed.pages[0].cards[0].chips = [{ symbol: 'NVDA', change: '▲ 3.10%', cls: 'green' }, { symbol: evil, change: 'x', cls: 'bad"x' }];
  ed.pages[0].board = {
    title: 'S&P 500 top 20', note: 'Ranked by market cap',
    columns: { rank: '#', company: 'Company', price: 'Price', change: 'Change', cap: 'Mkt cap', note: 'Why it moved' },
    rows: [
      { rank: 1, symbol: 'NVDA', name: 'NVIDIA', price: '$182.40', change: '▲ 3.10%', cls: 'green', marketCap: '$4.45T',
        sentiment: { key: 'bullish', label: 'Bullish news flow' }, note: `Demand ${evil}`, noteKind: 'ai', noteLabel: null,
        source: { name: 'Reuters', url: 'https://news.example/nvda', time: 'Nov 20, 2:05 PM ET' }, earnings: null },
      { rank: 2, symbol: 'AAPL', name: 'Apple', price: '$271.30', change: '▲ 1.10%', cls: 'green', marketCap: '$4.01T',
        sentiment: null, note: 'Supplier headline', noteKind: 'headline" onclick="x', noteLabel: 'Latest headline',
        source: { name: 'Bad', url: 'javascript:alert(1)', time: '' }, earnings: 'Earnings: Tue Nov 24' },
    ],
  };
  ed.qa = { status: 'degraded', release: '0123456789abcdef0123', modelLabel: 'Claude Sonnet 5.5' };
  const html = dash.renderEdition(ed, { latest: ed.id, editions: [] }, { viewingLatest: true });
  assert.ok(html.includes('<table class="board-table" role="table">'));
  assert.ok(html.includes('<th class="bd-co" scope="row" role="rowheader"><span class="tick">NVDA</span>'));
  assert.ok(html.includes('Demand &lt;img src=x onerror=&quot;alert(1)&quot;&gt;'));
  assert.ok(html.includes('href="https://news.example/nvda"'));
  assert.ok(!/href="javascript:/i.test(html));
  assert.ok(html.includes('bd-note bd-none'), 'unknown note kinds fall back to "none"');
  assert.ok(!html.includes('onclick='));
  assert.ok(html.includes('<span class="bd-note-lbl">Latest headline:</span>'));
  assert.ok(html.includes('<span class="td-chip"><span class="tick">NVDA</span><span class="td-chip-ch green">▲ 3.10%</span></span>'));
  assert.ok(html.includes('release 0123456789ab'), 'release id is shown, truncated');
  assert.ok(html.includes('Partially validated.'));
});

test('editions without a board (schema v1) still render', () => {
  const dash = load();
  const html = dash.renderEdition(edition(), { latest: 'x', editions: [] }, {});
  assert.ok(!html.includes('board-table'));
  assert.ok(html.includes('top-day'));
});
