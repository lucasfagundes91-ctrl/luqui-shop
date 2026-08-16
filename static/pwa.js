/* Barra "Instalar app" — aparece quando a página é aberta a partir do
   /instalar (com ?instalar=1).

   No Android o navegador só oferece a instalação na própria página do app
   (o manifest é o daquela página), então o /instalar manda pra cá e aqui a
   gente dispara o convite. No iPhone não existe API de instalar: mostra o
   caminho do menu Compartilhar.

   Copiado em cada repo, junto do mobile-kit. */
(function () {
  'use strict';

  var jaInstalado = window.matchMedia('(display-mode: standalone)').matches ||
                    window.navigator.standalone === true;
  if (jaInstalado) return;

  var pedido = null;   // beforeinstallprompt guardado
  var barra = null;

  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    pedido = e;
    if (querInstalar()) mostrar();
  });

  window.addEventListener('appinstalled', function () { fechar(); });

  function querInstalar() {
    return /[?&]instalar=1/.test(location.search) ||
           location.hash === '#instalar';
  }

  function ios() {
    var ua = navigator.userAgent || '';
    return /iPad|iPhone|iPod/.test(ua) ||
           (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  function nomeDoApp() {
    var m = document.querySelector('meta[name="apple-mobile-web-app-title"]');
    if (m && m.content) return m.content;
    return (document.title || 'este app').split(/[—–|-]/)[0].trim();
  }

  function fechar() {
    if (barra) { barra.remove(); barra = null; }
  }

  function mostrar() {
    if (barra) return;
    var cor = (document.querySelector('meta[name="theme-color"]') || {}).content
              || '#0f172a';

    barra = document.createElement('div');
    barra.setAttribute('role', 'dialog');
    barra.style.cssText = [
      'position:fixed', 'left:12px', 'right:12px',
      'bottom:calc(12px + env(safe-area-inset-bottom))', 'z-index:2147483000',
      'background:#fff', 'color:#0f172a', 'border-radius:14px',
      'box-shadow:0 8px 30px rgba(15,23,42,.28)', 'padding:14px 16px',
      'font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif',
      'max-width:520px', 'margin:0 auto'
    ].join(';');

    var titulo = document.createElement('div');
    titulo.style.cssText = 'font-weight:700;font-size:15px;margin-bottom:4px';
    titulo.textContent = 'Instalar ' + nomeDoApp();

    var texto = document.createElement('div');
    texto.style.cssText = 'color:#475569;margin-bottom:12px';

    var acoes = document.createElement('div');
    acoes.style.cssText = 'display:flex;gap:8px';

    var fecharBtn = document.createElement('button');
    fecharBtn.textContent = 'Agora não';
    fecharBtn.style.cssText = 'flex:1;min-height:42px;border:none;border-radius:9px;' +
      'background:#e2e8f0;color:#0f172a;font-size:15px;font-weight:600';
    fecharBtn.addEventListener('click', fechar);

    if (pedido) {
      texto.textContent = 'Fica com ícone próprio na tela de início e abre sem a barra do navegador.';
      var instalar = document.createElement('button');
      instalar.textContent = 'Instalar';
      instalar.style.cssText = 'flex:1;min-height:42px;border:none;border-radius:9px;' +
        'background:' + cor + ';color:#fff;font-size:15px;font-weight:600';
      instalar.addEventListener('click', function () {
        var p = pedido; pedido = null; fechar();
        p.prompt();
      });
      acoes.appendChild(fecharBtn);
      acoes.appendChild(instalar);
    } else if (ios()) {
      texto.innerHTML = 'Toque em <b>Compartilhar</b> ' +
        '<span style="display:inline-block;transform:translateY(2px)">&#x2934;</span>' +
        ' na barra do Safari e escolha <b>Adicionar à Tela de Início</b>.';
      fecharBtn.textContent = 'Entendi';
      fecharBtn.style.background = cor;
      fecharBtn.style.color = '#fff';
      acoes.appendChild(fecharBtn);
    } else {
      return;  // navegador sem suporte: não enche o saco com barra inútil
    }

    barra.appendChild(titulo);
    barra.appendChild(texto);
    barra.appendChild(acoes);
    document.body.appendChild(barra);
  }

  function iniciar() {
    if (!querInstalar()) return;
    // O beforeinstallprompt pode demorar; no iPhone ele nunca vem.
    setTimeout(mostrar, pedido ? 0 : 1200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', iniciar);
  } else {
    iniciar();
  }
})();
