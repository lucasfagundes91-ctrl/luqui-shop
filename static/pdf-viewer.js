/* Visualizador de PDF (e de anexo que é foto) pra quando o sistema roda como
   app instalado (PWA).

   O problema: no iPhone, com o app aberto pelo ícone da tela de início
   (standalone), não existe barra do navegador. Abrir um recibo/contrato com
   window.open troca a tela inteira pelo PDF e não sobra NENHUM botão de voltar
   — o jeito de sair é fechar o app e abrir de novo, perdendo a tela que estava.

   A solução: quando está em standalone, o window.open de um endereço do próprio
   sistema que tem cara de documento (ver pareceDocumento) é interceptado — o
   resto passa direto. O arquivo é baixado por fetch e, se for PDF mesmo,
   desenhado aqui dentro numa camada por cima do app, com cabeçalho próprio
   ("‹ Voltar", compartilhar, baixar, zoom). O app continua vivo atrás: fechar a
   camada devolve exatamente a tela de onde saiu. O botão/gesto de voltar do
   sistema também fecha (entra uma entrada no histórico).

   Se não for PDF (JSON de erro, página HTML), cai no window.open original.
   Fora do app instalado (navegador comum) nada muda — lá a aba nova funciona.

   Depende de static/vendor/pdfjs (Mozilla PDF.js, build legacy), carregado só
   na primeira vez que abre um PDF. Copiado em cada repo, junto do mobile-kit. */
