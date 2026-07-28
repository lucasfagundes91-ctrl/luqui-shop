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

  /* Menu gaveta: tira a coluna de ícones sem nome e devolve o menu completo
     atrás de um ☰. Só liga se o sistema tiver barra lateral fixa. */
  function montarGaveta() {
    if (!MOBILE() || document.querySelector('.mk-topbar')) return;
    var sb = document.querySelector('.sidebar');
    if (!sb) return;
    var pos = getComputedStyle(sb).position;
    if (pos !== 'fixed' && pos !== 'absolute') return;

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
    document.body.classList.remove('mk-gaveta', 'mk-aberta');
  }

  var agendado = null;
  function agendar() {
    if (agendado) return;
    agendado = setTimeout(function () {
      agendado = null;
      aplicar(); reforcarCampos(); quebrarLinhas();
      MOBILE() ? montarGaveta() : desmontarGaveta();
    }, 250);
  }

  function iniciar() {
    aplicar();
    reforcarCampos();
    quebrarLinhas();
    montarGaveta();
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
    setTimeout(aplicar, 1500); setTimeout(agendar, 3500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', iniciar);
  } else {
    iniciar();
  }
})();
