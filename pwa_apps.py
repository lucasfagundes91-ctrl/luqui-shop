# -*- coding: utf-8 -*-
"""Instalação dos apps do sistema na tela de início (iPhone e Android).

Cada sistema Luqsys vira mais de um app no celular: o sistema inteiro, o
/resumo (painel de acompanhamento) e o /conferir (fila de aprovação). Cada um
precisa do SEU manifest — o navegador identifica o app instalado pelo `id` do
manifest, então um manifest só instalaria um app só.

O que este módulo publica:
  /manifest/<slug>.json   — um manifest por app
  /instalar               — página pública com os apps e o botão de instalar
  /atalhos.mobileconfig   — perfil iOS que instala todos os atalhos de uma vez

Cópia igual em cada repo (mesmo esquema do mobile-kit): o que muda é só a
chamada de `registrar_pwa()` no app.py. Editar aqui e replicar.
"""
import base64
import os
import uuid
from xml.sax.saxutils import escape

from flask import (jsonify, render_template, request, Response)

# Namespace fixo: o UUID de cada payload precisa ser SEMPRE o mesmo, senão o
# iOS trata o perfil reinstalado como um perfil novo e duplica os atalhos.
_NS = uuid.UUID('6f1c0f8e-1d3a-4f2b-9a6d-2b5e7c9a4d10')

_ICONE_CACHE = {}


def _base_url():
    """URL pública do sistema, sempre https fora do localhost.

    O proxy do Railway entrega http pro Flask; sem forçar o esquema o perfil
    do iOS sairia com URL http e o iPhone abriria o atalho sem TLS."""
    raiz = (os.environ.get('PUBLIC_BASE_URL') or request.host_url).rstrip('/')
    if raiz.startswith('http://') and not (
            'localhost' in raiz or '127.0.0.1' in raiz):
        raiz = 'https://' + raiz[len('http://'):]
    return raiz


def _icone_b64(pasta_brand, nome, tamanho=180):
    """PNG do ícone em base64 pro perfil do iOS (o perfil embute a imagem)."""
    chave = (nome, tamanho)
    if chave not in _ICONE_CACHE:
        caminho = os.path.join(pasta_brand, '%s-%d.png' % (nome, tamanho))
        if not os.path.exists(caminho):  # tamanho ausente: cai no 192
            caminho = os.path.join(pasta_brand, '%s-192.png' % nome)
        with open(caminho, 'rb') as fh:
            _ICONE_CACHE[chave] = base64.b64encode(fh.read()).decode('ascii')
    return _ICONE_CACHE[chave]


def _instalar_intercepta(app, rotas):
    """Responde as rotas de instalação ANTES das guardas do sistema.

    Cada app tem sua própria trava de before_request (trial vencido,
    assinatura, permissão) e elas têm nomes e formatos diferentes em cada
    repo. Em vez de editar 16 listas de exceção — e voltar nelas a cada guarda
    nova —, o módulo intercepta os três caminhos aqui: `before_request` roda na
    ordem de registro, então basta chamar `registrar_pwa()` logo depois de
    criar o Flask que isto passa na frente.

    Vale a pena ser público: o manifest o navegador busca SEM cookie (a menos
    que a tag tenha crossorigin), e um /instalar atrás de login não serve pra
    mandar pro cliente. Nada aqui expõe dado — é nome, cor e ícone."""
    @app.before_request
    def _pwa_antes_das_guardas():
        p = request.path or ''
        fn = rotas.get(p)
        if fn:
            return fn()
        if p.startswith('/manifest/') and p.endswith('.json'):
            return rotas['__manifest__'](p[len('/manifest/'):-len('.json')])
        return None


