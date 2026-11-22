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
