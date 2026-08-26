/* ════════════════════════════════════════════════════════════════════════
   Kit mobile Luqsys — v1
   Complemento em JS do mobile-kit.css. Só faz efeito em tela ≤900px
   (celular e iPad em pé).

   O que faz:
     1. Copia o cabeçalho da coluna (<th>) pra cada célula (data-mk), pra
        quando a tabela vira card no celular cada número dizer o que é.
     2. Marca a célula de ações e as células vazias (o CSS esconde).
     3. Embrulha tabela solta num container com rolagem horizontal própria.
     4. Reaplica sozinho quando a tela é re-renderizada (as telas do sistema
        são SPA: trocam o HTML inteiro no innerHTML).

   Não altera dado, não intercepta clique, não muda comportamento nenhum
   no desktop.
   ════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  var MOBILE = function () { return window.innerWidth <= 900; };

  function rotularTabela(tb) {
    // pega os rótulos do cabeçalho
    var ths = tb.querySelectorAll('thead th');
    if (!ths.length) {
      var pl = tb.querySelector('tr');
      ths = pl ? pl.querySelectorAll('th') : [];
    }
    if (!ths.length) return false;
    var rot = [];
    for (var i = 0; i < ths.length; i++) {
      rot.push((ths[i].textContent || '').trim().replace(/\s+/g, ' ').slice(0, 22));
    }
    var linhas = tb.querySelectorAll('tbody tr');
    if (!linhas.length) linhas = tb.querySelectorAll('tr');
    for (var l = 0; l < linhas.length; l++) {
      var tds = linhas[l].children;
      if (tds.length < 2) continue;           // linha de "nenhum resultado"
      for (var c = 0; c < tds.length; c++) {
        var td = tds[c];
        if (td.tagName !== 'TD') continue;
        var txt = (td.textContent || '').trim();
        // célula de ações: tem botão/link e nenhum texto próprio
        var temBotao = td.querySelector('button, a.btn, .btn');
        if (temBotao && txt.length < 30) { td.classList.add('mk-acoes'); continue; }
        if (!txt && !td.querySelector('img, svg, input, .pill')) {
          td.classList.add('mk-vazio'); continue;
        }
        td.classList.remove('mk-vazio');
        if (rot[c]) td.setAttribute('data-mk', rot[c]);
      }
    }
    tb.classList.add('mk-cards');
    return true;
  }

  function temScrollerAcima(el) {
    var p = el.parentElement;
    while (p && p !== document.body) {
      var ov = getComputedStyle(p).overflowX;
      if (ov === 'auto' || ov === 'scroll') return true;
      p = p.parentElement;
    }
    return false;
  }

  function aplicar() {
    if (!MOBILE()) return;
    var tabelas = document.querySelectorAll('table');
    for (var i = 0; i < tabelas.length; i++) {
      var tb = tabelas[i];
      try {
        rotularTabela(tb);
        // Tabela com muita coluna em tela estreita: espremer 6 colunas em
        // 390px deixa ~30px por coluna e o texto quebra letra a letra. Vira
        // lista de cards (o rótulo de cada valor já foi posto acima).
        var pl = tb.querySelector('tr');
        var nCols = pl ? pl.children.length : 0;
        var apertada = nCols >= 4 || tb.scrollWidth > window.innerWidth + 2;
        if (window.innerWidth <= 600 && apertada) {
          tb.classList.add('mk-card-tbl');
        } else {
          tb.classList.remove('mk-card-tbl');
        }
        // tabela sem nenhum container rolável e mais larga que a tela
        if (!temScrollerAcima(tb) && tb.getBoundingClientRect().width > window.innerWidth + 2) {
          var pai = tb.parentElement;
          if (pai && !pai.classList.contains('mk-scroll')) {
            var wrap = document.createElement('div');
            wrap.className = 'mk-scroll';
            pai.insertBefore(wrap, tb);
            wrap.appendChild(tb);
          }
        }
      } catch (e) { /* uma tabela problemática não derruba o resto */ }
    }
  }

  /* Campos que continuaram com fonte < 16px porque a folha de estilo do
     sistema usa !important. Sem isso o iPhone dá zoom ao tocar no campo e o
     operador precisa fechar o zoom na mão a cada digitação. */
  function reforcarCampos() {
    if (!MOBILE()) return;
    var campos = document.querySelectorAll('input, select, textarea');
    for (var i = 0; i < campos.length; i++) {
      var c = campos[i];
      if (c.type === 'checkbox' || c.type === 'radio' || c.type === 'hidden' || c.type === 'range') continue;
      if (c.dataset.mkFs) continue;
      var fs = parseFloat(getComputedStyle(c).fontSize);
      if (fs && fs < 16) {
        c.style.setProperty('font-size', '16px', 'important');
        c.dataset.mkFs = '1';
      }
    }
  }

  /* Barra em linha única (filtros, botões de ação, menu de topo) que não cabe
     na largura do telefone: o que passa da borda fica inalcançável porque a
     linha não quebra nem rola. Só mexe em quem JÁ está estourando. */
  function quebrarLinhas() {
    if (!MOBILE()) return;
    var els = document.querySelectorAll('div, nav, header, section, ul, form');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.dataset.mkWrap) continue;
      var cs = getComputedStyle(el);
      if (cs.display.indexOf('flex') === -1) continue;
      if (cs.flexWrap !== 'nowrap') continue;
      // barra que já rola de propósito (abas) fica como está
      if (cs.overflowX === 'auto' || cs.overflowX === 'scroll') continue;
      var r = el.getBoundingClientRect();
      // três jeitos de não caber: conteúdo maior que o próprio elemento, o
      // elemento passando da borda da tela, ou o elemento sendo mais largo
      // que o espaço que o pai tem pra dar.
      var naoCabe = el.scrollWidth > el.clientWidth + 2 || r.right > window.innerWidth + 2;
      if (!naoCabe && el.parentElement) naoCabe = r.width > el.parentElement.clientWidth + 2;
      if (naoCabe) {
        // Barra de navegação (fila de ícones/abas): quebrar em várias linhas
        // vira uma coluna gigante ocupando meia tela. Melhor deixar em uma
        // linha só, rolando de lado.
        var clic = el.querySelectorAll(':scope > a, :scope > button').length;
        if (clic >= 4 && clic >= el.children.length - 1) {
          el.style.setProperty('overflow-x', 'auto', 'important');
          el.style.setProperty('flex-wrap', 'nowrap', 'important');
          el.style.setProperty('max-width', '100%', 'important');
          el.style.setProperty('-webkit-overflow-scrolling', 'touch');
        } else {
          el.style.setProperty('flex-wrap', 'wrap', 'important');
          el.style.setProperty('max-width', '100%', 'important');
        }
        el.dataset.mkWrap = '1';
      }
    }
    // Grid espremido: 4-5 colunas em 390px deixam ~40px por coluna e cada
    // palavra do rótulo ("Disponíveis", "pendentes") quebra no meio. Com
    // coluna abaixo de 90px, passa a 2 colunas.
    if (window.innerWidth <= 600) {
      var todos = document.querySelectorAll('*');
      for (var t = 0; t < todos.length; t++) {
        var g = todos[t];
        if (g.dataset.mkGridN) continue;
        var cg = getComputedStyle(g);
        if (cg.display.indexOf('grid') === -1) continue;
        var cols = cg.gridTemplateColumns.split(' ').filter(Boolean);
        if (cols.length < 3) continue;
        var estreita = cols.some(function (c) { var v = parseFloat(c); return v && v < 90; });
        if (!estreita) continue;
        g.style.setProperty('grid-template-columns', 'repeat(2, minmax(0, 1fr))', 'important');
        g.dataset.mkGridN = '1';
      }
    }
    // grid com colunas fixas em px (ex: "1fr 170px 170px 180px 200px" numa
    // linha de filtros) que nao cabe: vira uma coluna no celular, e no iPad
    // em pe quebra em quantas couberem. Vitrine com minmax()/auto-fill nao
    // entra aqui porque ela ja se ajusta e nao estoura.
    var grids = document.querySelectorAll('[style*="grid-template-columns"]');
    for (var g = 0; g < grids.length; g++) {
      var el2 = grids[g];
      if (el2.dataset.mkGrid) continue;
      if (el2.scrollWidth > el2.clientWidth + 2 || el2.getBoundingClientRect().right > window.innerWidth + 2) {
        el2.style.setProperty('grid-template-columns',
          window.innerWidth <= 600 ? '1fr' : 'repeat(auto-fit, minmax(170px, 1fr))', 'important');
        el2.dataset.mkGrid = '1';
      }
    }
  }

  /* Texto que o CSS mandou não quebrar (white-space:nowrap) e que não cabe:
     no desktop sobra largura, no celular ele some cortado na borda. Volta a
     quebrar linha — só nos que estão realmente cortados. */
  function destravarTextoCortado() {
    if (!MOBILE()) return;
    var els = document.querySelectorAll('div, span, td, th, p, li, button, a, label, h1, h2, h3');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.dataset.mkWs) continue;
      var cs = getComputedStyle(el);
      if (cs.whiteSpace !== 'nowrap' && cs.whiteSpace !== 'pre') continue;
      if (cs.overflowX === 'auto' || cs.overflowX === 'scroll') continue;
      if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
        el.style.setProperty('white-space', 'normal', 'important');
        el.dataset.mkWs = '1';
      }
    }
  }

  /* word-break:break-all na folha do sistema parte QUALQUER palavra no meio
     ("Shop/ping"), e às vezes vence o kit por especificidade. Onde o texto é
     uma frase (tem espaço), desliga no próprio elemento — inline com
     !important ganha de qualquer seletor. Hash/URL sem espaço fica como está. */
  function soltarBreakAll() {
    if (!MOBILE()) return;
    var els = document.querySelectorAll('td, th, div, span, p, li, b, strong, small, label, a');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.dataset.mkWb || el.children.length) continue;
      if (getComputedStyle(el).wordBreak !== 'break-all') continue;
      var t = (el.textContent || '').trim();
      if (t.length < 4 || t.indexOf(' ') === -1) continue;   // sem espaço: pode ser hash/URL
      el.style.setProperty('word-break', 'normal', 'important');
      el.style.setProperty('overflow-wrap', 'break-word', 'important');
      el.dataset.mkWb = '1';
    }
  }

  /* Valor em dinheiro não pode quebrar: "R$ 78.505,87" virando "78.505," numa
     linha e "87" na outra é o pior jeito de mostrar um número. Em vez de
     quebrar, o valor encolhe a fonte até caber (limite de ~38% menor; se nem
     assim couber, deixa quebrar — melhor ilegível pequeno do que cortado). */
  var RE_VALOR = /^[-+]?[R$\s]*[-+]?[\d][\d.,]*\s*(%|kWh|km|un|x)?$/i;
  function ajustarValores() {
    if (!MOBILE()) return;
    var els = document.querySelectorAll('div, span, td, b, strong, h1, h2, h3, p, small');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      // Já ajustado antes: se o cartão alargou depois (o kit muda o grid na
      // mesma passada), devolve a fonte original e mede de novo — senão o
      // valor fica miúdo à toa num espaço que agora cabe.
      if (el.dataset.mkVal) {
        if (!el.dataset.mkFs0) continue;
        var f0 = parseFloat(el.dataset.mkFs0);
        el.style.setProperty('font-size', f0 + 'px', 'important');
        el.style.setProperty('white-space', 'nowrap', 'important');
        if (el.scrollWidth <= el.clientWidth + 1) { el.style.removeProperty('font-size'); continue; }
        delete el.dataset.mkVal;
      }
      if (el.children.length > 2) continue;
      var soInline = true;
      for (var k = 0; k < el.children.length; k++) {
        var d = getComputedStyle(el.children[k]).display;
        if (d !== 'inline' && d !== 'inline-block') { soInline = false; break; }
      }
      if (!soInline) continue;
      var t = (el.textContent || '').trim();
      if (t.length < 5 || t.length > 22 || !RE_VALOR.test(t)) continue;
      var cw = el.clientWidth;
      if (!cw) continue;
      var fsOrig = parseFloat(el.dataset.mkFs0 || getComputedStyle(el).fontSize);
      el.dataset.mkFs0 = fsOrig;
      el.style.setProperty('white-space', 'nowrap', 'important');
      var fs = fsOrig, min = fsOrig * 0.55, n = 0;
      while (el.scrollWidth > cw + 1 && fs > min && n < 14) {
        fs -= 1; el.style.setProperty('font-size', fs + 'px', 'important'); n++;
      }
      if (el.scrollWidth > cw + 1) {
        // Nem encolhendo coube: deixa quebrar (mantendo a fonte menor). Tem
        // que ser 'normal' explícito — só tirar o inline devolve o nowrap que
        // veio da folha de estilo do sistema, e aí o valor fica CORTADO na
        // borda do cartão, que é pior que quebrar em duas linhas.
        el.style.setProperty('white-space', 'normal', 'important');
      }
      el.dataset.mkVal = '1';
    }
  }

  /* Fila de cartões de indicador (KPI) espremida: 4 cartões numa linha de
     390px dão ~60px cada e "atrasadas" quebra no meio. Passa a 2 por linha. */
  function alargarCartoes() {
    if (window.innerWidth > 600) return;
    var els = document.querySelectorAll('div, ul, section');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.dataset.mkKpi) continue;
      var cs = getComputedStyle(el);
      if (cs.display.indexOf('flex') === -1) continue;
      var filhos = el.children;
      if (filhos.length < 3) continue;
      // barra de botões de ação não é fila de indicador: forçar 50% em cada
      // corta o rótulo ("📋 Encargos" em 56px). Só cartão mesmo.
      var temControle = false;
      for (var b0 = 0; b0 < filhos.length; b0++) {
        var tg = filhos[b0].tagName;
        if (tg === 'BUTTON' || tg === 'A' || tg === 'INPUT' || tg === 'SELECT' || tg === 'LABEL') { temControle = true; break; }
      }
      if (temControle) continue;
      var estreitos = 0;
      for (var f = 0; f < filhos.length; f++) {
        var w = filhos[f].getBoundingClientRect().width;
        if (w > 0 && w < 90) estreitos++;
      }
      if (estreitos < 3) continue;
      el.style.setProperty('flex-wrap', 'wrap', 'important');
      for (var f2 = 0; f2 < filhos.length; f2++) {
        filhos[f2].style.setProperty('flex', '1 1 calc(50% - 8px)', 'important');
        filhos[f2].style.setProperty('min-width', '0', 'important');
      }
      el.dataset.mkKpi = '1';
    }
  }

  /* Rede de segurança: o que ainda estiver passando da borda da tela é contido
     no lugar. Dois casos que as regras por classe não pegam: um <b> com nome de
     arquivo gigante sem espaço dentro de um <div style="display:flex"> (sem
     classe, então nenhum seletor de flex casa) e um <select> com opção longa
     dentro de um label que já estourava. */
  function conterEstouros() {
    if (!MOBILE()) return;
    var vw = window.innerWidth;
    var els = document.querySelectorAll('body *');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.dataset.mkFix) continue;
      if (el.closest('.sidebar') || el.classList.contains('mk-topbar')) continue;
      var r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      if (r.right <= vw + 2) continue;
      var p = el.parentElement, dentroScroller = false;
      while (p && p !== document.body) {
        var ov = getComputedStyle(p).overflowX;
        if (ov === 'auto' || ov === 'scroll') { dentroScroller = true; break; }
        p = p.parentElement;
      }
      if (dentroScroller) continue;           // rola de propósito, deixa
      el.style.setProperty('max-width', '100%', 'important');
      el.style.setProperty('overflow-wrap', 'break-word', 'important');
      el.dataset.mkFix = '1';
      // ancestral flex/grid precisa de min-width:0, senão o filho nunca encolhe
      var q = el.parentElement, n = 0;
      while (q && q !== document.body && n < 6) {
        var cs = getComputedStyle(q);
        if (cs.display.indexOf('flex') >= 0 || cs.display.indexOf('grid') >= 0) {
          q.style.setProperty('min-width', '0', 'important');
        }
        q = q.parentElement; n++;
      }
    }
  }

  /* Menu gaveta: tira a coluna de ícones sem nome e devolve o menu completo
     atrás de um ☰. Só liga se o sistema tiver barra lateral fixa. */
  function montarGaveta() {
    if (!MOBILE() || document.querySelector('.mk-topbar')) return;
    var sb = document.querySelector('.sidebar, aside.sidebar, aside[class*="sidebar"], nav.sidebar, #sidebar');
    if (!sb) return;
    var cs0 = getComputedStyle(sb);
    var largura = sb.getBoundingClientRect().width;
    // Vale a gaveta quando a barra é flutuante (fixed/absolute) OU quando ela é
    // uma coluna do layout que come a largura da tela — o caso do
    // ContabilidadePro: 260px de menu em 390px de tela deixavam ~130px pro
    // conteúdo e o texto saía uma letra por linha.
    var flutua = cs0.position === 'fixed' || cs0.position === 'absolute';
    var comeATela = largura >= window.innerWidth * 0.28;
    if (!flutua && !comeATela) return;
    if (!flutua) sb.dataset.mkFixar = '1';

    var barra = document.createElement('div');
    barra.className = 'mk-topbar';
    var bt = document.createElement('button');
    bt.setAttribute('aria-label', 'Menu');
    bt.textContent = '☰';
    var tit = document.createElement('div');
    tit.className = 'mk-tit';
    var logo = sb.querySelector('.sb-logo');
    tit.textContent = (logo ? logo.textContent : document.title).trim().slice(0, 28);
    barra.appendChild(bt); barra.appendChild(tit);

    var fundo = document.createElement('div');
    fundo.className = 'mk-fundo';

    document.body.appendChild(barra);
    document.body.appendChild(fundo);
    document.body.classList.add('mk-gaveta');
    if (sb.dataset.mkFixar) document.body.classList.add('mk-gaveta-fixar');

    var abrir = function () { document.body.classList.add('mk-aberta'); };
    var fechar = function () { document.body.classList.remove('mk-aberta'); };
    bt.addEventListener('click', function (e) {
      e.stopPropagation();
      document.body.classList.contains('mk-aberta') ? fechar() : abrir();
    });
    fundo.addEventListener('click', fechar);
    // escolheu um item do menu: fecha a gaveta e mostra a tela
    sb.addEventListener('click', function (e) {
      if (e.target.closest('button, a')) setTimeout(fechar, 60);
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') fechar(); });
  }

  function desmontarGaveta() {
    if (MOBILE()) return;
    var b = document.querySelector('.mk-topbar'), f = document.querySelector('.mk-fundo');
    if (b) b.remove();
    if (f) f.remove();
    document.body.classList.remove('mk-gaveta', 'mk-aberta', 'mk-gaveta-fixar');
  }

  var agendado = null;
  function agendar() {
    if (agendado) return;
    agendado = setTimeout(function () {
      agendado = null;
      aplicar(); reforcarCampos(); quebrarLinhas(); destravarTextoCortado(); soltarBreakAll(); alargarCartoes(); ajustarValores(); conterEstouros();
      MOBILE() ? montarGaveta() : desmontarGaveta();
    }, 250);
  }

  function iniciar() {
    aplicar();
    reforcarCampos();
    quebrarLinhas();
    destravarTextoCortado();
    soltarBreakAll();
    montarGaveta();
    alargarCartoes();
    ajustarValores();
    conterEstouros();
    // as telas são SPA: o conteúdo troca sem recarregar a página
    try {
      new MutationObserver(function (muts) {
        for (var i = 0; i < muts.length; i++) {
          if (muts[i].addedNodes && muts[i].addedNodes.length) { agendar(); return; }
        }
      }).observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
    window.addEventListener('resize', agendar);
    window.addEventListener('orientationchange', agendar);
    /* Em vários sistemas a troca de tela não cria elemento nenhum: as telas já
       estão no HTML e a navegação só alterna display. Aí o observador acima
       não vê nada — por isso reavalia também depois de cada toque. */
    document.addEventListener('click', agendar, true);
    /* telas que só aparecem depois do fetch */
    setTimeout(agendar, 1200); setTimeout(agendar, 3000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', iniciar);
  } else {
    iniciar();
  }
})();

/* ═══════════════════════════════════════════════════════════════════════════
   PUXAR PRA ATUALIZAR + botão de recarregar          (kit mobile Luqsys)

   No app instalado (PWA) não existe barra do navegador: nem botão de
   recarregar, nem o "puxa pra baixo" do Safari. Sem isso a pessoa fecha e
   reabre o app pra ver informação nova. Bloco autocontido de propósito — dá
   pra colar no fim de qualquer mobile-kit.js sem mexer no que já existe.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  if (window.__mkRefresh) return;
  window.__mkRefresh = true;

  function ehApp() {
    try {
      return window.matchMedia('(display-mode: standalone)').matches
          || window.navigator.standalone === true;
    } catch (e) { return false; }
  }

  /* Sobe pelos pais procurando quem realmente rola: se a lista de dentro já
     está rolada, o gesto é dela, não de atualizar. */
  function alvoDeRolagem(el) {
    while (el && el !== document.body && el !== document.documentElement) {
      var st = null;
      try { st = getComputedStyle(el); } catch (e) {}
      if (st && /(auto|scroll)/.test(st.overflowY) && el.scrollHeight > el.clientHeight + 4) return el;
      el = el.parentElement;
    }
    return null;
  }

  function montar() {
    var barra = document.createElement('div');
    barra.id = 'mk-puxar';
    barra.innerHTML = '<span class="mk-puxar-txt">↓ puxe pra atualizar</span>';
    document.body.appendChild(barra);

    var btn = document.createElement('button');
    btn.id = 'mk-recarregar';
    btn.type = 'button';
    btn.title = 'Atualizar';
    btn.setAttribute('aria-label', 'Atualizar');
    btn.textContent = '↻';
    btn.addEventListener('click', function () {
      btn.classList.add('girando');
      location.reload();
    });
    document.body.appendChild(btn);

    var y0 = null, dist = 0, ativo = false;
    var LIMITE = 70;

    document.addEventListener('touchstart', function (ev) {
      if (ev.touches.length !== 1) return;
      var rolando = alvoDeRolagem(ev.target);
      if (rolando && rolando.scrollTop > 2) return;
      if (window.scrollY > 2) return;
      y0 = ev.touches[0].clientY; dist = 0; ativo = true;
    }, { passive: true });

    document.addEventListener('touchmove', function (ev) {
      if (!ativo || y0 === null) return;
      dist = ev.touches[0].clientY - y0;
      if (dist <= 0) { barra.classList.remove('on', 'pronto'); return; }
      barra.classList.add('on');
      barra.style.transform = 'translateY(' + Math.min(dist, LIMITE + 30) + 'px)';
      barra.classList.toggle('pronto', dist >= LIMITE);
      barra.querySelector('.mk-puxar-txt').textContent =
        dist >= LIMITE ? '↻ solte pra atualizar' : '↓ puxe pra atualizar';
    }, { passive: true });

    function soltar() {
      if (!ativo) return;
      ativo = false;
      if (dist >= LIMITE) {
        barra.querySelector('.mk-puxar-txt').textContent = '↻ atualizando…';
        location.reload();
        return;
      }
      barra.classList.remove('on', 'pronto');
      barra.style.transform = '';
      y0 = null; dist = 0;
    }
    document.addEventListener('touchend', soltar, { passive: true });
    document.addEventListener('touchcancel', soltar, { passive: true });
  }

  function iniciar() {
    var estreito = false;
    try { estreito = window.matchMedia('(max-width: 900px)').matches; } catch (e) {}
    if (!ehApp() && !estreito) return;
    document.body.classList.add('mk-app');
    montar();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', iniciar);
  } else {
    iniciar();
  }
})();