def registrar_pwa(app, *, sistema, slug_sistema, cor, cor_fundo, apps,
                  pasta_brand=None):
    """Liga as rotas de instalação neste app Flask.

    sistema      — nome de marca ("FarmPro")
    slug_sistema — usado no identificador do perfil iOS ("farmpro")
    cor          — cor do tema (barra de status, botões)
    cor_fundo    — fundo da splash do Android
    apps         — lista de dicts:
        slug     identificador curto ('app', 'resumo', 'conferir')
        nome     nome completo do app ("FarmPro Resumo")
        rotulo   rótulo curto embaixo do ícone ("Resumo") — o iOS corta ~12
        url      caminho ('/', '/resumo', '/conferir')
        icone    nome-base do PNG em static/brand (sem o -192/-512)
        desc     uma linha explicando pra que serve
        cheio    True = abre sem a barra do Safari (padrão: True fora do '/')
    """
    pasta_brand = pasta_brand or os.path.join(app.root_path, 'static', 'brand')
    por_slug = {a['slug']: a for a in apps}

    def _manifest_dict(a):
        icone = a['icone']
        return {
            # `id` é a identidade do app instalado — mudar a URL do manifest
            # depois não cria um app duplicado enquanto o id não mudar.
            'id': a['url'],
            'name': a['nome'],
            'short_name': a.get('rotulo') or a['nome'],
            'description': a.get('desc', ''),
            'start_url': a['url'],
            'scope': '/',
            'display': 'standalone',
            'orientation': 'portrait',
            'background_color': cor_fundo,
            'theme_color': cor,
            'lang': 'pt-BR',
            'icons': [
                {'src': '/static/brand/%s-192.png' % icone, 'sizes': '192x192',
                 'type': 'image/png', 'purpose': 'any'},
                {'src': '/static/brand/%s-512.png' % icone, 'sizes': '512x512',
                 'type': 'image/png', 'purpose': 'any'},
                {'src': '/static/brand/%s-512.png' % icone, 'sizes': '512x512',
                 'type': 'image/png', 'purpose': 'maskable'},
            ],
        }

    @app.route('/manifest/<slug>.json')
    def pwa_manifest(slug):
        a = por_slug.get(slug)
        if not a:
            return jsonify({'erro': 'app desconhecido'}), 404
        resp = jsonify(_manifest_dict(a))
        resp.headers['Content-Type'] = 'application/manifest+json'
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp

    @app.route('/instalar')
    def pwa_instalar():
        return render_template(
            'instalar.html', sistema=sistema, cor=cor, cor_fundo=cor_fundo,
            apps=apps, base_url=_base_url())

    @app.route('/atalhos.mobileconfig')
    @app.route('/atalhos')
    def pwa_mobileconfig():
        """Perfil de configuração do iOS: instala todos os atalhos de uma vez.

        O Content-Type é o que faz o iPhone abrir isso como perfil; com
        text/plain ele mostra o XML na tela."""
        raiz = _base_url()
        partes = []
        for a in apps:
            cheio = a.get('cheio', a['url'] != '/')
            ident = 'br.com.luqsys.%s.webclip.%s' % (slug_sistema, a['slug'])
            partes.append("""	<dict>
		<key>PayloadType</key><string>com.apple.webClip.managed</string>
		<key>PayloadVersion</key><integer>1</integer>
		<key>PayloadIdentifier</key><string>%s</string>
		<key>PayloadUUID</key><string>%s</string>
		<key>PayloadDisplayName</key><string>%s</string>
		<key>Label</key><string>%s</string>
		<key>URL</key><string>%s</string>
		<key>Icon</key><data>%s</data>
		<key>IsRemovable</key><true/>
		<key>Precomposed</key><true/>
		<key>FullScreen</key><%s/>
		<key>IgnoreManifestScope</key><true/>
	</dict>""" % (
                ident, uuid.uuid5(_NS, ident), escape(a['nome']),
                escape(a.get('rotulo') or a['nome']), escape(raiz + a['url']),
                _icone_b64(pasta_brand, a['icone']),
                'true' if cheio else 'false'))

        ident_perfil = 'br.com.luqsys.%s.atalhos' % slug_sistema
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>PayloadContent</key>
	<array>
%s
	</array>
	<key>PayloadDisplayName</key><string>Atalhos %s</string>
	<key>PayloadDescription</key><string>Instala os atalhos do %s na tela de inicio do iPhone.</string>
	<key>PayloadIdentifier</key><string>%s</string>
	<key>PayloadUUID</key><string>%s</string>
	<key>PayloadOrganization</key><string>Luqsys</string>
	<key>PayloadType</key><string>Configuration</string>
	<key>PayloadVersion</key><integer>1</integer>
	<key>PayloadRemovalDisallowed</key><false/>
</dict>
</plist>
""" % ('\n'.join(partes), escape(sistema), escape(sistema),
       ident_perfil, uuid.uuid5(_NS, ident_perfil))

        resp = Response(xml, mimetype='application/x-apple-aspen-config')
        resp.headers['Content-Disposition'] = (
            'attachment; filename="atalhos-%s.mobileconfig"' % slug_sistema)
        return resp

    _instalar_intercepta(app, {
        '/instalar': pwa_instalar,
        '/atalhos': pwa_mobileconfig,
        '/atalhos.mobileconfig': pwa_mobileconfig,
        '__manifest__': pwa_manifest,
    })
    return app
