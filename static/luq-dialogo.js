/* ════════════════════════════════════════════════════════════════════════
   Diálogo Luqsys — v1 (08/10/2026)
   Janela de confirmação e de aviso no padrão visual dos sistemas, no lugar
   da caixa cinza do navegador (confirm/alert), que no iPhone mostra até o
   endereço do site e não diz o que vai acontecer.

   Uso:
     if (!await luqConfirmar('Excluir este imóvel?')) return;
     await luqConfirmar({titulo: 'Enviar recibo', mensagem: '…',
                         ok: 'Enviar', perigo: false});
     luqAvisar('Salvo!');            // também substitui window.alert

   - Sem título, a 1ª linha curta seguida de linha em branco vira o título.
   - Sem rótulo, o botão usa o verbo da pergunta ("Excluir este…" → Excluir).
   - Excluir/remover/apagar/encerrar/cancelar/limpar/desfazer → botão vermelho.
   - window.alert passa a abrir esta janela. Como ela não trava a página, um
     aviso dado logo antes de recarregar/navegar é reapresentado na página
     seguinte (sessionStorage), pra não sumir sem ser lido.

   Mesmo arquivo em todos os sistemas: editar aqui e replicar.
   ════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  if (window.luqConfirmar) return;

  var CSS =
    '.luqd-fundo{position:fixed;inset:0;z-index:2147483000;background:rgba(15,23,42,.55);' +
    'display:flex;align-items:center;justify-content:center;padding:16px;' +
    'opacity:0;transition:opacity .15s ease;-webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px)}' +
    '.luqd-fundo.luqd-on{opacity:1}' +
    '.luqd-caixa{background:#fff;color:#0f172a;width:100%;max-width:420px;border-radius:16px;' +
    'box-shadow:0 20px 50px rgba(0,0,0,.3);overflow:hidden;transform:translateY(8px) scale(.98);' +
    'transition:transform .15s ease;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}' +
    '.luqd-on .luqd-caixa{transform:none}' +
    '.luqd-corpo{padding:22px 22px 6px;display:flex;gap:14px;align-items:flex-start}' +
    '.luqd-icone{flex:0 0 40px;height:40px;border-radius:50%;display:flex;align-items:center;' +
    'justify-content:center;font-size:20px;background:#e0e7ff}' +
    '.luqd-info .luqd-icone{color:var(--luqd-cor,#1e3a8a);font-weight:800;font-size:21px}' +
    '.luqd-perigo .luqd-icone{background:#fee2e2}.luqd-ok .luqd-icone{background:#dcfce7}' +
    '.luqd-erro .luqd-icone{background:#fee2e2}' +
    '.luqd-texto{min-width:0;flex:1}' +
    '.luqd-titulo{font-size:17px;font-weight:700;margin:0 0 4px;line-height:1.3;overflow-wrap:break-word}' +
    '.luqd-msg{margin:0;color:#334155;white-space:pre-line;overflow-wrap:break-word;max-height:55vh;overflow:auto}' +
    '.luqd-botoes{display:flex;gap:10px;justify-content:flex-end;padding:16px 22px 20px}' +
    '.luqd-botoes button{min-height:44px;padding:0 18px;border-radius:10px;font:600 15px/1 inherit;' +
    'font-family:inherit;cursor:pointer;border:1px solid transparent}' +
    '.luqd-nao{background:#fff;color:#334155;border-color:#cbd5e1!important}' +
    '.luqd-sim{background:var(--luqd-cor,#1e3a8a);color:#fff}' +
    '.luqd-perigo .luqd-sim{background:#dc2626}' +
    '.luqd-botoes button:focus-visible{outline:3px solid #93c5fd;outline-offset:2px}' +
    '@media (max-width:600px){.luqd-fundo{align-items:flex-end;padding:0}' +
    '.luqd-caixa{max-width:none;border-radius:18px 18px 0 0;padding-bottom:env(safe-area-inset-bottom)}' +
    '.luqd-botoes{flex-direction:column-reverse}.luqd-botoes button{width:100%;min-height:48px}}' +
    '@media (prefers-color-scheme:dark){.luqd-caixa{background:#1e293b;color:#f1f5f9}' +
    '.luqd-msg{color:#cbd5e1}.luqd-nao{background:#1e293b;color:#e2e8f0;border-color:#475569!important}' +
    '.luqd-icone{background:#312e81}.luqd-perigo .luqd-icone,.luqd-erro .luqd-icone{background:#7f1d1d}' +
    '.luqd-ok .luqd-icone{background:#14532d}}';

  var PERIGO = /^(⚠️\s*)?(excluir|remover|apagar|encerrar|cancelar|limpar|desfazer|descartar|deletar|zerar|inutilizar|estornar|sair)/i;
  var ERRO = /\b(erro|falh|inválid|invalid|não foi possível|nao foi possivel|negad)/i;
  var OK = /^(✅|✔|sucesso|salvo|pronto|enviado|feito)/i;

  function injetarCss() {
    if (document.getElementById('luqd-css')) return;
    var st = document.createElement('style');
    st.id = 'luqd-css';
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  // Cor do botão: a do próprio sistema, se ele declarar uma das variáveis usuais.
  function corDoSistema() {
    try {
      var cs = getComputedStyle(document.documentElement);
      var vs = ['--primary', '--accent', '--brand', '--cor-primaria', '--azul', '--navy'];
      for (var i = 0; i < vs.length; i++) {
        var v = (cs.getPropertyValue(vs[i]) || '').trim();
        if (/^(#|rgb|hsl)/i.test(v)) return v;
      }
    } catch (e) {}
    return '';
  }

  function separar(texto) {
    var t = String(texto == null ? '' : texto).replace(/\r/g, '').trim();
    var m = t.match(/^([^\n]{1,70})\n\s*\n([\s\S]+)$/);
    if (m) return { titulo: m[1].trim(), mensagem: m[2].trim() };
    return { titulo: '', mensagem: t };
  }

  function verbo(texto) {
    var p = String(texto || '').replace(/^[^A-Za-zÀ-ú]+/, '').split(/[\s?!.,:]/)[0] || '';
    if (/^(excluir|remover|apagar|encerrar|cancelar|limpar|desfazer|enviar|mandar|marcar|confirmar|gerar|recarregar|refazer|voltar|retirar|sair|aplicar|salvar|substituir|inserir|abrir|reenviar|baixar|finalizar|aprovar|cobrar|emitir|importar|descartar|zerar|estornar|duplicar|arquivar|liberar|bloquear|ativar|desativar|trocar|continuar|reescrever)$/i.test(p))
      return p.charAt(0).toUpperCase() + p.slice(1).toLowerCase();
    return '';
  }

  var fila = Promise.resolve();

  function abrir(cfg) {
    // Um diálogo de cada vez — dois confirm seguidos não se sobrepõem.
    var p = fila.then(function () { return mostrar(cfg); });
    fila = p.catch(function () {});
    return p;
  }

  function mostrar(cfg) {
    return new Promise(function (resolve) {
      injetarCss();
      var fundo = document.createElement('div');
      fundo.className = 'luqd-fundo luqd-' + cfg.tipo;
      fundo.setAttribute('role', cfg.confirmar ? 'alertdialog' : 'dialog');
      fundo.setAttribute('aria-modal', 'true');
      var cor = corDoSistema();
      if (cor) fundo.style.setProperty('--luqd-cor', cor);

      var icone = { perigo: '⚠️', erro: '⚠️', ok: '✅', info: cfg.confirmar ? '?' : 'i' }[cfg.tipo] || 'i';
      var caixa = document.createElement('div');
      caixa.className = 'luqd-caixa';
      var corpo = document.createElement('div');
      corpo.className = 'luqd-corpo';
      var ic = document.createElement('div');
      ic.className = 'luqd-icone';
      ic.textContent = icone;
      var tx = document.createElement('div');
      tx.className = 'luqd-texto';
      if (cfg.titulo) {
        var h = document.createElement('p');
        h.className = 'luqd-titulo';
        h.textContent = cfg.titulo;
        tx.appendChild(h);
      }
      if (cfg.mensagem) {
        var ms = document.createElement('p');
        ms.className = 'luqd-msg';
        ms.textContent = cfg.mensagem;
        tx.appendChild(ms);
      }
      corpo.appendChild(ic);
      corpo.appendChild(tx);

      var botoes = document.createElement('div');
      botoes.className = 'luqd-botoes';
      var nao = null;
      if (cfg.confirmar) {
        nao = document.createElement('button');
        nao.type = 'button';
        nao.className = 'luqd-nao';
        nao.textContent = cfg.cancelar || 'Cancelar';
        botoes.appendChild(nao);
      }
      var sim = document.createElement('button');
      sim.type = 'button';
      sim.className = 'luqd-sim';
      sim.textContent = cfg.ok || (cfg.confirmar ? 'Confirmar' : 'OK');
      botoes.appendChild(sim);

      caixa.appendChild(corpo);
      caixa.appendChild(botoes);
      fundo.appendChild(caixa);

      var foco = document.activeElement;
      var fechado = false;
      function fechar(valor) {
        if (fechado) return;
        fechado = true;
        document.removeEventListener('keydown', tecla, true);
        fundo.classList.remove('luqd-on');
        setTimeout(function () { if (fundo.parentNode) fundo.parentNode.removeChild(fundo); }, 160);
        try { if (foco && foco.focus) foco.focus(); } catch (e) {}
        if (cfg.aoFechar) cfg.aoFechar();
        resolve(valor);
      }
      function tecla(e) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); fechar(cfg.confirmar ? false : undefined); }
        else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); fechar(cfg.confirmar ? true : undefined); }
        else if (e.key === 'Tab' && nao) {
          e.preventDefault();
          (document.activeElement === sim ? nao : sim).focus();
        }
      }
      sim.addEventListener('click', function () { fechar(cfg.confirmar ? true : undefined); });
      if (nao) nao.addEventListener('click', function () { fechar(false); });
      fundo.addEventListener('click', function (e) {
        if (e.target === fundo) fechar(cfg.confirmar ? false : undefined);
      });
      document.addEventListener('keydown', tecla, true);

      (document.body || document.documentElement).appendChild(fundo);
      requestAnimationFrame(function () {
        fundo.classList.add('luqd-on');
        // Perigo: o foco fica no Cancelar, pra Enter distraído não apagar nada.
        try { ((cfg.tipo === 'perigo' && nao) ? nao : sim).focus({ preventScroll: true }); } catch (e) {}
      });
    });
  }

  function normalizar(arg, confirmar) {
    var cfg = (arg && typeof arg === 'object') ? Object.assign({}, arg) : separar(arg);
    if (!cfg.titulo && !cfg.mensagem) cfg.mensagem = '';
    // Sem título separado, a própria pergunta vira o título (fica em negrito).
    if (!cfg.titulo && cfg.mensagem && cfg.mensagem.length <= 90 && cfg.mensagem.indexOf('\n') < 0) {
      cfg.titulo = cfg.mensagem;
      cfg.mensagem = '';
    }
    var base = (cfg.titulo || '') + ' ' + (cfg.mensagem || '');
    if (cfg.titulo) cfg.titulo = cfg.titulo.replace(/^⚠️\s*/, '');
    cfg.confirmar = confirmar;
    if (!cfg.tipo) {
      if (confirmar) cfg.tipo = (cfg.perigo || (cfg.perigo !== false && PERIGO.test(base.trim()))) ? 'perigo' : 'info';
      else cfg.tipo = ERRO.test(base) ? 'erro' : (OK.test(base.trim()) ? 'ok' : 'info');
    }
    if (confirmar && !cfg.ok) cfg.ok = verbo(cfg.titulo || cfg.mensagem) || 'Confirmar';
    return cfg;
  }

  window.luqConfirmar = function (arg) { return abrir(normalizar(arg, true)); };

  // ── Aviso (alert) ────────────────────────────────────────────────────────
  var CHAVE = 'luqd-aviso-pendente';
  window.luqAvisar = function (arg) {
    var cfg = normalizar(arg, false);
    try { sessionStorage.setItem(CHAVE, JSON.stringify({ c: cfg, t: Date.now() })); } catch (e) {}
    cfg.aoFechar = function () { try { sessionStorage.removeItem(CHAVE); } catch (e) {} };
    return abrir(cfg);
  };
  var alertaNativo = window.alert;
  window.alert = function (msg) {
    try { window.luqAvisar(msg); } catch (e) { alertaNativo.call(window, msg); }
  };

  // Aviso que não chegou a ser fechado porque a página recarregou/navegou logo
  // depois: mostra de novo aqui (só se for recente).
  function reapresentar() {
    var s = null;
    try { s = JSON.parse(sessionStorage.getItem(CHAVE) || 'null'); } catch (e) {}
    if (!s || !s.c || Date.now() - (s.t || 0) > 15000) {
      try { sessionStorage.removeItem(CHAVE); } catch (e) {}
      return;
    }
    var cfg = s.c;
    cfg.aoFechar = function () { try { sessionStorage.removeItem(CHAVE); } catch (e) {} };
    abrir(cfg);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', reapresentar);
  else reapresentar();
})();