(function () {
  'use strict';

  var PDFJS_SRC  = '/static/vendor/pdfjs/pdf.min.js?v=1';
  var WORKER_SRC = '/static/vendor/pdfjs/pdf.worker.min.js?v=1';

  var STANDALONE = (window.matchMedia && (
                      window.matchMedia('(display-mode: standalone)').matches ||
                      window.matchMedia('(display-mode: fullscreen)').matches ||
                      window.matchMedia('(display-mode: minimal-ui)').matches)) ||
                   window.navigator.standalone === true;

  var COR = (document.querySelector('meta[name="theme-color"]') || {}).content || '#0f172a';
  var abrirNativo = window.open.bind(window);

  var atual = null;      // { fundo, area, doc, blob, nome, zoom, token, comHistorico }
  var pdfjsPromise = null;

  // ── PDF.js sob demanda ────────────────────────────────────────────────────
  function carregarPdfjs() {
    if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
    if (pdfjsPromise) return pdfjsPromise;
    pdfjsPromise = new Promise(function (ok, falhou) {
      var s = document.createElement('script');
      s.src = PDFJS_SRC;
      s.onload = function () {
        if (!window.pdfjsLib) return falhou(new Error('pdfjsLib não carregou'));
        window.pdfjsLib.GlobalWorkerOptions.workerSrc = WORKER_SRC;
        ok(window.pdfjsLib);
      };
      s.onerror = function () { pdfjsPromise = null; falhou(new Error('falha ao carregar o PDF.js')); };
      document.head.appendChild(s);
    });
    return pdfjsPromise;
  }

  // ── utilidades ────────────────────────────────────────────────────────────
  function mesmaOrigem(url) {
    try {
      var u = new URL(String(url), location.href);
      return u.origin === location.origin && /^https?:$/.test(u.protocol);
    } catch (e) { return false; }
  }

  /* Só intercepta o que TEM CARA de PDF, e isso é decidido antes de qualquer
     fetch. Motivo: vários sistemas abrem HTML em janela nova (impressão de
     cupom, etiqueta, prévia). Se a decisão dependesse da resposta do servidor,
     o window.open de socorro sairia fora do toque do usuário e o iOS bloquearia
     — a janela simplesmente não abriria. Na dúvida, deixa passar direto. */
  var NAO = /(imprimir|print|etiqueta|cupom|xml|xlsx|excel|csv|zip|txt|exportar|caixa|comanda)/;
  var SIM = /(recibo|demonstrativo|presta[cç][aã]o|comprovante|boleto|danfe|carn[eê]|fatura|espelho|apresenta[cç]|anexo|arquivo)/;

  function pareceDocumento(url) {
    try {
      var u = new URL(String(url), location.href);
      var p = u.pathname.toLowerCase(), q = (u.search || '').toLowerCase();
      if (NAO.test(p) || NAO.test(q)) return false;     // tela de impressão, planilha, texto
      return /\.pdf$/.test(p) || /(^|\/)pdf(\/|$)/.test(p) || /pdf/.test(q) || SIM.test(p);
    } catch (e) { return false; }
  }

  function nomeArquivo(resp, url) {
    var cd = (resp && resp.headers.get('content-disposition')) || '';
    var m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd);
    if (m) { try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; } }
    var tipo = (resp && (resp.headers.get('content-type') || '')).toLowerCase();
    var ext = /^image\/(\w+)/.test(tipo) ? '.' + RegExp.$1.replace('jpeg', 'jpg') : '.pdf';
    try {
      var p = new URL(String(url), location.href).pathname.split('/').filter(Boolean);
      var f = p.pop() || 'documento';
      return /\.(pdf|jpe?g|png|webp|gif)$/i.test(f) ? f : f + ext;
    } catch (e) { return 'documento' + ext; }
  }

  function titulo(nome) {
    var t = String(nome).replace(/\.(pdf|jpe?g|png|webp|gif)$/i, '').replace(/[_-]+/g, ' ').trim();
    return t ? t.charAt(0).toUpperCase() + t.slice(1) : 'Documento';
  }

  function botao(rotulo, titulo_, principal) {
    var b = document.createElement('button');
    b.type = 'button';
    b.innerHTML = rotulo;
    if (titulo_) b.title = titulo_;
    b.style.cssText = 'min-height:38px;min-width:38px;padding:0 12px;border:none;border-radius:9px;' +
      'font:600 15px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;' +
      'display:inline-flex;align-items:center;justify-content:center;gap:6px;cursor:pointer;' +
      (principal ? 'background:rgba(255,255,255,.18);color:#fff'
                 : 'background:rgba(255,255,255,.10);color:#fff');
    return b;
  }

  // ── camada ────────────────────────────────────────────────────────────────
  function montarCamada(nome) {
    var fundo = document.createElement('div');
    fundo.setAttribute('role', 'dialog');
    fundo.style.cssText = [
      'position:fixed', 'inset:0', 'z-index:2147483100', 'background:#334155',
      'display:flex', 'flex-direction:column',
      'font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif'
    ].join(';');

    var barra = document.createElement('div');
    barra.style.cssText = [
      'flex:0 0 auto', 'display:flex', 'align-items:center', 'gap:8px',
      'padding:8px 10px', 'padding-top:calc(8px + env(safe-area-inset-top))',
      'background:' + COR, 'color:#fff', 'box-shadow:0 2px 8px rgba(0,0,0,.25)'
    ].join(';');

    var voltar = botao('&#8249;&nbsp;Voltar', 'Voltar pra tela anterior', true);
    voltar.style.fontWeight = '700';

    var titulo_ = document.createElement('div');
    titulo_.textContent = titulo(nome);
    titulo_.style.cssText = 'flex:1;min-width:0;font-weight:600;font-size:14px;' +
      'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:center;opacity:.95';

    barra.appendChild(voltar);
    barra.appendChild(titulo_);

    var area = document.createElement('div');
    area.style.cssText = [
      'flex:1 1 auto', 'overflow:auto', '-webkit-overflow-scrolling:touch',
      'padding:10px 8px calc(16px + env(safe-area-inset-bottom))',
      'display:flex', 'flex-direction:column', 'align-items:center', 'gap:10px'
    ].join(';');

    fundo.appendChild(barra);
    fundo.appendChild(area);
    document.body.appendChild(fundo);

    return { fundo: fundo, barra: barra, area: area, voltar: voltar, titulo: titulo_ };
  }

  function aviso(area, texto, acao) {
    area.innerHTML = '';
    var cx = document.createElement('div');
    cx.style.cssText = 'background:#fff;color:#0f172a;border-radius:14px;padding:18px;margin-top:24px;' +
      'max-width:420px;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.25)';
    var p = document.createElement('div');
    p.textContent = texto;
    p.style.cssText = 'margin-bottom:' + (acao ? '14px' : '0');
    cx.appendChild(p);
    if (acao) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = acao.rotulo;
      b.style.cssText = 'min-height:42px;padding:0 18px;border:none;border-radius:9px;font-size:15px;' +
        'font-weight:600;background:' + COR + ';color:#fff;cursor:pointer';
      b.addEventListener('click', acao.fn);
      cx.appendChild(b);
    }
    area.appendChild(cx);
  }

  // Anexo que é foto (comprovante fotografado, nota escaneada) abre aqui do
  // mesmo jeito — em standalone a foto também tomava a tela sem volta.
  function desenharImagem() {
    var estado = atual;
    estado.area.innerHTML = '';
    var img = document.createElement('img');
    img.src = estado.blobUrl;
    img.alt = estado.nome;
    img.style.cssText = 'width:' + Math.round(100 * estado.zoom) + '%;max-width:none;height:auto;' +
      'border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.3);flex:0 0 auto;background:#fff';
    estado.area.appendChild(img);
  }

  function girando(area) {
    area.innerHTML = '';
    var d = document.createElement('div');
    d.style.cssText = 'color:#e2e8f0;margin-top:40px;font-size:15px';
    d.textContent = 'Abrindo documento…';
    area.appendChild(d);
  }

  // ── desenho das páginas ───────────────────────────────────────────────────
  function desenhar() {
    if (!atual) return;
    if (atual.imagem) return desenharImagem();
    if (!atual.doc) return;
    var estado = atual, doc = estado.doc;
    var token = ++estado.token;
    var area = estado.area;
    var larguraBase = Math.max(240, Math.min(area.clientWidth - 16, 1100));
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    area.innerHTML = '';

    var seq = Promise.resolve();
    for (var i = 1; i <= doc.numPages; i++) {
      (function (n) {
        seq = seq.then(function () {
          if (atual !== estado || estado.token !== token) return;
          return doc.getPage(n).then(function (page) {
            if (atual !== estado || estado.token !== token) return;
            var vp1 = page.getViewport({ scale: 1 });
            var escala = (larguraBase / vp1.width) * estado.zoom;
            var vp = page.getViewport({ scale: escala * dpr });
            var cv = document.createElement('canvas');
            cv.width = Math.round(vp.width);
            cv.height = Math.round(vp.height);
            cv.style.cssText = 'width:' + Math.round(vp.width / dpr) + 'px;height:auto;max-width:none;' +
              'background:#fff;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.3);flex:0 0 auto';
            area.appendChild(cv);
            return page.render({ canvasContext: cv.getContext('2d'), viewport: vp }).promise;
          });
        });
      })(i);
    }
    seq.catch(function (e) {
      if (atual === estado && estado.token === token) {
        aviso(area, 'Não consegui desenhar o documento: ' + (e && e.message ? e.message : e), null);
      }
    });
  }

  // ── abrir / fechar ────────────────────────────────────────────────────────
  function fechar(viaHistorico) {
    if (!atual) return;
    var estado = atual;
    atual = null;
    try { estado.fundo.remove(); } catch (e) {}
    if (estado.blobUrl) { try { URL.revokeObjectURL(estado.blobUrl); } catch (e) {} }
    document.documentElement.style.overflow = estado.overflowAntes || '';
    window.removeEventListener('resize', estado.aoRedimensionar);
    document.removeEventListener('keydown', estado.aoTeclar);
    if (!viaHistorico && estado.comHistorico) {
      estado.comHistorico = false;
      try { history.back(); } catch (e) {}
    }
  }

  function abrir(url, nomeSugerido) {
    if (atual) fechar();

    var cam = montarCamada(nomeSugerido || 'documento.pdf');
    var estado = {
      fundo: cam.fundo, barra: cam.barra, area: cam.area, titulo: cam.titulo,
      doc: null, blob: null, blobUrl: null, nome: nomeSugerido || 'documento.pdf',
      zoom: 1, token: 0, comHistorico: false,
      overflowAntes: document.documentElement.style.overflow
    };
    atual = estado;
    document.documentElement.style.overflow = 'hidden';

    cam.voltar.addEventListener('click', function () { fechar(false); });

    estado.aoTeclar = function (e) { if (e.key === 'Escape') fechar(false); };
    document.addEventListener('keydown', estado.aoTeclar);

    var tmr = null;
    estado.aoRedimensionar = function () {
      clearTimeout(tmr);
      tmr = setTimeout(function () { if (atual === estado && estado.doc) desenhar(); }, 250);
    };
    window.addEventListener('resize', estado.aoRedimensionar);

    // Voltar do sistema (gesto/botão) fecha a camada em vez de sair da tela.
    try {
      history.pushState({ luqpdf: 1 }, '', location.href);
      estado.comHistorico = true;
      window.addEventListener('popstate', function ouvinte(ev) {
        window.removeEventListener('popstate', ouvinte);
        if (atual === estado) { estado.comHistorico = false; fechar(true); }
      });
    } catch (e) {}

    girando(cam.area);

    fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/pdf,*/*' } })
      .then(function (resp) {
        if (atual !== estado) return;
        var tipo = (resp.headers.get('content-type') || '').toLowerCase();
        estado.nome = nomeArquivo(resp, url);
        estado.titulo.textContent = titulo(estado.nome);

        var ehPdf = tipo.indexOf('application/pdf') !== -1;
        var ehImagem = /^image\//.test(tipo);

        if (!resp.ok || (!ehPdf && !ehImagem)) {
          // Erro do servidor (JSON), página HTML, planilha… Mostra o motivo com
          // um botão — abrir daqui é toque do usuário, então o iOS deixa.
          return resp.text().then(function (txt) {
            if (atual !== estado) return;
            var msg = resp.ok ? 'Este arquivo não é PDF nem imagem.'
                              : 'Não consegui abrir este documento.';
            try { var j = JSON.parse(txt); if (j && j.erro) msg = j.erro; } catch (e) {}
            aviso(cam.area, msg, { rotulo: 'Tentar abrir mesmo assim',
                                   fn: function () { fechar(false); abrirNativo(url, '_blank'); } });
          });
        }

        return resp.blob().then(function (blob) {
          if (atual !== estado) return;
          estado.blob = blob;
          estado.blobUrl = URL.createObjectURL(blob);
          montarAcoes(estado);
          if (ehImagem) { estado.imagem = true; desenhar(); return null; }
          return blob.arrayBuffer();
        }).then(function (buf) {
          if (atual !== estado || !buf) return;
          return carregarPdfjs().then(function (lib) {
            if (atual !== estado) return;
            return lib.getDocument({ data: new Uint8Array(buf) }).promise;
          });
        }).then(function (doc) {
          if (atual !== estado || !doc) return;
          estado.doc = doc;
          desenhar();
        });
      })
      .catch(function (e) {
        if (atual !== estado) return;
        aviso(cam.area, 'Não consegui abrir o documento (' + (e && e.message ? e.message : e) + ').',
              { rotulo: 'Tentar abrir mesmo assim',
                fn: function () { fechar(false); abrirNativo(url, '_blank'); } });
      });
  }

  // Compartilhar / baixar / zoom — só aparecem quando o arquivo já chegou.
  function montarAcoes(estado) {
    var arq = null;
    try { arq = new File([estado.blob], estado.nome, { type: 'application/pdf' }); } catch (e) {}
    var podeCompartilhar = !!(arq && navigator.canShare && navigator.canShare({ files: [arq] }));

    var menos = botao('&#8722;', 'Diminuir');
    var mais = botao('+', 'Aumentar');
    menos.addEventListener('click', function () {
      estado.zoom = Math.max(0.6, Math.round((estado.zoom - 0.25) * 100) / 100); desenhar();
    });
    mais.addEventListener('click', function () {
      estado.zoom = Math.min(3, Math.round((estado.zoom + 0.25) * 100) / 100); desenhar();
    });

    if (podeCompartilhar) {
      var comp = botao('&#x1F4E4;', 'Compartilhar / salvar / imprimir');
      comp.addEventListener('click', function () {
        navigator.share({ files: [arq], title: titulo(estado.nome) }).catch(function () {});
      });
      estado.barra.appendChild(comp);
    } else {
      var baixar = document.createElement('a');
      baixar.href = estado.blobUrl;
      baixar.download = estado.nome;
      baixar.innerHTML = '&#11015;';
      baixar.title = 'Baixar';
      baixar.style.cssText = 'min-height:38px;min-width:38px;padding:0 12px;border-radius:9px;' +
        'display:inline-flex;align-items:center;justify-content:center;text-decoration:none;' +
        'background:rgba(255,255,255,.10);color:#fff;font-size:15px';
      estado.barra.appendChild(baixar);
    }
    estado.barra.appendChild(menos);
    estado.barra.appendChild(mais);
  }

  // ── ligação com o app ─────────────────────────────────────────────────────
  window.LuqPDF = { abrir: abrir, fechar: fechar, standalone: STANDALONE, nativo: abrirNativo };

  if (STANDALONE) {
    window.open = function (url, alvo, feats) {
      try {
        if (url && mesmaOrigem(url) && pareceDocumento(url) && (!alvo || alvo === '_blank')) {
          abrir(String(url));
          // Alguns códigos guardam o retorno; devolve algo inofensivo.
          return { closed: false, close: function () { fechar(false); }, focus: function () {} };
        }
      } catch (e) {}
      return abrirNativo(url, alvo, feats);
    };
  }
})();
