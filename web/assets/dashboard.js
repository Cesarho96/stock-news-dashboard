/* AI Stock News Dashboard — renderer.
   Reads data/manifest.json and one edition JSON (written by the news agent) and builds the page.
   Everything coming from data files is escaped; links must be http(s). No inline scripts or
   styles are generated, so the site runs under a strict Content-Security-Policy. */
(function (root) {
  'use strict';

  var UI = {
    en: {
      sources: 'Sources',
      readSource: 'Read the source',
      fresh: 'New',
      earlier: 'Earlier',
      show: 'show',
      hide: 'hide',
      timelineKicker: 'Edition history',
      timelineTitle: 'Briefings',
      editions: 'editions',
      edition: 'edition',
      latest: 'Latest',
      history: 'Earlier',
      timelineHelp: 'Pick an edition to read that briefing.',
      demo: 'Demo data.',
      demoText: 'Synthetic content for layout testing. Not real market data or news.',
      fallback: 'AI summary unavailable.',
      fallbackText: 'This edition shows market data and source headlines only.',
      degraded: 'Partially validated.',
      degradedText: 'Some AI sections did not pass validation and were replaced with source headlines.',
      historyBanner: 'You are reading an earlier edition:',
      goLatest: 'Go to the latest edition',
      newEdition: 'A newer edition is available.',
      reload: 'Load it',
      loading: 'Loading the latest edition…',
      loadError: 'Could not load the dashboard data.',
      noEditions: 'No editions have been published yet.',
      methodology: 'Methodology',
      disclaimer: 'Disclaimer',
      generated: 'Generated',
      clock: 'Current time',
      headlines: 'Latest headlines',
      noHeadlines: 'No headlines in this window.',
      indicators: 'Key indicators',
      summary: 'Summary'
    },
    es: {
      sources: 'Fuentes',
      readSource: 'Ver fuente',
      fresh: 'Nuevo',
      earlier: 'Anterior',
      show: 'mostrar',
      hide: 'ocultar',
      timelineKicker: 'Histórico',
      timelineTitle: 'Ediciones',
      editions: 'ediciones',
      edition: 'edición',
      latest: 'Vigente',
      history: 'Histórico',
      timelineHelp: 'Elige una edición para consultarla.',
      demo: 'Datos demo.',
      demoText: 'Contenido sintético para probar el diseño. No son datos ni noticias reales.',
      fallback: 'Resumen IA no disponible.',
      fallbackText: 'Esta edición muestra solo datos de mercado y titulares de las fuentes.',
      degraded: 'Validación parcial.',
      degradedText: 'Algunas secciones de IA no pasaron la validación y se reemplazaron con titulares de las fuentes.',
      historyBanner: 'Estás viendo una edición anterior:',
      goLatest: 'Ir a la edición vigente',
      newEdition: 'Hay una edición más reciente.',
      reload: 'Cargarla',
      loading: 'Cargando la edición vigente…',
      loadError: 'No se pudieron cargar los datos del tablero.',
      noEditions: 'Aún no hay ediciones publicadas.',
      methodology: 'Metodología',
      disclaimer: 'Aviso',
      generated: 'Generado',
      clock: 'Hora actual',
      headlines: 'Titulares recientes',
      noHeadlines: 'Sin titulares en esta ventana.',
      indicators: 'Indicadores clave',
      summary: 'Resumen'
    }
  };

  var CLASS_OK = /^(green|red|flat)$/;
  var BADGE_OK = /^ncat-[a-z]$/;
  var ID_OK = /^[a-z0-9-]{1,40}$/;
  var FILE_OK = /^editions\/[A-Za-z0-9._-]+\.json$/;

  function ui(lang) {
    return UI[lang] || UI.en;
  }

  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function safeUrl(url) {
    if (typeof url !== 'string') return null;
    var u = url.trim();
    return /^https?:\/\/[^\s<>"'`]+$/i.test(u) ? u : null;
  }

  function cls(value) {
    return CLASS_OK.test(value || '') ? value : '';
  }

  function safeId(value) {
    var v = String(value || '');
    return ID_OK.test(v) ? v : 'x';
  }

  function extLink(url, text, className) {
    var href = safeUrl(url);
    if (!href) return '<span class="' + esc(className || '') + '">' + esc(text) + '</span>';
    return '<a class="' + esc(className || '') + '" href="' + esc(href) + '" target="_blank" rel="noopener noreferrer">' + esc(text) + '</a>';
  }

  // ---- small pieces --------------------------------------------------------------------
  function fields(list) {
    return (list || [])
      .map(function (f) {
        return '<div class="fld"><span class="lbl">' + esc(f.label) + '</span>' + esc(f.text) + '</div>';
      })
      .join('');
  }

  function sourcesLine(sources, t, withButton) {
    var list = (sources || []).filter(function (s) { return s && s.name; });
    if (!list.length) return '';
    var parts = list
      .map(function (s) {
        return extLink(s.url, s.name, 'src-link') + (s.time ? '<span class="src-time"> · ' + esc(s.time) + '</span>' : '');
      })
      .join('<span class="src-sep">; </span>');
    var button = withButton && safeUrl(list[0].url) ? extLink(list[0].url, '+ ' + t.readSource, 'src-btn') : '';
    return '<div class="srcs"><span class="srcs-lbl">' + esc(t.sources) + ':</span> ' + parts + '</div>' + button;
  }

  function windowBadge(story, t) {
    if (story.inWindow === false) return '<span class="win-badge hist">' + esc(t.earlier) + '</span>';
    return '';
  }

  function tickerChips(list) {
    if (!list || !list.length) return '';
    return '<div class="tickers">' + list.map(function (s) { return '<span class="tick">' + esc(s) + '</span>'; }).join('') + '</div>';
  }

  function badge(b) {
    if (!b || !b.text) return '';
    return '<span class="ncat ' + (BADGE_OK.test(b.cls || '') ? b.cls : '') + '">' + esc(b.text) + '</span>';
  }

  // ---- header, banners, intro, strip ------------------------------------------------------
  function renderTopbar(ed, t) {
    return (
      '<header class="topbar"><div class="brandmark" aria-hidden="true">AI</div>' +
      '<div class="topbar-text"><h1>' + esc(ed.title) + '</h1><div class="sub">' + esc(ed.subtitle) + '</div></div>' +
      '<div class="clock" id="clock" role="timer" aria-label="' + esc(t.clock) + '">--:--:--</div></header>'
    );
  }

  function renderBanners(ed, opts, t) {
    var out = '';
    var qa = ed.qa || {};
    if (ed.demo) out += '<div class="banner banner-demo" role="note"><b>' + esc(t.demo) + '</b> ' + esc(t.demoText) + '</div>';
    if (qa.status === 'fallback') out += '<div class="banner banner-qa" role="note"><b>' + esc(t.fallback) + '</b> ' + esc(t.fallbackText) + '</div>';
    if (qa.status === 'degraded') out += '<div class="banner banner-qa" role="note"><b>' + esc(t.degraded) + '</b> ' + esc(t.degradedText) + '</div>';
    if (opts && opts.viewingLatest === false) {
      out += '<div class="banner banner-hist" role="note"><b>' + esc(t.historyBanner) + '</b> ' + esc(ed.timelineLabel) +
        ' · <a href="./">' + esc(t.goLatest) + '</a></div>';
    }
    out += '<div class="banner banner-new" id="new-edition" hidden><b>' + esc(t.newEdition) + '</b> <a href="./">' + esc(t.reload) + '</a></div>';
    return out;
  }

  function renderIntro(intro, t) {
    intro = intro || {};
    return (
      '<section class="intro" aria-label="' + esc(t.summary) + '"><p class="intro-lead"><b>' + esc(intro.greeting) + '</b> ' +
      '<span class="intro-headline">' + esc(intro.headline) + '</span></p>' +
      (intro.body ? '<p class="intro-body">' + esc(intro.body) + '</p>' : '') +
      (intro.stamp ? '<div class="stamp">' + esc(intro.stamp) + '</div>' : '') + '</section>'
    );
  }

  function renderStrip(strip, t) {
    if (!strip || !strip.tiles || !strip.tiles.length) return '';
    return (
      '<div class="kpi-strip" role="list" aria-label="' + esc(t.indicators) + '">' +
      strip.tiles
        .map(function (x) {
          return (
            '<div class="tile kpi-tile" role="listitem"><div class="nm">' + esc(x.name) +
            (x.mini ? ' <span class="mini">' + esc(x.mini) + '</span>' : '') + '</div>' +
            '<div class="vl">' + esc(x.value) + '</div>' +
            (x.change ? '<div class="ch ' + cls(x.cls) + '">' + esc(x.change) + '</div>' : '') + '</div>'
          );
        })
        .join('') +
      '</div>' + (strip.meta ? '<div class="kpi-meta">' + esc(strip.meta) + '</div>' : '')
    );
  }

  // ---- pages ---------------------------------------------------------------------------
  function topCard(c, t) {
    var slotCls = { lead: 'lead', alert: 'alert', mover: 'mkt' }[c.slot] || 'lead';
    var tk = c.ticker
      ? '<div class="mover-line"><span class="tick">' + esc(c.ticker.symbol) + '</span><span class="mover-name">' +
        esc(c.ticker.name) + '</span><span class="mover-px">' + esc(c.ticker.price) + '</span><span class="mover-ch ' +
        cls(c.ticker.cls) + '">' + esc(c.ticker.change) + '</span></div>'
      : '';
    return (
      '<article class="top-day-card ' + slotCls + '"><div class="td-kicker">' + esc(c.kicker) + '</div>' +
      (c.badge ? '<div class="td-badge">' + badge(c.badge) + '</div>' : '') +
      '<h3 class="td-title">' + esc(c.title) + '</h3>' + tk +
      (c.paragraph ? '<p class="td-parrafo">' + esc(c.paragraph) + '</p>' : '') +
      fields(c.fields) + sourcesLine(c.sources, t, true) + '</article>'
    );
  }

  function renderTop(p, t) {
    return '<div class="top-day">' + (p.cards || []).map(function (c) { return topCard(c, t); }).join('') + '</div>';
  }

  function storyCard(s, t, open) {
    return (
      '<article class="topnews-card"><details class="tn-details"' + (open ? ' open' : '') + '>' +
      '<summary class="tn-summary"><span class="tn-title-text">' + esc(s.title) + '</span>' + windowBadge(s, t) + '</summary>' +
      '<div class="tn-expand">' + fields(s.fields) + tickerChips(s.tickers) + '</div></details>' +
      '<div class="tn-foot">' + sourcesLine(s.sources, t, false) + '</div></article>'
    );
  }

  function renderLatest(p, t) {
    var blocks = (p.blocks || []).filter(function (b) { return b && b.stories && b.stories.length; });
    if (!blocks.length) return '<div class="empty-note">' + esc(p.empty) + '</div>';
    return (
      '<div class="tn-thematic">' +
      blocks
        .map(function (b) {
          return (
            '<section class="tn-block" aria-label="' + esc(b.title) + '"><div class="tn-block-head"><h3 class="tn-block-title">' +
            esc(b.title) + '</h3><span class="topnews-count">' + b.stories.length + '</span></div>' +
            '<div class="topnews-body">' + b.stories.map(function (s, i) { return storyCard(s, t, i === 0); }).join('') +
            '</div></section>'
          );
        })
        .join('') +
      '</div>'
    );
  }

  function renderWatchlist(p, t) {
    return (
      '<div class="wl-grid">' +
      (p.cards || [])
        .map(function (c) {
          var sent = c.sentiment ? '<span class="sent sent-' + safeId(c.sentiment.key) + '">' + esc(c.sentiment.label) + '</span>' : '';
          var earn = c.earnings ? '<span class="earn">' + esc(c.earnings) + '</span>' : '';
          var hl = (c.headlines || []).length
            ? '<div class="wl-hl-title">' + esc(t.headlines) + '</div><ul class="wl-hl">' +
              c.headlines
                .map(function (h) {
                  return '<li>' + extLink(h.url, h.title, 'wl-hl-link') + '<span class="wl-hl-meta">' + esc(h.name) +
                    (h.time ? ' · ' + esc(h.time) : '') + '</span></li>';
                })
                .join('') + '</ul>'
            : '<div class="wl-hl-empty">' + esc(t.noHeadlines) + '</div>';
          return (
            '<article class="wl-card wl-' + (cls(c.cls) || 'flat') + '"><header class="wl-head"><span class="tick">' + esc(c.symbol) +
            '</span><span class="wl-name">' + esc(c.name) + '</span><span class="wl-px">' + esc(c.price) +
            '</span><span class="wl-ch ' + cls(c.cls) + '">' + esc(c.change) + '</span></header>' +
            (sent || earn ? '<div class="wl-meta">' + sent + earn + '</div>' : '') +
            '<p class="wl-summary">' + esc(c.summary) + '</p>' +
            (c.watch ? '<div class="fld"><span class="lbl">' + esc(c.watch.label) + '</span>' + esc(c.watch.text) + '</div>' : '') +
            hl + '</article>'
          );
        })
        .join('') +
      '</div>'
    );
  }

  function tile(x) {
    return (
      '<div class="tile"><div class="nm">' + esc(x.name) + (x.mini ? ' <span class="mini">' + esc(x.mini) + '</span>' : '') +
      '</div><div class="vl">' + esc(x.value) + '</div>' + (x.change ? '<div class="ch ' + cls(x.cls) + '">' + esc(x.change) + '</div>' : '') + '</div>'
    );
  }

  function renderMarkets(p, t) {
    var mv = p.movers || {};
    var movers = (mv.items || []).length
      ? '<div class="mkt-block-title">' + esc(mv.title) + '</div><div class="co-list">' +
        mv.items
          .map(function (m) {
            return (
              '<details class="co-item"><summary><span class="co-ticker">' + esc(m.symbol) + '</span><span class="co-name">' +
              esc(m.name) + '</span><span class="co-px">' + esc(m.price) + '</span><span class="co-chg ' + cls(m.cls) + '">' +
              esc(m.change) + '</span></summary><div class="co-body"><div class="co-metrics">' +
              (m.metrics || [])
                .map(function (x) {
                  return '<div class="co-metric"><span class="co-ml">' + esc(x.label) + '</span><span class="co-mv ' + cls(x.cls) + '">' + esc(x.value) + '</span></div>';
                })
                .join('') + '</div></div></details>'
            );
          })
          .join('') + '</div>'
      : '';
    var sec = p.secondary || {};
    var secondary = (sec.tiles || []).length
      ? '<details class="sec"><summary data-show="' + esc(t.show) + '" data-hide="' + esc(t.hide) + '">' + esc(sec.summary) +
        '</summary><div class="inner"><div class="tiles">' + sec.tiles.map(tile).join('') + '</div>' +
        (sec.note ? '<div class="foot-note">' + esc(sec.note) + '</div>' : '') + '</div></details>'
      : '';
    var ag = p.agenda || {};
    var agenda =
      '<div class="agenda-wrap"><div class="card agenda-card"><div class="mkt-block-title">' + esc(ag.title) + '</div>' +
      ((ag.items || []).length
        ? '<ul class="agenda">' + ag.items.map(function (i) { return '<li><span class="hr">' + esc(i.when) + '</span><span>' + esc(i.text) + '</span></li>'; }).join('') + '</ul>'
        : '<div class="empty-note small">' + esc(ag.empty) + '</div>') +
      '</div></div>';
    return movers + secondary + agenda;
  }

  function sectorCard(s, t) {
    return (
      '<article class="card"><h3><span class="ttl">' + esc(s.title) + '</span>' + windowBadge(s, t) + '</h3>' +
      fields(s.fields) + tickerChips(s.tickers) + sourcesLine(s.sources, t, false) + '</article>'
    );
  }

  function renderSectors(p, t) {
    var tabs = p.tabs || [];
    if (!tabs.length) return '';
    var buttons = tabs
      .map(function (tab, i) {
        var id = safeId(tab.id);
        var perf = tab.perf ? ' <span class="tab-perf ' + cls(tab.perf.cls) + '">' + esc(tab.perf.change) + '</span>' : '';
        var count = (tab.stories || []).length ? ' <span class="tab-count">' + tab.stories.length + '</span>' : '';
        return (
          '<button type="button" class="tab' + (i === 0 ? ' active' : '') + '" role="tab" id="tab-' + id + '" aria-controls="pane-' + id +
          '" aria-selected="' + (i === 0 ? 'true' : 'false') + '" tabindex="' + (i === 0 ? '0' : '-1') + '" data-t="' + id + '">' +
          esc(tab.label) + perf + count + '</button>'
        );
      })
      .join('');
    var panes = tabs
      .map(function (tab, i) {
        var id = safeId(tab.id);
        var stories = tab.stories || [];
        var body = stories.length
          ? '<div class="grid2">' + stories.map(function (s) { return sectorCard(s, t); }).join('') + '</div>'
          : '<div class="empty-note">' + esc(tab.empty) + '</div>';
        return '<div class="tabpane' + (i === 0 ? ' active' : '') + '" role="tabpanel" id="pane-' + id + '" aria-labelledby="tab-' + id + '" data-p="' + id + '">' + body + '</div>';
      })
      .join('');
    return '<div class="tabs" role="tablist" aria-label="' + esc(p.title) + '">' + buttons + '</div>' + panes;
  }

  function renderRadar(p, t) {
    var kinds = { catalyst: 'ncat-b', risk: 'ncat-v', opportunity: 'ncat-o', data: 'ncat-n' };
    var signals = (p.signals || []).length
      ? '<div class="card radar-list">' +
        p.signals
          .map(function (s) {
            return (
              '<div class="radar-item"><span class="ncat ' + (kinds[s.kind] || 'ncat-n') + '">' + esc(s.label) + '</span>' +
              '<div class="radar-text">' + esc(s.text) + sourcesLine(s.sources, t, false) + '</div></div>'
            );
          })
          .join('') + '</div>'
      : '';
    var m = p.method || {};
    var method = (m.lines || []).length
      ? '<div class="card radar-method"><div class="mkt-block-title">' + esc(m.title) + '</div><ul class="method">' +
        m.lines.map(function (l) { return '<li>' + esc(l) + '</li>'; }).join('') + '</ul></div>'
      : '';
    return '<div class="radar-page">' + signals + method + '</div>';
  }

  var PAGES = {
    top: renderTop,
    latest: renderLatest,
    watchlist: renderWatchlist,
    markets: renderMarkets,
    sectors: renderSectors,
    radar: renderRadar
  };

  function renderPage(p, t) {
    var fn = PAGES[p.type];
    if (!fn) return '';
    var id = safeId(p.type);
    var count = p.countLabel ? '<span class="page-count">' + esc(p.countLabel) + '</span>' : '';
    return (
      '<section class="report-page" id="page-' + id + '" aria-labelledby="h-' + id + '">' +
      '<header class="report-page-head"><h2 class="report-page-title" id="h-' + id + '"><span class="report-page-num">' +
      esc(p.number) + '</span>' + esc(p.title) + count + '</h2></header>' +
      '<div class="report-page-body">' + fn(p, t) + '</div></section>'
    );
  }

  function renderFooter(ed, t) {
    var f = ed.footer || {};
    var qa = ed.qa || {};
    var srcs = (f.sources || []).map(function (s) { return extLink(s.url, s.name, 'foot-link'); }).join(' · ');
    var gen = ed.generatedAt ? new Date(ed.generatedAt) : null;
    var genText = gen && !isNaN(gen.getTime()) ? gen.toISOString().replace('T', ' ').slice(0, 16) + ' UTC' : '';
    var model = qa.modelLabel && qa.modelLabel !== 'demo' ? ' · ' + esc(qa.modelLabel) + ' · Amazon Bedrock' : '';
    return (
      '<footer class="site-footer">' +
      (srcs ? '<p><b>' + esc(f.sourcesLabel) + ':</b> ' + srcs + '</p>' : '') +
      (f.methodology ? '<p><b>' + esc(t.methodology) + ':</b> ' + esc(f.methodology) + '</p>' : '') +
      (f.disclaimer ? '<p class="disclaimer"><b>' + esc(t.disclaimer) + ':</b> ' + esc(f.disclaimer) + '</p>' : '') +
      '<p class="gen">' + esc(t.generated) + ' ' + esc(genText) + model + '</p></footer>'
    );
  }

  function renderTimeline(manifest, currentId, t) {
    var eds = ((manifest && manifest.editions) || []).slice(0, 30);
    var total = ((manifest && manifest.editions) || []).length;
    if (!eds.length) return '';
    var nodes = eds
      .map(function (e, i) {
        var isLatest = e.id === manifest.latest;
        var current = e.id === currentId;
        var href = isLatest ? './' : '?edition=' + encodeURIComponent(e.id);
        var node =
          '<a class="timeline-node' + (current ? ' current' : '') + '" href="' + esc(href) + '"' + (current ? ' aria-current="page"' : '') + '>' +
          '<span class="timeline-dot"></span><span class="timeline-card"><span class="timeline-date">' + esc(e.label || e.id) + '</span>' +
          '<span class="timeline-status' + (isLatest ? '' : ' history') + '">' + esc(isLatest ? t.latest : t.history) + '</span></span></a>';
        return (i > 0 ? '<span class="timeline-connector" aria-hidden="true"></span>' : '') + node;
      })
      .join('');
    return (
      '<nav class="report-timeline" aria-label="' + esc(t.timelineKicker) + '"><div class="timeline-shell">' +
      '<div class="timeline-head"><div><span class="timeline-kicker">' + esc(t.timelineKicker) + '</span>' +
      '<span class="timeline-title">' + esc(t.timelineTitle) + '</span></div><span class="timeline-count">' +
      total + ' ' + esc(total === 1 ? t.edition : t.editions) + '</span></div>' +
      '<div class="timeline-track">' + nodes + '</div><p class="timeline-help">' + esc(t.timelineHelp) + '</p></div></nav>'
    );
  }

  function renderEdition(ed, manifest, opts) {
    var t = ui(ed.language);
    return (
      renderTopbar(ed, t) +
      '<main class="wrap" id="main">' + renderBanners(ed, opts, t) + renderIntro(ed.intro, t) + renderStrip(ed.strip, t) +
      (ed.pages || []).map(function (p) { return renderPage(p, t); }).join('') + renderFooter(ed, t) + '</main>' +
      renderTimeline(manifest, ed.id, t)
    );
  }

  // ---- behaviour -------------------------------------------------------------------------
  function wireTabs(container) {
    function activate(btn) {
      var list = btn.closest('[role="tablist"]');
      var scope = list.parentElement;
      list.querySelectorAll('.tab').forEach(function (b) {
        var on = b === btn;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
        b.setAttribute('tabindex', on ? '0' : '-1');
      });
      scope.querySelectorAll('.tabpane').forEach(function (pane) {
        pane.classList.toggle('active', pane.getAttribute('data-p') === btn.getAttribute('data-t'));
      });
    }
    container.addEventListener('click', function (ev) {
      var btn = ev.target.closest && ev.target.closest('.tab');
      if (btn && container.contains(btn)) activate(btn);
    });
    container.addEventListener('keydown', function (ev) {
      var btn = ev.target.closest && ev.target.closest('.tab');
      if (!btn || (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft')) return;
      var tabs = Array.prototype.slice.call(btn.closest('[role="tablist"]').querySelectorAll('.tab'));
      var next = tabs[(tabs.indexOf(btn) + (ev.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      activate(next);
      next.focus();
      ev.preventDefault();
    });
  }

  function startClock(tz, label) {
    var el = document.getElementById('clock');
    if (!el) return;
    var fmt;
    try {
      fmt = new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    } catch (e) {
      fmt = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
      label = '';
    }
    function tick() {
      el.textContent = fmt.format(new Date()) + (label ? ' ' + label : '');
    }
    tick();
    setInterval(tick, 1000);
  }

  function getJSON(url, fresh) {
    return fetch(url, { cache: fresh ? 'no-store' : 'default', credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + url);
      return r.json();
    });
  }

  function findEdition(manifest, id) {
    var list = (manifest && manifest.editions) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id) return list[i];
    }
    return null;
  }

  function watchForNewEdition(currentId) {
    setInterval(function () {
      getJSON('data/manifest.json', true)
        .then(function (m) {
          if (m && m.latest && m.latest !== currentId) {
            var el = document.getElementById('new-edition');
            if (el) el.hidden = false;
          }
        })
        .catch(function () { /* offline or transient: ignore */ });
    }, 5 * 60 * 1000);
  }

  function boot() {
    var app = document.getElementById('app');
    var params = new URLSearchParams(window.location.search);
    var wanted = params.get('edition');
    getJSON('data/manifest.json', true)
      .then(function (manifest) {
        var entry = (wanted && findEdition(manifest, wanted)) || findEdition(manifest, manifest.latest);
        if (!entry || !FILE_OK.test(entry.file || '')) throw new Error(UI.en.noEditions);
        return getJSON('data/' + entry.file, false).then(function (ed) {
          return { manifest: manifest, ed: ed, entry: entry };
        });
      })
      .then(function (r) {
        var isLatest = r.entry.id === r.manifest.latest;
        document.documentElement.lang = r.ed.language || 'en';
        document.title = (r.ed.title || 'Dashboard') + ' — ' + (r.ed.timelineLabel || r.ed.id);
        app.innerHTML = renderEdition(r.ed, r.manifest, { viewingLatest: isLatest });
        wireTabs(app);
        startClock(r.ed.timezone || 'America/New_York', r.ed.tzLabel || '');
        if (isLatest) watchForNewEdition(r.ed.id);
      })
      .catch(function (err) {
        app.innerHTML = '<div class="tb-loading"><b>' + esc(UI.en.loadError) + '</b><br><small>' + esc(err && err.message) + '</small></div>';
      });
  }

  var api = { esc: esc, safeUrl: safeUrl, renderEdition: renderEdition, renderTimeline: renderTimeline, UI: UI };
  root.StockDash = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;

  if (typeof document === 'undefined' || root.__STOCKDASH_NO_BOOT__) return;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(typeof window !== 'undefined' ? window : globalThis);
