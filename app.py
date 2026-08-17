"""LuquiShop — Loja online + Clube Caixa Misteriosa da Luqui Brinquedos.

Stack Flask+PG. Produtos/estoque/promoções são puxados do PDV Pro em tempo real
via API (X-API-Key). Quando um pedido é pago, dispara webhook que cria a venda
no PDV Pro automaticamente.
"""
# ── Fuso horário da aplicação ────────────────────────────────────────────────
# O container roda em UTC. Sem isto, date.today() e datetime.now() devolvem a
# data e a hora de Londres: das 21h à meia-noite em Brasília o UTC já virou o
# dia seguinte, e todo "hoje" do sistema apontava pro dia errado.
# Precisa vir ANTES de qualquer import que leia a hora.
import os as _os_tz, time as _time_tz
_os_tz.environ.setdefault('TZ', 'America/Sao_Paulo')
try:
    _time_tz.tzset()
except AttributeError:            # Windows não tem tzset
    pass
if _time_tz.tzname[0] in ('UTC', 'GMT'):
    # Imagem sem tzdata: o TZ acima não pega e as datas voltam a sair em UTC.
    # Falhar em silêncio aqui viraria relatório torto semanas depois.
    print('AVISO: fuso nao aplicado (falta tzdata na imagem) - datas em UTC',
          flush=True)
# ─────────────────────────────────────────────────────────────────────────────

import base64
import difflib
import hashlib
import io
import json
import logging
import math
import os
import secrets
import time
import unicodedata
from datetime import date as _date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import re

import psycopg2
import psycopg2.extras
import requests
from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('luquishop')
SP_TZ = ZoneInfo('America/Sao_Paulo')

app = Flask(__name__)

def _cron_token_ok():
    """Confere o ?token= das rotas de cron. Fail-closed.

    Antes era os.environ.get('CRON_TOKEN', 'troque') repetido em 6 rotas: sem a
    env, o token virava a string publica 'troque' e qualquer um disparava
    disparo de WhatsApp/e-mail em massa. Nao ha default — sem CRON_TOKEN no
    ambiente, nenhuma passa."""
    esperado = os.environ.get('CRON_TOKEN', '')
    recebido = request.args.get('token', '') or request.headers.get('X-Cron-Token', '')
    return bool(esperado and secrets.compare_digest(recebido, esperado))


app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
# Cookie de sessão: HttpOnly tira do alcance de JS (XSS não rouba a sessão do
# admin), SameSite=Lax impede que outro site dispare POST autenticado no lugar
# do Lucas, Secure só sai em HTTPS. Sem Secure, um único acesso em http:// vaza
# o cookie em texto claro.
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('RAILWAY_ENVIRONMENT'))

DATABASE_URL = os.environ.get('DATABASE_URL') or ''

# Status de pedido que JA FOI PAGO — tudo que vem depois de 'aguardando_pagto'
# na STATUS_TIMELINE. Existia como lista solta em cada consulta e as copias
# esqueciam 'preparando' e 'pronto_retirada', entao no instante em que o PDV
# Pro aceitava o pedido (status vira 'preparando') o cliente passava a
# aparecer com 0 pedidos e R$ 0,00 gasto no painel, o e-mail de avaliacao
# nunca saia e ele ainda podia receber "voce esqueceu o carrinho".
STATUS_PAGOS = ('pago', 'preparando', 'pronto_retirada', 'enviado', 'entregue')
_SQL_PAGOS = "('" + "','".join(STATUS_PAGOS) + "')"

PDVPRO_URL = os.environ.get('PDVPRO_URL', 'https://pdvpro.luqsys.com.br')
PDVPRO_API_KEY = os.environ.get('PDVPRO_API_KEY', '')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'lucasfagundes91@hotmail.com')
ADMIN_SENHA_PADRAO = os.environ.get('ADMIN_SENHA') or secrets.token_urlsafe(16)
WHATSAPP_LOJA = os.environ.get('WHATSAPP_LOJA', '5545991119800')
ASAAS_API_KEY = os.environ.get('ASAAS_API_KEY', '')
ASAAS_WEBHOOK_TOKEN = os.environ.get('ASAAS_WEBHOOK_TOKEN', '')
ASAAS_BASE = 'https://api.asaas.com/v3'
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
EMAIL_FROM = os.environ.get('EMAIL_FROM',
                            'Luqui Brinquedos <contato@luquibrinquedos.com.br>')
# O Resend só entrega de domínio verificado na conta. Enquanto o plano
# comportar 1 domínio só (ocupado por luqsys.com.br), o EMAIL_FROM aponta pra
# lá e o Reply-To traz a resposta do cliente pro endereço real da loja.
EMAIL_REPLY_TO = os.environ.get('EMAIL_REPLY_TO',
                                'contato@luquibrinquedos.com.br')
SITE_URL = os.environ.get('SITE_URL', 'https://www.luquibrinquedos.com.br')
ZAPI_INSTANCE = os.environ.get('ZAPI_INSTANCE', '')
ZAPI_TOKEN = os.environ.get('ZAPI_TOKEN', '')
ZAPI_CLIENT_TOKEN = os.environ.get('ZAPI_CLIENT_TOKEN', '')
ADMIN_WHATSAPP = os.environ.get('ADMIN_WHATSAPP', '5545991119800')
META_PIXEL_ID = os.environ.get('META_PIXEL_ID', '')
GOOGLE_TAG_ID = os.environ.get('GOOGLE_TAG_ID', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

CLUBE_LUQUI_ATIVO = os.environ.get('CLUBE_LUQUI_ATIVO', '0') == '1'
CUPOM_ANIVERSARIO_ATIVO = os.environ.get('CUPOM_ANIVERSARIO_ATIVO', '0') == '1'
CUPOM_PRIMEIRA_COMPRA_ATIVO = os.environ.get('CUPOM_PRIMEIRA_COMPRA_ATIVO', '0') == '1'


@app.context_processor
def _ctx_flags():
    return {'clube_ativo': CLUBE_LUQUI_ATIVO,
            'cupom_aniversario_ativo': CUPOM_ANIVERSARIO_ATIVO,
            'cupom_primeira_compra_ativo': CUPOM_PRIMEIRA_COMPRA_ATIVO,
            'SITE_URL': SITE_URL}


SITE_HOST = SITE_URL.split('//', 1)[-1].strip('/')     # www.luquibrinquedos.com.br


# ===================== Apps na tela de início (PWA) =====================
# Manifest, /instalar e perfil de atalhos do iPhone — ver pwa_apps.py.
# Antes das rotas: o módulo intercepta a instalação no before_request,
# que roda na ordem de registro.
from pwa_apps import registrar_pwa

registrar_pwa(
    app,
    sistema='Luqui Brinquedos',
    slug_sistema='luqui',
    cor='#f5b20a',
    cor_fundo='#f5b20a',
    apps=[
        {'slug': 'app', 'nome': 'Luqui Brinquedos', 'rotulo': 'Luqui', 'url': '/',
         'icone': 'luqui-icon', 'cheio': False,
         'desc': 'A loja: catálogo, carrinho e acompanhamento do pedido.'},
        {'slug': 'admin', 'nome': 'Luqui Gestão', 'rotulo': 'Luqui Gestão',
         'url': '/admin', 'icone': 'luqui-admin-icon', 'cheio': False,
         'desc': 'Pedidos, produtos, clientes e campanhas da loja.'},
    ],
)


@app.before_request
def _forca_host_canonico():
    """301 do apex pro www.

    O dominio nu e o www respondiam 200 os DOIS, sem redirect: pro Google eram
    dois sites com o mesmo conteudo. Pior, o buscador tinha indexado o apex
    enquanto sitemap, feed.xml, og:url e JSON-LD apontavam pro www — link
    externo caia num, sinal de ranking ia pro outro. So GET/HEAD: redirecionar
    POST perderia o corpo do webhook.
    """
    if request.method not in ('GET', 'HEAD'):
        return None
    host = (request.host or '').lower().split(':')[0]
    if not host or host == SITE_HOST or host.endswith('.railway.app'):
        return None
    if host != SITE_HOST.replace('www.', ''):
        return None                       # dominio desconhecido: nao mexe
    destino = SITE_URL + request.full_path.rstrip('?')
    return redirect(destino, code=301)


@app.before_request
def _bloqueia_clube_se_desligado():
    if CLUBE_LUQUI_ATIVO:
        return None
    p = request.path or ''
    if p == '/clube' or p.startswith('/clube/') or p.startswith('/api/clube'):
        if p.startswith('/api/'):
            return jsonify({'erro': 'Clube Luqui temporariamente indisponível'}), 404
        abort(404)
    return None


def get_conn():
    """Conexão Postgres por request (autocommit)."""
    if 'conn' not in g:
        g.conn = psycopg2.connect(DATABASE_URL)
        g.conn.autocommit = True
    return g.conn


@app.teardown_appcontext
def _close_conn(_exc):
    c = g.pop('conn', None)
    if c is not None:
        c.close()


def db_execute(sql, params=None, fetch=None):
    cur = get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(sql, params or [])
        if fetch == 'one':
            return cur.fetchone()
        if fetch == 'all':
            return cur.fetchall()
        return None
    finally:
        cur.close()


def _rl_ip():
    """IP do CLIENTE, pra rate limit por visitante.

    Pegava `xff.split(',')[-1]` — a ULTIMA entrada do X-Forwarded-For, que por
    definicao e o proxy mais proximo do app, nunca o cliente. Resultado: todo
    mundo caia no mesmo balde (a tabela tinha 2 IPs em 30 dias, os dois de
    borda, contra ~58 mil visitantes distintos), entao o limite de CPF e o de
    login eram GLOBAIS em vez de por pessoa — um visitante sozinho podia
    trancar a consulta de CPF do site inteiro.

    CF-Connecting-IP vem do Cloudflare e nao e falsificavel de fora (o edge
    reescreve). O primeiro XFF e o fallback; ele ACEITA spoof, entao serve pra
    espalhar carga legitima, nao como controle de seguranca forte.
    """
    cf = (request.headers.get('CF-Connecting-IP') or '').strip()
    if cf:
        return cf
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'


def rate_limit_ok(bucket, chave, max_hits, janela_seg):
    """False se estourou o limite na janela. Falha aberto se o banco oscilar."""
    try:
        row = db_execute(
            "SELECT COUNT(*) AS n FROM rate_limit_hits "
            "WHERE bucket=%s AND chave=%s "
            "AND criado_em > NOW() - make_interval(secs => %s)",
            [bucket, chave, janela_seg], fetch='one')
        if (row or {}).get('n', 0) >= max_hits:
            return False
        db_execute("INSERT INTO rate_limit_hits (bucket, chave) VALUES (%s,%s)",
                   [bucket, chave])
        db_execute("DELETE FROM rate_limit_hits WHERE criado_em < NOW() - interval '1 day'")
        return True
    except Exception:
        return True


# ─── Tracker de visitas do site (analytics) ───────────────────────────────────
_BOT_HINTS = ('bot', 'crawler', 'spider', 'curl', 'wget', 'headless',
              'python-', 'go-http', 'http-client', 'facebookexternal',
              'whatsapp', 'preview', 'fetch')
_VISITA_SKIP_PREFIXES = ('/static/', '/admin', '/api/', '/webhook', '/auth/',
                         '/cron/', '/_', '/sw.js', '/robots.txt',
                         '/favicon', '/sitemap', '/health')


def _ip_hash_atual():
    """Hash do IP do visitante (anonimizado, LGPD)."""
    ip = (request.headers.get('CF-Connecting-IP')
          or (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
          or request.remote_addr or '')
    return hashlib.sha256(
        (ip + (app.secret_key or 'salt')).encode('utf-8')
    ).hexdigest()[:40]


def _normalizar_termo(termo):
    """minúsculas, sem acento e sem pontuação — pra agrupar 'BONECA!' com
    'boneca' e 'bonéca' no ranking de buscas."""
    t = unicodedata.normalize('NFKD', (termo or '').lower())
    t = ''.join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r'[^a-z0-9 ]+', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()[:120]


def log_busca(termo, resultados=0, origem='site'):
    """Registra o que a pessoa procurou. Nunca quebra a request."""
    try:
        termo = (termo or '').strip()[:120]
        norm = _normalizar_termo(termo)
        if not norm or len(norm) < 2:
            return
        ua_low = (request.headers.get('User-Agent') or '').lower()
        if any(b in ua_low for b in _BOT_HINTS):
            return
        db_execute(
            """INSERT INTO site_buscas
               (termo, termo_norm, origem, resultados, ip_hash, cliente_id)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            [termo, norm, origem, int(resultados or 0),
             _ip_hash_atual(), session.get('cliente_id')])
    except Exception:
        pass


@app.before_request
def _track_visita():
    """Grava pageviews em site_visitas. Silencioso em erro."""
    try:
        if request.method != 'GET':
            return
        p = request.path or '/'
        for pref in _VISITA_SKIP_PREFIXES:
            if p == pref or p.startswith(pref):
                return
        ua = (request.headers.get('User-Agent') or '')[:300]
        ref = (request.headers.get('Referer') or '')[:500]
        ip_h = _ip_hash_atual()
        ua_low = ua.lower()
        is_bot = any(b in ua_low for b in _BOT_HINTS)
        cid = session.get('cliente_id')

        # Bot não vira linha crua. Em julho/2026 o crawler passou de 20 mil pra
        # 130 mil hits/dia e site_visitas chegou a 910 MB — 75% lixo de robô,
        # que nenhuma consulta de analytics usa (todas filtram NOT is_bot).
        # Guardamos só a contagem diária, que é o único sinal que interessa.
        # Scraper que se passa por navegador: em 30/07/2026 eram 104.069 hits em
        # /categoria/ vindos de 95.539 IPs distintos num dia — uma pagina por
        # "visitante", numa loja com 112 pedidos no total. O User-Agent e de
        # navegador real, entao _BOT_HINTS nao pega.
        #
        # Assinatura: pagina de categoria + sem referer + sem cookie de sessao.
        # Os dois sinais sao confiaveis aqui — a politica de referer do site e a
        # padrao (navegacao interna manda referer) e toda resposta ja crava
        # cookie de sessao, entao quem volta sempre traz um. No mesmo dia,
        # /categoria/ COM referer deu 91 hits: a proporcao e de mil pra um.
        #
        # Paliativo ate a regra no edge (Cloudflare) — aqui a CPU e a banda ja
        # foram gastas; so evita poluir o analytics e inchar a tabela.
        sem_rastro = (not ref and not request.cookies.get('session')
                      and p.startswith('/categoria/'))

        if is_bot or sem_rastro:
            db_execute(
                """INSERT INTO site_visitas_bots_diario (dia, visitas)
                   VALUES (CURRENT_DATE, 1)
                   ON CONFLICT (dia) DO UPDATE
                   SET visitas = site_visitas_bots_diario.visitas + 1""")
            return

        db_execute(
            """INSERT INTO site_visitas
               (path, referer, user_agent, ip_hash, is_bot, cliente_id)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            [p[:500], ref, ua, ip_h, is_bot, cid])
    except Exception:
        # nunca quebra request por erro de tracking
        pass


# ─── Inicialização do banco ───────────────────────────────────────────────────
def init_db():
    """Cria schema mínimo. Cada CREATE/ALTER é independente pra schema-drift
    não matar boots seguintes."""
    ddls = [
        # Clientes do site (cadastro com email/senha)
        """CREATE TABLE IF NOT EXISTS clientes_site (
            id SERIAL PRIMARY KEY,
            nome VARCHAR(160) NOT NULL,
            email VARCHAR(160) UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            cpf VARCHAR(14),
            telefone VARCHAR(20),
            cep VARCHAR(9),
            endereco VARCHAR(200),
            numero VARCHAR(20),
            complemento VARCHAR(80),
            bairro VARCHAR(80),
            cidade VARCHAR(80),
            uf VARCHAR(2),
            criado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        # OAuth Google: cadastro sem senha (sub = unique id do Google)
        "ALTER TABLE clientes_site ALTER COLUMN senha_hash DROP NOT NULL",
        "ALTER TABLE clientes_site ADD COLUMN IF NOT EXISTS google_sub VARCHAR(40)",
        "ALTER TABLE clientes_site ADD COLUMN IF NOT EXISTS foto_url TEXT",
        # E-mail comprovadamente do cliente: veio do Google ou ele clicou no
        # link magico que so chegou naquela caixa. Sem isso nao da pra casar
        # pedido de visitante so por e-mail sem risco de entregar CPF e
        # endereco de um cliente pra outro.
        "ALTER TABLE clientes_site ADD COLUMN IF NOT EXISTS email_verificado BOOLEAN DEFAULT FALSE",
        "ALTER TABLE clientes_site ADD COLUMN IF NOT EXISTS email_verificado_em TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS idx_clientes_site_google ON clientes_site(google_sub)",
        # Pedidos da loja
        """CREATE TABLE IF NOT EXISTS pedidos (
            id SERIAL PRIMARY KEY,
            cliente_id INT REFERENCES clientes_site(id),
            email VARCHAR(160),
            nome VARCHAR(160),
            telefone VARCHAR(20),
            cpf VARCHAR(14),
            cep VARCHAR(9),
            endereco VARCHAR(200),
            numero VARCHAR(20),
            complemento VARCHAR(80),
            bairro VARCHAR(80),
            cidade VARCHAR(80),
            uf VARCHAR(2),
            subtotal NUMERIC(12,2) DEFAULT 0,
            frete NUMERIC(12,2) DEFAULT 0,
            desconto NUMERIC(12,2) DEFAULT 0,
            total NUMERIC(12,2) DEFAULT 0,
            forma_pagto VARCHAR(20),
            parcelas INT DEFAULT 1,
            frete_servico VARCHAR(40),
            frete_prazo VARCHAR(40),
            status VARCHAR(20) DEFAULT 'aguardando_pagto',
            asaas_cobranca_id VARCHAR(60),
            asaas_link TEXT,
            asaas_pix_qrcode TEXT,
            asaas_boleto_url TEXT,
            pago_em TIMESTAMPTZ,
            melhorenvio_etiqueta_id VARCHAR(60),
            melhorenvio_rastreio VARCHAR(60),
            pdv_venda_id INT,
            observacao TEXT,
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            atualizado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS pedido_itens (
            id SERIAL PRIMARY KEY,
            pedido_id INT REFERENCES pedidos(id) ON DELETE CASCADE,
            produto_pdv_id INT,
            codigo_barras VARCHAR(40),
            descricao VARCHAR(200),
            preco_unitario NUMERIC(12,2),
            quantidade NUMERIC(10,3),
            subtotal NUMERIC(12,2),
            foto_url TEXT
        )""",
        # Clube de assinatura
        """CREATE TABLE IF NOT EXISTS clube_planos (
            id SERIAL PRIMARY KEY,
            slug VARCHAR(40) UNIQUE NOT NULL,
            nome VARCHAR(80) NOT NULL,
            modalidade VARCHAR(20) NOT NULL,
            preco_mensal NUMERIC(12,2) NOT NULL,
            ordem INT DEFAULT 0,
            ativo BOOLEAN DEFAULT TRUE,
            descricao TEXT,
            beneficios_json JSONB DEFAULT '[]'::jsonb,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS clube_assinaturas (
            id SERIAL PRIMARY KEY,
            cliente_id INT REFERENCES clientes_site(id),
            plano_id INT REFERENCES clube_planos(id),
            status VARCHAR(20) DEFAULT 'aguardando_pagto',
            proximo_envio DATE,
            ultimo_envio DATE,
            asaas_assinatura_id VARCHAR(60),
            iniciada_em TIMESTAMPTZ DEFAULT NOW(),
            cancelada_em TIMESTAMPTZ
        )""",
        """CREATE TABLE IF NOT EXISTS clube_envios (
            id SERIAL PRIMARY KEY,
            assinatura_id INT REFERENCES clube_assinaturas(id),
            referencia_mes VARCHAR(7),
            produto_pdv_id INT,
            descricao TEXT,
            enviado_em TIMESTAMPTZ DEFAULT NOW(),
            rastreio VARCHAR(60),
            observacao TEXT
        )""",
        # Carrinho temporário por sessão (pra anônimo)
        """CREATE TABLE IF NOT EXISTS carrinho_sessao (
            sessao_id VARCHAR(40) PRIMARY KEY,
            itens_json JSONB DEFAULT '[]'::jsonb,
            atualizado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        # Admin (uma linha, edita login/senha pelo painel)
        """CREATE TABLE IF NOT EXISTS admin_user (
            id SERIAL PRIMARY KEY,
            email VARCHAR(160) UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL
        )""",
        # CMS: paginas estaticas editaveis pelo admin
        """CREATE TABLE IF NOT EXISTS paginas_cms (
            slug VARCHAR(60) PRIMARY KEY,
            titulo VARCHAR(160) NOT NULL,
            conteudo TEXT NOT NULL,
            atualizado_em TIMESTAMP DEFAULT NOW()
        )""",
        # Configurações gerais (key/value)
        """CREATE TABLE IF NOT EXISTS site_config (
            chave VARCHAR(60) PRIMARY KEY,
            valor TEXT
        )""",
        # Banners do hero
        """CREATE TABLE IF NOT EXISTS banners (
            id SERIAL PRIMARY KEY,
            titulo VARCHAR(160),
            subtitulo VARCHAR(200),
            imagem_url TEXT,
            link TEXT,
            cta_texto VARCHAR(40) DEFAULT 'Ver',
            cor_fundo VARCHAR(20) DEFAULT '#4FB8FF',
            ordem INT DEFAULT 0,
            ativo BOOLEAN DEFAULT TRUE
        )""",
        # Cupons de desconto
        """CREATE TABLE IF NOT EXISTS cupons (
            id SERIAL PRIMARY KEY,
            codigo VARCHAR(40) UNIQUE NOT NULL,
            tipo VARCHAR(10) NOT NULL,
            valor NUMERIC(10,2) NOT NULL,
            valor_min NUMERIC(10,2) DEFAULT 0,
            usos_max INT,
            usos INT DEFAULT 0,
            valido_ate DATE,
            ativo BOOLEAN DEFAULT TRUE,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        "ALTER TABLE banners ADD COLUMN IF NOT EXISTS subtitulo VARCHAR(200)",
        "ALTER TABLE banners ADD COLUMN IF NOT EXISTS cta_texto VARCHAR(40) DEFAULT 'Ver'",
        "ALTER TABLE banners ADD COLUMN IF NOT EXISTS cor_fundo VARCHAR(20) DEFAULT '#4FB8FF'",
        # cor_fundo agora aceita gradientes longos (linear-gradient(...))
        "ALTER TABLE banners ALTER COLUMN cor_fundo TYPE VARCHAR(200)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cupom_codigo VARCHAR(40)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cupom_desconto NUMERIC(10,2) DEFAULT 0",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS embrulho_presente BOOLEAN DEFAULT FALSE",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS embrulho_mensagem VARCHAR(300)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS embrulho_tipo VARCHAR(10)",
        # Lista de aniversario (wishlist publica que a mae compartilha)
        """CREATE TABLE IF NOT EXISTS listas_aniversario (
            id SERIAL PRIMARY KEY,
            cliente_id INT REFERENCES clientes_site(id) ON DELETE CASCADE,
            nome_crianca VARCHAR(120) NOT NULL,
            idade INT,
            data_aniversario DATE,
            slug VARCHAR(40) UNIQUE NOT NULL,
            mensagem TEXT,
            ativo BOOLEAN DEFAULT TRUE,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_listas_cliente ON listas_aniversario(cliente_id)",
        "CREATE INDEX IF NOT EXISTS idx_listas_slug ON listas_aniversario(slug)",
        """CREATE TABLE IF NOT EXISTS lista_aniversario_itens (
            id SERIAL PRIMARY KEY,
            lista_id INT REFERENCES listas_aniversario(id) ON DELETE CASCADE,
            produto_pdv_id INT NOT NULL,
            qtd INT DEFAULT 1,
            comprado_por_nome VARCHAR(120),
            pedido_id INT REFERENCES pedidos(id) ON DELETE SET NULL,
            comprado_em TIMESTAMPTZ,
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(lista_id, produto_pdv_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_lista_itens_lista ON lista_aniversario_itens(lista_id)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS juros_valor NUMERIC(10,2) DEFAULT 0",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS entrega_agendada VARCHAR(40)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pontos_resgatados NUMERIC(10,2) DEFAULT 0",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS desconto_pontos NUMERIC(10,2) DEFAULT 0",
        # PNG base64 do QR Pix (pra mostrar na própria página, sem redirect)
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS asaas_pix_qr_image TEXT",
        # Linha digitável do boleto (pra copia-cola na própria página)
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS asaas_boleto_barcode VARCHAR(80)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS nfe_ref VARCHAR(80)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS nfe_numero VARCHAR(20)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS nfe_modelo VARCHAR(5)",
        # Fluxo de atendimento da caixa fisica (PDV Pro): quando pago, fica
        # na fila pra alguem aceitar; depois marca pronto pra retirar/enviar
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS aceito_em TIMESTAMPTZ",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS aceito_por VARCHAR(80)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pronto_em TIMESTAMPTZ",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pdv_cliente_id INT",  # vinculo permanente
        # Reconciliacao: contador + ultima tentativa de POST pro PDV Pro
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pdv_tentativas INT DEFAULT 0",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pdv_ultima_tentativa TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS idx_pedidos_pdv_pendente ON pedidos(pago_em) WHERE status='pago' AND pdv_venda_id IS NULL",
        # Cliente site → cliente loja física (mesmo CPF = mesma pessoa)
        "ALTER TABLE clientes_site ADD COLUMN IF NOT EXISTS pdv_cliente_id INT",
        "CREATE INDEX IF NOT EXISTS idx_pedidos_fila_caixa ON pedidos(status, aceito_em) WHERE status='pago' AND aceito_em IS NULL",
        # Luquizinha do site (chatbot IA)
        """CREATE TABLE IF NOT EXISTS site_chat_conversas (
            id SERIAL PRIMARY KEY,
            sessao_id VARCHAR(40) UNIQUE NOT NULL,
            ip VARCHAR(64),
            user_agent VARCHAR(200),
            cliente_id INT REFERENCES clientes_site(id) ON DELETE SET NULL,
            nome VARCHAR(120),
            idade_crianca INT,
            sexo_crianca VARCHAR(10),
            lead_marcado BOOLEAN DEFAULT FALSE,
            lead_telefone VARCHAR(20),
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            ultimo_msg_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_chat_sessao ON site_chat_conversas(sessao_id)",
        "CREATE INDEX IF NOT EXISTS idx_chat_ip ON site_chat_conversas(ip, criado_em)",
        """CREATE TABLE IF NOT EXISTS site_chat_mensagens (
            id SERIAL PRIMARY KEY,
            conversa_id INT REFERENCES site_chat_conversas(id) ON DELETE CASCADE,
            role VARCHAR(15) NOT NULL,
            content TEXT,
            blocks JSONB,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_chat_msgs ON site_chat_mensagens(conversa_id, id)",
        # Melhor Envio: além do etiqueta_id+rastreio que já existem,
        # precisamos guardar URL do PDF, nome do serviço, valor cotado.
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS melhorenvio_etiqueta_url TEXT",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS melhorenvio_servico_nome VARCHAR(80)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS melhorenvio_servico_id VARCHAR(20)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS melhorenvio_valor NUMERIC(12,2)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS melhorenvio_pago_em TIMESTAMPTZ",
        # Token imprevisível pra acesso às páginas públicas de pedido (anti-IDOR).
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS token VARCHAR(32)",
        # resultado do Purchase mandado pra Meta (CAPI) — sem isso a unica
        # forma de saber se o evento saiu era o log do Railway ou esperar as
        # 5h de atraso da API de estatisticas do Facebook.
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS capi_em TIMESTAMPTZ",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS capi_resposta TEXT",
        """CREATE TABLE IF NOT EXISTS rate_limit_hits (
            id BIGSERIAL PRIMARY KEY, bucket TEXT NOT NULL, chave TEXT NOT NULL,
            criado_em TIMESTAMPTZ DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS idx_rl_lookup ON rate_limit_hits (bucket, chave, criado_em)",
        "UPDATE pedidos SET token = substr(md5(random()::text || id::text || clock_timestamp()::text), 1, 24) WHERE token IS NULL",
        "ALTER TABLE clientes_site ADD COLUMN IF NOT EXISTS data_nascimento DATE",
        "ALTER TABLE clientes_site ADD COLUMN IF NOT EXISTS ganhou_primeira BOOLEAN DEFAULT FALSE",
        "ALTER TABLE avaliacoes ADD COLUMN IF NOT EXISTS foto_url TEXT",
        # Newsletter (opt-in pra promoções)
        """CREATE TABLE IF NOT EXISTS newsletter (
            id SERIAL PRIMARY KEY,
            email VARCHAR(160) UNIQUE NOT NULL,
            nome VARCHAR(160),
            ativo BOOLEAN DEFAULT TRUE,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        # Avaliações de produto
        """CREATE TABLE IF NOT EXISTS avaliacoes (
            id SERIAL PRIMARY KEY,
            produto_pdv_id INT NOT NULL,
            cliente_id INT REFERENCES clientes_site(id),
            pedido_id INT REFERENCES pedidos(id),
            estrelas INT NOT NULL CHECK (estrelas BETWEEN 1 AND 5),
            titulo VARCHAR(120),
            comentario TEXT,
            aprovado BOOLEAN DEFAULT FALSE,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_avaliacoes_produto ON avaliacoes(produto_pdv_id, aprovado)",
        # Carrinho abandonado: pedidos sem pagamento criados pelo checkout
        # já gravam linha em pedidos com status=aguardando_pagto. O cron pega
        # esses + 24h sem mexer.
        # Avise-me quando voltar ao estoque
        """CREATE TABLE IF NOT EXISTS avise_me (
            id SERIAL PRIMARY KEY,
            produto_pdv_id INT NOT NULL,
            email VARCHAR(160) NOT NULL,
            telefone VARCHAR(20),
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            notificado_em TIMESTAMPTZ,
            UNIQUE(produto_pdv_id, email)
        )""",
        # Wishlist / favoritos
        """CREATE TABLE IF NOT EXISTS wishlist (
            id SERIAL PRIMARY KEY,
            cliente_id INT REFERENCES clientes_site(id) ON DELETE CASCADE,
            produto_pdv_id INT NOT NULL,
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(cliente_id, produto_pdv_id)
        )""",
        # Checkout do clube: snapshot da primeira cobrança + endereço de envio
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS forma_pagto VARCHAR(10)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS valor NUMERIC(12,2)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS asaas_cobranca_id VARCHAR(60)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS asaas_link TEXT",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS asaas_pix_qrcode TEXT",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS asaas_pix_qr_image TEXT",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS asaas_boleto_url TEXT",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS asaas_boleto_barcode VARCHAR(80)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS pago_em TIMESTAMPTZ",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS cep VARCHAR(10)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS endereco VARCHAR(200)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS numero VARCHAR(20)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS complemento VARCHAR(100)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS bairro VARCHAR(100)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS cidade VARCHAR(100)",
        "ALTER TABLE clube_assinaturas ADD COLUMN IF NOT EXISTS uf VARCHAR(2)",
        # Analytics: pageviews do site público
        """CREATE TABLE IF NOT EXISTS site_visitas (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ DEFAULT NOW(),
            path VARCHAR(500) NOT NULL,
            referer VARCHAR(500),
            user_agent VARCHAR(300),
            ip_hash VARCHAR(64),
            is_bot BOOLEAN DEFAULT FALSE,
            cliente_id INT REFERENCES clientes_site(id) ON DELETE SET NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_site_visitas_ts ON site_visitas(ts DESC)",
        "CREATE INDEX IF NOT EXISTS idx_site_visitas_path ON site_visitas(path, ts DESC)",
        "CREATE INDEX IF NOT EXISTS idx_site_visitas_ip ON site_visitas(ip_hash, ts DESC)",
        # Crawler não vira linha crua (ver _track_visita) — só esta contagem.
        """CREATE TABLE IF NOT EXISTS site_visitas_bots_diario (
            dia DATE PRIMARY KEY,
            visitas BIGINT NOT NULL DEFAULT 0
        )""",
        # O que as pessoas procuram (busca do site + Luquizinha). Alimenta a
        # vitrine "Bombando nas buscas" da home e o relatório do admin.
        """CREATE TABLE IF NOT EXISTS site_buscas (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ DEFAULT NOW(),
            termo VARCHAR(120) NOT NULL,
            termo_norm VARCHAR(120) NOT NULL,
            origem VARCHAR(20) DEFAULT 'site',
            resultados INT DEFAULT 0,
            ip_hash VARCHAR(64),
            cliente_id INT REFERENCES clientes_site(id) ON DELETE SET NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_site_buscas_ts ON site_buscas(ts DESC)",
        "CREATE INDEX IF NOT EXISTS idx_site_buscas_termo ON site_buscas(termo_norm, ts DESC)",
        # Etiquetas Melhor Envio emitidas avulsas pela calculadora do PDV Pro
        # (sem vinculo com pedido do site). Guarda rastreio + PDF + dados do
        # destinatario pra auditoria e reenvio do link pelo WhatsApp.
        """CREATE TABLE IF NOT EXISTS etiquetas_avulsas (
            id SERIAL PRIMARY KEY,
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            origem VARCHAR(20) DEFAULT 'pdv-calc',
            cep_destino VARCHAR(10),
            destinatario_nome  VARCHAR(120),
            destinatario_doc   VARCHAR(20),
            destinatario_fone  VARCHAR(30),
            destinatario_email VARCHAR(120),
            endereco VARCHAR(200),
            numero VARCHAR(20),
            complemento VARCHAR(100),
            bairro VARCHAR(100),
            cidade VARCHAR(100),
            uf VARCHAR(2),
            servico_id  VARCHAR(20),
            servico_nome VARCHAR(80),
            valor_frete NUMERIC(12,2),
            valor_seguro NUMERIC(12,2),
            itens_json TEXT,
            me_etiqueta_id VARCHAR(60),
            me_etiqueta_url TEXT,
            me_rastreio VARCHAR(60)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_etiq_avulsa_dt ON etiquetas_avulsas(criado_em DESC)",
        # Servicos que o ME cota mas a transportadora recusa na postagem.
        # cep_prefixo NULL = recusa da origem, vale pra qualquer destino.
        # cep_prefixo_k existe so pra UNIQUE tratar NULL como valor (no
        # Postgres cada NULL e distinto e o ON CONFLICT nao pegaria).
        """CREATE TABLE IF NOT EXISTS me_bloqueios (
            id SERIAL PRIMARY KEY,
            service_id INT NOT NULL,
            cep_prefixo VARCHAR(5),
            cep_prefixo_k VARCHAR(5) GENERATED ALWAYS AS
                (COALESCE(cep_prefixo, '')) STORED,
            servico_nome VARCHAR(80),
            motivo TEXT,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_me_bloq ON me_bloqueios(service_id, cep_prefixo_k)",
        # ── Antifraude ────────────────────────────────────────────────────
        # O titular do cartao vinha no checkout transparente e era jogado
        # fora depois de mandar pro Asaas. Sem isso nao da pra saber se quem
        # pagou e quem comprou — que e exatamente a pergunta que aparece
        # quando um chargeback chega 60 dias depois.
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS titular_nome VARCHAR(80)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS titular_cpf VARCHAR(20)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS ip_cliente VARCHAR(45)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS risco_score INT DEFAULT 0",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS risco_motivos TEXT",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS risco_em TIMESTAMPTZ",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS risco_liberado_em TIMESTAMPTZ",
        # Contador de tentativas de cartão POR PEDIDO. Em 24/07 o pedido #48
        # levou 67 tentativas em 5min30 (uma a cada 5s) — script, não pessoa.
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS tentativas_cartao INT DEFAULT 0",
        # 4 ultimos digitos + bandeira do cartao que PAGOU (o Asaas devolve
        # isso na confirmacao; o numero completo nunca e guardado, e nem pode).
        # Em 27/07 o cartao VISA final 2746 pagou dois pedidos de CPFs, nomes,
        # e-mails e ESTADOS diferentes — Natal/RN e Salvador/BA. Sem guardar o
        # final, esse sinal, que e o mais forte de todos, era invisivel.
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cartao_final VARCHAR(4)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cartao_bandeira VARCHAR(20)",
        "CREATE INDEX IF NOT EXISTS idx_pedidos_cartao ON pedidos(cartao_final, criado_em DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pedidos_ip ON pedidos(ip_cliente, criado_em DESC)",
        "CREATE INDEX IF NOT EXISTS idx_pedidos_risco ON pedidos(risco_score DESC, criado_em DESC)",
        # Cache de reputacao de IP (RDAP). Sem cache, cada checkout pagaria
        # uma consulta externa; com cache e ~1 por IP novo.
        # Coordenadas de CEP, pra medir distância até a loja. Cache porque a
        # consulta é externa e o mesmo CEP se repete muito.
        """CREATE TABLE IF NOT EXISTS cep_geo (
            cep VARCHAR(8) PRIMARY KEY,
            lat DOUBLE PRECISION,
            lng DOUBLE PRECISION,
            cidade VARCHAR(120),
            uf VARCHAR(2),
            checado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS ip_reputacao (
            ip VARCHAR(45) PRIMARY KEY,
            rir VARCHAR(20),
            org VARCHAR(200),
            datacenter BOOLEAN DEFAULT FALSE,
            checado_em TIMESTAMPTZ DEFAULT NOW()
        )""",
    ]
    for ddl in ddls:
        try:
            db_execute(ddl)
        except Exception as e:
            log.error("init_db falhou em DDL: %s", e)

    # Seed admin se não existir
    try:
        if not db_execute("SELECT 1 FROM admin_user LIMIT 1", fetch='one'):
            db_execute(
                "INSERT INTO admin_user (email, senha_hash) VALUES (%s,%s)",
                [ADMIN_EMAIL, generate_password_hash(ADMIN_SENHA_PADRAO)])
            log.info("Admin seed: %s", ADMIN_EMAIL)
    except Exception as e:
        log.error("seed admin: %s", e)

    # Seed dos 6 planos do clube
    try:
        if not db_execute("SELECT 1 FROM clube_planos LIMIT 1", fetch='one'):
            planos = [
                ('smart-mensal', 'Plano Smart', 'mensal', 79.99, 1,
                 ['1 brinquedo surpresa a cada mês',
                  'Marca página lúdico com personagens',
                  'Material auxiliar instrutivo']),
                ('essencial-mensal', 'Plano Essencial', 'mensal', 129.99, 2,
                 ['1 brinquedo surpresa a cada mês',
                  '1 livro a cada mês',
                  'Marca página lúdico com personagens',
                  'Material auxiliar instrutivo',
                  '10% de desconto na loja física']),
                ('premium-mensal', 'Plano Premium', 'mensal', 199.99, 3,
                 ['2 brinquedos surpresa a cada mês',
                  '2 livros a cada mês',
                  'Marca página lúdico com personagens',
                  '10% de desconto na loja física',
                  'Uma inscrição GRÁTIS no evento mensal da Luqui',
                  'Material auxiliar instrutivo']),
                ('smart-anual', 'Plano Smart Anual', 'anual', 70.99, 4,
                 ['1 brinquedo surpresa a cada mês',
                  'Marca página lúdico com personagens',
                  'Economia de R$ 9,00/mês vs mensal']),
                ('essencial-anual', 'Plano Essencial Anual', 'anual', 116.99, 5,
                 ['1 brinquedo surpresa a cada mês',
                  '1 livro a cada mês',
                  'Marca página lúdico com personagens',
                  '10% de desconto na loja física',
                  'Economia de R$ 13,00/mês vs mensal']),
                ('premium-anual', 'Plano Premium Anual', 'anual', 179.99, 6,
                 ['2 brinquedos surpresa a cada mês',
                  '2 livros a cada mês',
                  'Marca página lúdico com personagens',
                  '10% de desconto na loja física',
                  'Uma inscrição GRÁTIS no evento mensal',
                  'Material auxiliar instrutivo',
                  'Economia de R$ 20,00/mês vs mensal']),
            ]
            for slug, nome, mod, preco, ordem, beneficios in planos:
                db_execute(
                    """INSERT INTO clube_planos
                       (slug, nome, modalidade, preco_mensal, ordem, beneficios_json)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    [slug, nome, mod, preco, ordem,
                     json.dumps(beneficios, ensure_ascii=False)])
            log.info("Seed: 6 planos do clube criados")
    except Exception as e:
        log.error("seed planos: %s", e)

    # Cupom de primeira compra
    try:
        db_execute("""INSERT INTO cupons (codigo, tipo, valor, valor_min, ativo)
                      VALUES ('PRIMEIRO10','pct',10,50,true)
                      ON CONFLICT (codigo) DO NOTHING""")
    except Exception as e:
        log.error("seed PRIMEIRO10: %s", e)

    # Banners default — insere os que ainda nao existem (checa por titulo).
    # Assim Lucas pode editar/desativar pelo /admin/banners sem perder no
    # proximo deploy, e novos banners do seed entram sem duplicar os antigos.
    try:
        seeds = [
            ('PAGUE NO PIX',
             'Ganhe <b style="color:#FFC700">3% de desconto</b> em qualquer compra!',
             '/produtos', 'Ver produtos',
             'linear-gradient(135deg,#1652C7,#3B82F6)', 1),
            ('PARCELE EM 12X',
             '<b style="color:#FFC700">À vista no cartão</b> ou parcelado com juros',
             '/produtos', 'Comprar agora',
             'linear-gradient(135deg,#0E3D9E,#1652C7)', 2),
            ('RETIRE NA LOJA',
             '📍 Cascavel/PR — <b style="color:#FFC700">frete grátis</b>. Agende o horário no checkout!',
             '/retirar-na-loja', 'Como funciona',
             'linear-gradient(135deg,#1652C7,#4FB8FF)', 3),
            ('CLUBE LUQUI 🎁',
             'Descontos exclusivos + <b style="color:#FFC700">5% acumulativo</b> + 1 ponto a cada R$ 1',
             '/clube', 'Quero entrar',
             'linear-gradient(135deg,#A16207,#FFC700)', 4),
            ('NOVIDADES ✨',
             'Confere o que <b style="color:#FFC700">chegou</b> de mais legal!',
             '/novidades', 'Ver novidades',
             'linear-gradient(135deg,#1652C7,#3B82F6)', 5),
        ]
        inseridos = 0
        for tit, sub, link, cta, cor, ordem in seeds:
            ja = db_execute("SELECT id FROM banners WHERE titulo=%s",
                            [tit], fetch='one')
            if ja:
                continue
            db_execute("""INSERT INTO banners
              (titulo, subtitulo, link, cta_texto, cor_fundo, ordem, ativo)
              VALUES (%s,%s,%s,%s,%s,%s,true)""",
              [tit, sub, link, cta, cor, ordem])
            inseridos += 1
        if inseridos:
            log.info(f"Seed: {inseridos} banner(s) default criado(s)")
    except Exception as e:
        log.error("seed banners: %s", e)

    # Configs default
    try:
        defaults = {
            # Cidades com frete LOCAL FIXO (default só Cascavel, R$ 10).
            # Toledo saiu — usa cotação ME normal.
            'frete_fixo_cidades': 'Cascavel',
            'frete_fixo_uf': 'PR',
            'frete_fixo_cascavel': '10',
            # Deprecated (mantido por compat com configs antigas no banco)
            'frete_gratis_cidades': '',
            'frete_gratis_uf': '',
            'desconto_pix_pct': '3',
            'desconto_boleto_pct': '3',
            'parcelamento_max': '12',
            'parcelas_sem_juros_max': '1',  # so 1x sem juros; 2x+ ja tem juros
            'parcela_minima': '50',  # legado (nao usado mais no calculo)
            'juros_parcelamento_am': '2.49',  # % ao mes, acima do limite sem juros
            'whatsapp_loja': WHATSAPP_LOJA,
            # Melhor Envio — preencher em /admin/melhorenvio
            'me_cep_origem': '85801080',  # Luqui Brinquedos Cascavel
            'me_remetente_nome': 'Luqui Brinquedos',
            'me_remetente_cnpj': '',
            'me_remetente_cnae': '',
            'me_remetente_telefone': '',
            'me_remetente_email': '',
            'me_remetente_logradouro': '',
            'me_remetente_numero': '',
            'me_remetente_complemento': '',
            'me_remetente_bairro': '',
            'me_remetente_cidade': 'Cascavel',
            'me_remetente_uf': 'PR',
            # Caixa "padrão" pra produto que não tem dimensão (cm) — usado na cotação
            'me_caixa_padrao_largura': '20',
            'me_caixa_padrao_altura': '15',
            'me_caixa_padrao_comprimento': '25',
            'me_caixa_padrao_peso_kg': '0.5',
            # Retirada na loja
            'retirada_loja_ativa': '1',
            'loja_endereco_completo': 'R. Eng. Rebouças, 2053 - Cascavel/PR',
            'loja_horario_funcionamento': 'Seg a Sex: 9h às 18h · Sáb: 9h às 13h',
            'loja_tempo_separacao_min': '30',
        }
        for k, v in defaults.items():
            db_execute("""INSERT INTO site_config (chave, valor) VALUES (%s,%s)
                          ON CONFLICT (chave) DO NOTHING""", [k, v])
        # Migração one-time: corrige horario antigo (Seg-Sex abria 8h, é 9h)
        db_execute("UPDATE site_config SET valor=%s "
                   "WHERE chave='loja_horario_funcionamento' "
                   "  AND valor='Seg a Sex: 8h às 18h · Sáb: 9h às 13h'",
                   ['Seg a Sex: 9h às 18h · Sáb: 9h às 13h'])
        # Migração one-time: tira Toledo da lista de frete-grátis antiga
        # (agora a regra é frete_fixo_cidades=Cascavel, Toledo cota no ME)
        db_execute("UPDATE site_config SET valor='' "
                   "WHERE chave='frete_gratis_cidades' "
                   "  AND valor IN ('Cascavel,Toledo','Toledo,Cascavel',"
                   "                'Cascavel','Toledo')")
        # Migração one-time: desconto PIX subiu de 5% pra 10% (so atualiza
        # se ainda estiver no valor antigo padrao)
        db_execute("UPDATE site_config SET valor='10' "
                   "WHERE chave='desconto_pix_pct' AND valor='5'")
        # Migração one-time 2026-05-28: PIX baixou de 10% pra 3%
        db_execute("UPDATE site_config SET valor='3' "
                   "WHERE chave='desconto_pix_pct' AND valor='10'")
        # Migração one-time 2026-06-03: Boleto equiparado ao PIX (3% em vez de 5%)
        # — boleto tem taxa do Asaas, 5% off ficava negativo
        db_execute("UPDATE site_config SET valor='3' "
                   "WHERE chave='desconto_boleto_pct' AND valor='5'")
        # Atualiza o subtitulo do banner "PAGUE NO PIX" se ainda estiver
        # no texto antigo de 10%
        db_execute(
            # %% escapado: psycopg2 faz interpolação no texto da query quando
            # há params, e '% d' quebra com "list index out of range" — o que
            # abortava TODAS as migrações abaixo deste ponto.
            "UPDATE banners SET subtitulo = REPLACE(subtitulo, '10%% de desconto', '3%% de desconto') "
            "WHERE titulo='PAGUE NO PIX' AND subtitulo LIKE %s",
            ['%10% de desconto%'])
        # Migração 2026-05-28: cartão agora só 1x sem juros; troca o
        # subtitulo do banner "PARCELE EM 12X" se ainda estiver com o
        # texto antigo "Sem juros (parcela mínima R$ 50)"
        db_execute(
            "UPDATE banners SET subtitulo=%s "
            "WHERE titulo='PARCELE EM 12X' AND subtitulo LIKE %s",
            ['<b style="color:#FFC700">À vista no cartão</b> ou parcelado com juros',
             '%Sem juros%parcela m%nima%'])
    except Exception as e:
        log.error("seed config: %s", e)

    # Backfill one-time: as buscas que a Luquizinha já fez estão gravadas
    # nos blocks das conversas. Sem isso a vitrine "Bombando nas buscas"
    # ficaria vazia por dias esperando gente buscar de novo.
    try:
        n = (db_execute("SELECT COUNT(*) AS n FROM site_buscas",
                        fetch='one') or {}).get('n', 0)
        if not n:
            db_execute("""
                INSERT INTO site_buscas (ts, termo, termo_norm, origem, resultados)
                SELECT m.criado_em,
                       LEFT(b->'input'->>'termo', 120),
                       LEFT(LOWER(TRIM(b->'input'->>'termo')), 120),
                       'luquizinha', 1
                  FROM site_chat_mensagens m,
                       LATERAL jsonb_array_elements(m.blocks) b
                 WHERE m.blocks IS NOT NULL
                   AND b->>'type' = 'tool_use'
                   AND b->>'name' = 'buscar_produtos'
                   AND COALESCE(TRIM(b->'input'->>'termo'), '') <> ''""")
    except Exception as e:
        log.error("backfill buscas: %s", e)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def cfg(chave, default=''):
    r = db_execute("SELECT valor FROM site_config WHERE chave=%s",
                   [chave], fetch='one')
    return r['valor'] if r else default


def cfg_set(chave, valor):
    db_execute("""INSERT INTO site_config (chave, valor) VALUES (%s,%s)
                  ON CONFLICT (chave) DO UPDATE SET valor=EXCLUDED.valor""",
               [chave, '' if valor is None else str(valor)])


# ─── Melhor Envio — OAuth + cotação + etiqueta ───────────────────────────────
ME_CLIENT_ID     = os.environ.get('MELHOR_ENVIO_CLIENT_ID', '')
ME_CLIENT_SECRET = os.environ.get('MELHOR_ENVIO_CLIENT_SECRET', '')
# 'sandbox' (default) ou 'prod'
ME_AMBIENTE      = os.environ.get('MELHOR_ENVIO_AMBIENTE', 'sandbox').lower()
ME_USER_AGENT    = os.environ.get(
    'MELHOR_ENVIO_USER_AGENT',
    'LuquiShop (lucasfagundes91@hotmail.com)')
ME_REDIRECT_URI  = os.environ.get('MELHOR_ENVIO_REDIRECT_URI', '')
ME_SCOPES = (
    'cart-read cart-write shipping-calculate shipping-checkout '
    'shipping-generate shipping-print shipping-tracking shipping-cancel '
    'shipping-companies users-read purchases-read')


def me_base():
    return ('https://www.melhorenvio.com.br' if ME_AMBIENTE == 'prod'
            else 'https://sandbox.melhorenvio.com.br')


def me_redirect_uri():
    if ME_REDIRECT_URI:
        return ME_REDIRECT_URI
    base = (request.url_root.rstrip('/') if request else
            os.environ.get('SITE_URL', '').rstrip('/'))
    return f"{base}/admin/melhorenvio/callback"


def me_configurado():
    return bool(ME_CLIENT_ID and ME_CLIENT_SECRET)


def me_token_atual():
    """Devolve access_token válido ou None. Faz refresh se vencendo."""
    tok = cfg('me_access_token')
    if not tok:
        return None
    venc = cfg('me_expires_at')
    try:
        venc_t = float(venc) if venc else 0
    except ValueError:
        venc_t = 0
    if venc_t and time.time() < venc_t - 60:
        return tok
    # Vencendo ou vencido — tenta refresh
    rt = cfg('me_refresh_token')
    if not rt:
        return tok or None  # tenta com o que tem (pode falhar)
    try:
        r = requests.post(
            f"{me_base()}/oauth/token",
            json={
                'grant_type': 'refresh_token',
                'refresh_token': rt,
                'client_id': ME_CLIENT_ID,
                'client_secret': ME_CLIENT_SECRET,
            },
            headers={'Accept': 'application/json',
                     'User-Agent': ME_USER_AGENT},
            timeout=20)
        if r.ok:
            d = r.json()
            cfg_set('me_access_token', d.get('access_token', ''))
            if d.get('refresh_token'):
                cfg_set('me_refresh_token', d['refresh_token'])
            exp_in = int(d.get('expires_in') or 0)
            if exp_in:
                cfg_set('me_expires_at', str(time.time() + exp_in))
            return d.get('access_token')
        log.warning("ME refresh falhou: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("ME refresh erro: %s", e)
    return tok or None


def me_request(method, path, *, json_body=None, params=None, timeout=30):
    """Chamada à API do Melhor Envio com Bearer + User-Agent."""
    tok = me_token_atual()
    if not tok:
        raise RuntimeError('Melhor Envio não conectado. '
                           'Configure em /admin/melhorenvio.')
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {tok}',
        'User-Agent': ME_USER_AGENT,
    }
    url = f"{me_base()}{path}"
    r = requests.request(method, url, headers=headers, json=json_body,
                         params=params, timeout=timeout)
    return r


def me_erro_texto(r, padrao='erro no Melhor Envio'):
    """Extrai a mensagem REAL de uma resposta de erro do Melhor Envio.

    O ME devolve o motivo em formatos diferentes conforme o endpoint:
    {"message": "..."} , {"error": "..."} ou {"errors": {"campo": ["..."]}}.
    Sem isso o operador recebia "checkout ME falhou" e tinha que adivinhar se
    era saldo, endereco invalido, token vencido ou dimensao recusada.
    """
    try:
        d = r.json()
    except Exception:
        txt = (getattr(r, 'text', '') or '').strip()
        return (txt[:300] or padrao)
    if isinstance(d, str):
        return d[:300]
    if not isinstance(d, dict):
        return padrao
    msg = d.get('message') or d.get('error') or ''
    partes = []
    errs = d.get('errors')
    if isinstance(errs, dict):
        for campo, v in errs.items():
            v = v if isinstance(v, list) else [v]
            partes.append(f"{campo}: " + '; '.join(str(x) for x in v))
    elif isinstance(errs, list):
        partes += [str(x) for x in errs]
    full = ' · '.join(x for x in ([str(msg)] if msg else []) + partes)
    return (full or padrao)[:400]


def me_saldo_atual():
    """Saldo em conta no Melhor Envio, ou None se nao der pra ler."""
    try:
        r = me_request('GET', '/api/v2/me/balance')
        if r.ok:
            return float((r.json() or {}).get('balance') or 0)
        r2 = me_request('GET', '/api/v2/me')
        if r2.ok:
            return float((r2.json() or {}).get('balance') or 0)
    except Exception as e:
        log.warning("me_saldo_atual: %s", e)
    return None


def me_remetente_dict():
    """Monta o payload `from` esperado pelo Melhor Envio nos endpoints
    de carrinho (precisa de dados completos do remetente)."""
    cnpj = ''.join(c for c in cfg('me_remetente_cnpj', '') if c.isdigit())
    # CNAE do remetente: transportadora passou a exigir pra remetente CNPJ e o
    # Melhor Envio trava a finalizacao da etiqueta pedindo o codigo. Vai so em
    # digitos (7), formato 0000000 -> ex. 4763601 = comercio varejista de
    # brinquedos. Fica em branco quando remetente e PF, que nao tem CNAE.
    cnae = ''.join(c for c in cfg('me_remetente_cnae', '') if c.isdigit())
    d = {
        'name':         cfg('me_remetente_nome', 'Luqui Brinquedos'),
        'phone':        cfg('me_remetente_telefone', ''),
        'email':        cfg('me_remetente_email', ''),
        'document':     cnpj if len(cnpj) == 11 else '',
        'company_document': cnpj if len(cnpj) == 14 else '',
        'address':      cfg('me_remetente_logradouro', ''),
        'complement':   cfg('me_remetente_complemento', ''),
        'number':       cfg('me_remetente_numero', ''),
        'district':     cfg('me_remetente_bairro', ''),
        'city':         cfg('me_remetente_cidade', 'Cascavel'),
        'state_abbr':   cfg('me_remetente_uf', 'PR'),
        'country_id':   'BR',
        'postal_code':  ''.join(c for c in cfg('me_cep_origem', '')
                                if c.isdigit()),
    }
    if len(cnae) == 7:
        d['economic_activity_code'] = cnae
    return d


def me_caixa_default():
    """Caixa fallback pra produto sem dimensão cadastrada."""
    def _f(k, default):
        try: return float(cfg(k, default) or default)
        except ValueError: return float(default)
    return {
        'width':  _f('me_caixa_padrao_largura', 20),
        'height': _f('me_caixa_padrao_altura', 15),
        'length': _f('me_caixa_padrao_comprimento', 25),
        'weight': _f('me_caixa_padrao_peso_kg', 0.5),
    }


def me_volume_dos_itens(itens):
    """Soma peso e usa MAIORES dimensões dos itens — formato `products` do
    endpoint de cotação. Cada item vira um 'package' separado conforme
    quantidade (ME prefere assim que volumes consolidados)."""
    cx = me_caixa_default()
    out = []
    for i, it in enumerate(itens):
        # Tenta puxar dimensão real do produto do PDV (já vem em /api/integracao/produtos)
        p = it.get('produto') or {}
        try:
            largura = float(p.get('largura_cm') or 0)
        except (TypeError, ValueError):
            largura = 0
        try:
            altura = float(p.get('altura_cm') or 0)
        except (TypeError, ValueError):
            altura = 0
        try:
            comprimento = float(p.get('comprimento_cm') or 0)
        except (TypeError, ValueError):
            comprimento = 0
        try:
            peso = float(p.get('peso_bruto') or 0)
        except (TypeError, ValueError):
            peso = 0
        qtd = max(1, int(float(it.get('qtd') or 1)))
        valor = float(it.get('preco') or 0)
        out.append({
            'id':              str(i + 1),
            'width':           largura  if largura  > 0 else cx['width'],
            'height':          altura   if altura   > 0 else cx['height'],
            'length':          comprimento if comprimento > 0 else cx['length'],
            'weight':          peso     if peso     > 0 else cx['weight'],
            'insurance_value': round(valor * qtd, 2),
            'quantity':        qtd,
        })
    return out


def me_cotar(cep_destino, itens):
    """Retorna lista de opções de frete (servico, valor, prazo, id).
    Força lista de services pra API retornar TODAS as transportadoras
    disponíveis na conta — sem isso o ME só devolve Correios por padrão.
    Os 15 IDs cobrem Correios, Jadlog, LATAM, Azul, Buslog, Loggi, J&T,
    Total Express (que estiverem habilitadas — as outras vêm com erro
    que a gente filtra em opt.get('error'))."""
    cep_destino = ''.join(c for c in (cep_destino or '') if c.isdigit())
    if len(cep_destino) != 8:
        return []
    body = {
        'from':     {'postal_code': ''.join(c for c in cfg('me_cep_origem','')
                                            if c.isdigit())},
        'to':       {'postal_code': cep_destino},
        'products': me_volume_dos_itens(itens),
        # Forca cotacao em todos os services habilitados (sem isso so vem Correios)
        'services': '1,2,3,4,12,15,16,17,22,27,31,32,33,34,35',
    }
    r = me_request('POST', '/api/v2/me/shipment/calculate', json_body=body)
    if not r.ok:
        log.warning("ME cotar %s: %s", r.status_code, r.text[:300])
        return []
    out = []
    for opt in r.json() or []:
        if opt.get('error'):
            continue  # serviço indisponível pra esse trecho
        out.append({
            'id':       opt.get('id'),
            'servico':  f"{opt.get('company',{}).get('name','')} "
                        f"{opt.get('name','')}".strip(),
            'valor':    float(opt.get('custom_price') or opt.get('price') or 0),
            'prazo':    f"{opt.get('delivery_time') or '?'} dias úteis",
            'company':  opt.get('company', {}).get('name', ''),
        })
    # Tira as que o Melhor Envio cota mas a transportadora recusa na postagem.
    out = me_filtrar_bloqueados(out, cep_destino)
    # Ordena por preço crescente (mais barato primeiro). Empate: prazo menor.
    out.sort(key=lambda o: (o['valor'], o.get('prazo', '')))
    return out


# ─── Servicos que cotam mas recusam na hora de postar ────────────────────────
# O /shipment/calculate do ME cota QUALQUER servico habilitado; quem valida as
# regras da transportadora e o /me/cart, la na emissao da etiqueta. Resultado:
# o cliente escolhia e PAGAVA um frete impossivel, e a loja so descobria ao
# tentar postar. Foi o pedido #35 — cliente pagou LATAM e o aeroporto de
# destino nao aceita declaracao de conteudo.
#
# Duas naturezas de recusa:
#  - fixa da loja (origem): "nao aceita envios nao-comerciais partindo deste
#    estado" (Jadlog, saindo do PR). Vale pra qualquer destino -> cep_prefixo NULL.
#  - por destino: "o aeroporto de destino nao aceita..." (LATAM). Depende da
#    regiao -> guarda os 3 primeiros digitos do CEP.
def me_filtrar_bloqueados(opcoes, cep_destino):
    cep = ''.join(c for c in (cep_destino or '') if c.isdigit())
    try:
        # Bloqueio expira em 60 dias. Ele nasce de uma recusa observada, e uma
        # recusa pode vir de bug NOSSO, nao de regra da transportadora — foi o
        # que aconteceu com LATAM e Jadlog, recusadas por falta da chave da
        # NF-e no payload. Sem prazo, o erro de um dia vira regra permanente e
        # esconde pra sempre uma opcao boa (a LATAM e aerea, a mais rapida).
        rows = db_execute(
            "SELECT service_id, cep_prefixo FROM me_bloqueios "
            "WHERE criado_em > NOW() - INTERVAL '60 days'", fetch='all') or []
    except Exception as e:
        log.warning("me_filtrar_bloqueados: %s", e)
        return opcoes
    bloq = set()
    for r in rows:
        pref = (r.get('cep_prefixo') or '').strip()
        if not pref or (cep and cep.startswith(pref)):
            bloq.add(str(r['service_id']))
    if not bloq:
        return opcoes
    fora = [o for o in opcoes if str(o.get('id')) in bloq]
    if fora:
        log.info("cotacao %s: escondendo %s (bloqueio conhecido)",
                 cep, ', '.join(o.get('servico', '?') for o in fora))
    return [o for o in opcoes if str(o.get('id')) not in bloq]


def me_registrar_bloqueio(service_id, cep_destino, motivo, servico_nome=''):
    """Aprende com a recusa: da proxima vez esse servico nao e nem oferecido.

    Recusa que cita a origem vale pra sempre; as demais ficam presas a regiao
    do CEP (3 digitos), que e mais ou menos o alcance de um aeroporto/base.
    """
    m = (motivo or '').lower()
    origem = ('partindo deste estado' in m or 'partindo do estado' in m
              or 'nao-comerciais' in m or 'não-comerciais' in m)
    cep = ''.join(c for c in (cep_destino or '') if c.isdigit())
    pref = None if origem else (cep[:3] or None)
    if not origem and not pref:
        return
    try:
        db_execute("""INSERT INTO me_bloqueios
                        (service_id, cep_prefixo, servico_nome, motivo)
                      VALUES (%s,%s,%s,%s)
                      ON CONFLICT (service_id, cep_prefixo_k) DO UPDATE
                        SET motivo=EXCLUDED.motivo, criado_em=NOW()""",
                   [int(service_id), pref, (servico_nome or '')[:80],
                    (motivo or '')[:400]])
        log.warning("bloqueio ME aprendido: servico %s (%s) cep_prefixo=%s — %s",
                    service_id, servico_nome, pref or 'TODOS', (motivo or '')[:160])
    except Exception as e:
        log.warning("me_registrar_bloqueio: %s", e)


# Rotas Melhor Envio que precisam de @requer_admin: definidas logo após
# a definição do requer_admin (mais abaixo neste arquivo).
# ─── Fim Melhor Envio (helpers) ──────────────────────────────────────────────


def rs(v):
    """Formata R$ 1.234,56."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0.0
    s = f"{v:,.2f}"
    return 'R$ ' + s.replace(',', 'X').replace('.', ',').replace('X', '.')


app.jinja_env.globals['rs'] = rs
app.jinja_env.globals['cfg'] = cfg
app.jinja_env.globals['whatsapp_loja'] = lambda: cfg('whatsapp_loja', WHATSAPP_LOJA)


@app.template_filter('titulo')
def _filtro_titulo(s):
    """Title case PT-BR: 'CARRO MOTO NAVAL AVIAO' -> 'Carro Moto Naval Aviao'.
    Preposicoes/artigos curtos ficam em minuscula."""
    if not s:
        return s
    pequenas = {'de', 'da', 'do', 'das', 'dos', 'e', 'a', 'o', 'as', 'os'}
    palavras = str(s).lower().split()
    return ' '.join(
        p.capitalize() if (i == 0 or p not in pequenas) else p
        for i, p in enumerate(palavras)
    )


# Cache do mega-menu pra nao bater /api/integracao/filtros em todo request
_MENU_CACHE = {'data': None, 'ts': 0}
_MENU_TTL = 300  # 5 minutos

def _menu_hierarquico():
    """Devolve a hierarquia depto > grupo > subgrupo pronta pro mega-menu.
    Estrutura:
      [{slug, nome, qtd, grupos: [{slug, nome, qtd, subgrupos: [...]}]}, ...]
    Cacheado por 5 min porque sao usados em todas as paginas (header)."""
    import time
    agora = time.time()
    if _MENU_CACHE['data'] and (agora - _MENU_CACHE['ts']) < _MENU_TTL:
        return _MENU_CACHE['data']
    f = listar_filtros()
    deps = f.get('departamentos', []) or []
    grupos = f.get('grupos', []) or []
    subs = f.get('subgrupos', []) or []
    # Indexa subs por pai_slug (grupo_slug)
    subs_por_grupo = {}
    for s in subs:
        subs_por_grupo.setdefault(s.get('pai_slug') or '', []).append(s)
    # Indexa grupos por pai_slug (depto_slug) + anexa subgrupos
    grps_por_depto = {}
    for g in grupos:
        g2 = dict(g)
        g2['subgrupos'] = subs_por_grupo.get(g.get('slug') or '', [])
        grps_por_depto.setdefault(g.get('pai_slug') or '', []).append(g2)
    # Monta a lista de departamentos com grupos aninhados
    out = []
    for d in deps:
        d2 = dict(d)
        d2['grupos'] = grps_por_depto.get(d.get('slug') or '', [])
        out.append(d2)
    _MENU_CACHE['data'] = out
    _MENU_CACHE['ts'] = agora
    return out


def _faixas_etarias_topbar():
    """Lista de faixas etarias disponiveis no PDV, pro dropdown do header.
    Ja vem ordenada numericamente e com flag em_meses (separa bebes x criancas)."""
    try:
        return _ordenar_faixas_etarias(
            listar_filtros().get('faixas_etarias', []) or [])
    except Exception:
        return []


@app.context_processor
def _ctx_globals():
    # menu_hierarquico esta disponivel em TODA pagina (carrega cacheado)
    try:
        menu = _menu_hierarquico()
    except Exception:
        menu = []
    return {'ano': datetime.now(SP_TZ).year,
            'META_PIXEL_ID': META_PIXEL_ID,
            'GOOGLE_TAG_ID': GOOGLE_TAG_ID,
            'menu_hierarquico': menu,
            'faixas_etarias_topbar': _faixas_etarias_topbar()}


@app.route('/api/checkout/cupom')
def checkout_aplicar_cupom():
    codigo = (request.args.get('codigo') or '').strip().upper()
    subtotal = float(request.args.get('subtotal') or 0)
    if not codigo:
        return jsonify({'erro': 'Digite o código'}), 400
    if codigo == 'PRIMEIRO10' and not CUPOM_PRIMEIRA_COMPRA_ATIVO:
        return jsonify({'erro': 'Cupom inválido ou expirado'}), 404
    c = db_execute("""SELECT * FROM cupons WHERE UPPER(codigo)=%s AND ativo
                      AND (valido_ate IS NULL OR valido_ate >= CURRENT_DATE)
                      AND (usos_max IS NULL OR usos < usos_max)""",
                   [codigo], fetch='one')
    if not c:
        return jsonify({'erro': 'Cupom inválido ou expirado'}), 404
    if subtotal < float(c['valor_min'] or 0):
        return jsonify({'erro': f'Pedido mínimo de R$ {c["valor_min"]} pra usar esse cupom'}), 400
    if c['tipo'] == 'pct':
        desconto = round(subtotal * float(c['valor']) / 100, 2)
    else:  # 'rs'
        desconto = min(float(c['valor']), subtotal)
    return jsonify({'ok': True, 'codigo': c['codigo'],
                    'tipo': c['tipo'], 'valor': float(c['valor']),
                    'desconto': desconto})


def cliente_logado():
    cid = session.get('cliente_id')
    if not cid:
        return None
    return db_execute("SELECT * FROM clientes_site WHERE id=%s",
                      [cid], fetch='one')


def admin_logado():
    return session.get('admin_id') is not None


def pedido_acesso_ok(p):
    """As páginas públicas de pedido (pagamento/tracking) exigem prova de posse
    pra evitar IDOR por id sequencial: admin logado, dono logado, ou ?t=<token>."""
    if not p:
        return False
    if admin_logado():
        return True
    cl = cliente_logado()
    if cl and p.get('cliente_id') and cl.get('id') == p.get('cliente_id'):
        return True
    tok = p.get('token') or ''
    return bool(tok) and secrets.compare_digest(str(request.args.get('t') or ''), str(tok))


def requer_admin(f):
    @wraps(f)
    def w(*a, **kw):
        if not admin_logado():
            return redirect(url_for('admin_login', next=request.path))
        return f(*a, **kw)
    return w


# ─── Melhor Envio: rotas que dependem de requer_admin ────────────────────────
@app.route('/admin/melhorenvio/conectar')
@requer_admin
def melhorenvio_conectar():
    if not me_configurado():
        return ('Configure as variáveis MELHOR_ENVIO_CLIENT_ID e '
                'MELHOR_ENVIO_CLIENT_SECRET no Railway antes.'), 400
    state = os.urandom(12).hex()
    session['me_oauth_state'] = state
    qs = urlencode({
        'client_id':     ME_CLIENT_ID,
        'redirect_uri':  me_redirect_uri(),
        'response_type': 'code',
        'scope':         ME_SCOPES,
        'state':         state,
    })
    return redirect(f"{me_base()}/oauth/authorize?{qs}")


@app.route('/admin/melhorenvio/callback')
@requer_admin
def melhorenvio_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    if not code:
        return f"Erro do Melhor Envio: {request.args.get('error','sem código')}", 400
    if state and session.pop('me_oauth_state', None) != state:
        return 'state inválido — refaça o fluxo.', 400
    try:
        r = requests.post(
            f"{me_base()}/oauth/token",
            json={
                'grant_type':    'authorization_code',
                'client_id':     ME_CLIENT_ID,
                'client_secret': ME_CLIENT_SECRET,
                'redirect_uri':  me_redirect_uri(),
                'code':          code,
            },
            headers={'Accept': 'application/json',
                     'User-Agent': ME_USER_AGENT},
            timeout=20)
        if not r.ok:
            return f"Token falhou: {r.status_code} — {r.text[:300]}", 400
        d = r.json()
        cfg_set('me_access_token', d.get('access_token', ''))
        cfg_set('me_refresh_token', d.get('refresh_token', ''))
        if d.get('expires_in'):
            cfg_set('me_expires_at',
                    str(time.time() + int(d['expires_in'])))
        cfg_set('me_ambiente', ME_AMBIENTE)
    except Exception as e:
        return f'Erro conectando: {e}', 500
    return redirect('/admin/melhorenvio?conectado=1')


@app.route('/admin/melhorenvio/desconectar', methods=['POST'])
@requer_admin
def melhorenvio_desconectar():
    cfg_set('me_access_token', '')
    cfg_set('me_refresh_token', '')
    cfg_set('me_expires_at', '')
    return redirect('/admin/melhorenvio')


@app.route('/admin/melhorenvio', methods=['GET', 'POST'])
@requer_admin
def admin_melhorenvio():
    if request.method == 'POST':
        for k in ('me_cep_origem', 'me_remetente_nome', 'me_remetente_cnpj',
                  'me_remetente_cnae',
                  'me_remetente_telefone', 'me_remetente_email',
                  'me_remetente_logradouro', 'me_remetente_numero',
                  'me_remetente_complemento', 'me_remetente_bairro',
                  'me_remetente_cidade', 'me_remetente_uf',
                  'me_caixa_padrao_largura', 'me_caixa_padrao_altura',
                  'me_caixa_padrao_comprimento', 'me_caixa_padrao_peso_kg',
                  # Frete local (entrega própria) — Cascavel etc
                  'frete_fixo_cidades', 'frete_fixo_uf',
                  'frete_fixo_cascavel'):
            v = (request.form.get(k) or '').strip()
            cfg_set(k, v)
        return redirect('/admin/melhorenvio?salvo=1')
    conectado = bool(cfg('me_access_token'))
    saldo = None
    if conectado:
        try:
            r = me_request('GET', '/api/v2/me/balance')
            if r.ok:
                saldo = r.json()
        except Exception as e:
            log.warning("ME balance: %s", e)
    return render_template('admin_melhorenvio.html',
                           configurado=me_configurado(),
                           ambiente=ME_AMBIENTE,
                           conectado=conectado,
                           saldo=saldo,
                           cfg=cfg)


@app.route('/api/admin/pedidos/<int:pid>/cotar', methods=['GET'])
@requer_admin
def admin_pedido_cotar(pid):
    ped = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not ped:
        return jsonify({'erro': 'pedido não encontrado'}), 404
    itens = db_execute(
        "SELECT * FROM pedido_itens WHERE pedido_id=%s", [pid], fetch='all') or []
    for it in itens:
        try:
            it['produto'] = buscar_produto(it['produto_pdv_id']) or {}
        except Exception:
            it['produto'] = {}
        it['qtd'] = it['quantidade']
        it['preco'] = it['preco_unitario']
    try:
        opcoes = me_cotar(ped.get('cep'), itens)
    except RuntimeError as e:
        return jsonify({'erro': str(e)}), 400
    return jsonify({'opcoes': opcoes})


def me_backfill_rastreio(ped):
    """Busca no ME o rastreio de um pedido que tem etiqueta mas nao tem codigo.

    Na emissao, a transportadora nem sempre ja atribuiu o codigo — o
    /shipment/tracking volta vazio e o campo fica nulo pra sempre, porque nada
    reconsultava depois. Resultado: cliente recebia "saiu pra entrega" sem
    rastreio mesmo com a etiqueta comprada. Devolve o codigo ou None.
    """
    eid = (ped or {}).get('melhorenvio_etiqueta_id')
    if not eid or (ped.get('melhorenvio_rastreio') or '').strip():
        return (ped or {}).get('melhorenvio_rastreio') or None
    try:
        r = me_request('POST', '/api/v2/me/shipment/tracking',
                       json_body={'orders': [eid]})
        if not r.ok:
            return None
        t = r.json()
        info = t.get(eid) if isinstance(t, dict) else None
        cod = (info or {}).get('tracking') if isinstance(info, dict) else None
        cod = (cod or '').strip() or None
        if cod:
            db_execute("UPDATE pedidos SET melhorenvio_rastreio=%s WHERE id=%s",
                       [cod, ped['id']])
            log.info("rastreio do pedido %s preenchido depois: %s",
                     ped['id'], cod)
        return cod
    except Exception as e:
        log.warning("me_backfill_rastreio pedido %s: %s",
                    (ped or {}).get('id'), e)
        return None


def _nfe_chave_do_pedido(ped):
    """Chave de 44 digitos da NF-e do pedido, ou None.

    Vale a pena consultar o PDV Pro: a chave so existe depois que a SEFAZ
    autoriza, e o pedido guarda a `ref`, nao a chave.
    """
    ref = (ped or {}).get('nfe_ref')
    if not ref or not PDVPRO_API_KEY:
        return None
    try:
        r = requests.get(PDVPRO_URL + f'/api/integracao/nfe/{ref}',
                         headers={'X-API-Key': PDVPRO_API_KEY}, timeout=10)
        if not r.ok:
            return None
        d = r.json() or {}
        if (d.get('status') or '') != 'autorizada':
            return None
        chave = ''.join(c for c in str(d.get('chave') or '') if c.isdigit())
        return chave if len(chave) == 44 else None
    except Exception as e:
        log.warning("_nfe_chave_do_pedido %s: %s", ref, e)
        return None


def me_volume_consolidado(vol):
    """Junta os itens do pedido num UNICO volume, que e como a loja despacha.

    A etiqueta declarava um volume por item (e, com a correcao de 23/07, um
    por unidade). So que Correios e Loggi RECUSAM envio com mais de um volume,
    e a Jadlog recusa nao-comercial saindo do PR — no pedido #35 pra Brasilia
    sobrava so a Azul Cargo, a R$ 78,98, contra R$ 55,11 que o cliente pagou.
    Consolidado, 5 das 8 transportadoras aceitam e o frete cai.

    Caixa = maior comprimento x maior largura, e altura suficiente pro volume
    total caber. Peso = soma real. Fica >= que a soma dos itens, entao nao
    subdeclara: transportadora que remede nao acha caixa maior que a da nota.
    """
    itens = [v for v in vol for _ in range(max(1, int(v.get('quantity') or 1)))]
    if not itens:
        return []
    if len(itens) == 1:
        v = itens[0]
        return [{'height': v['height'], 'width': v['width'],
                 'length': v['length'], 'weight': v['weight']}]
    peso = round(sum(float(v.get('weight') or 0) for v in itens), 3)
    comp = max(float(v.get('length') or 0) for v in itens)
    larg = max(float(v.get('width') or 0) for v in itens)
    alt_max = max(float(v.get('height') or 0) for v in itens)
    vol_total = sum(float(v.get('length') or 0) * float(v.get('width') or 0)
                    * float(v.get('height') or 0) for v in itens)
    base = comp * larg
    alt = max(alt_max, math.ceil(vol_total / base) if base > 0 else alt_max)
    return [{'height': round(alt, 1), 'width': round(larg, 1),
             'length': round(comp, 1), 'weight': peso or 0.3}]


def _admin_ou_api_key():
    """Permite admin logado OU X-API-Key do PDV Pro."""
    if admin_logado():
        return True
    return _verifica_api_key_pdv()


def _gerar_etiqueta_me(pid, service_id, servico_nome=''):
    """Cart → checkout → generate → print no Melhor Envio. Retorna
    (ok, dict_resultado_ou_erro). Reutilizado pela rota admin E pelo
    webhook Asaas (gera etiqueta automatica apos pagamento)."""
    if not service_id:
        return False, {'erro': 'service_id obrigatorio'}
    ped = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not ped:
        return False, {'erro': 'pedido nao encontrado'}
    if ped.get('melhorenvio_etiqueta_id'):
        return False, {'erro': 'pedido ja tem etiqueta gerada',
                       'etiqueta_id': ped.get('melhorenvio_etiqueta_id')}
    itens = db_execute(
        "SELECT * FROM pedido_itens WHERE pedido_id=%s", [pid], fetch='all') or []
    if not itens:
        return False, {'erro': 'pedido sem itens'}
    for it in itens:
        try:
            it['produto'] = buscar_produto(it['produto_pdv_id']) or {}
        except Exception:
            it['produto'] = {}
        it['qtd'] = it['quantidade']
        it['preco'] = it['preco_unitario']
    # Uma caixa so — e assim que a loja despacha, e Correios/Loggi recusam
    # envio multi-volume. Ver me_volume_consolidado().
    vol_resumo = me_volume_consolidado(me_volume_dos_itens(itens))
    produtos_carrinho = [
        {'name': (it.get('descricao') or 'Produto')[:80],
         'quantity': int(float(it.get('quantidade') or 1)),
         'unitary_value': float(it.get('preco_unitario') or 0)}
        for it in itens
    ]
    cep_destino = ''.join(c for c in (ped.get('cep') or '') if c.isdigit())
    body = {
        'service': service_id,
        'from':    me_remetente_dict(),
        'to': {
            'name':        ped.get('nome') or '',
            'phone':       ''.join(c for c in (ped.get('telefone') or '')
                                   if c.isdigit()),
            'email':       ped.get('email') or '',
            'document':    ''.join(c for c in (ped.get('cpf') or '')
                                   if c.isdigit()),
            'address':     ped.get('endereco') or '',
            'complement':  ped.get('complemento') or '',
            'number':      ped.get('numero') or '',
            'district':    ped.get('bairro') or '',
            'city':        ped.get('cidade') or '',
            'state_abbr':  ped.get('uf') or '',
            'country_id':  'BR',
            'postal_code': cep_destino,
        },
        'products': produtos_carrinho,
        'volumes':  vol_resumo,
        'options': {
            # Seguro = valor de VENDA da mercadoria (subtotal). Nao entra
            # frete nem juros de cartao: seguro cobre o que pode se perder no
            # transporte, nao o custo do transporte.
            'insurance_value': float(ped.get('subtotal') or 0),
            'receipt': False, 'own_hand': False,
            'reverse': False, 'non_commercial': False,
        },
    }
    # ── Nota fiscal no envio ─────────────────────────────────────────────
    # SEM a chave da NF-e o Melhor Envio trata o envio como "declaracao de
    # conteudo" (nao-comercial), mesmo com non_commercial=False. E ai:
    #   LATAM  -> "o aeroporto de destino nao aceita envios com declaracao de
    #             conteudo" (aereo exige nota)
    #   Jadlog -> "nao aceita envios nao-comerciais partindo deste estado"
    # Era a causa das duas recusas — nao havia restricao de transportadora
    # nenhuma. Emitindo pelo painel do ME funcionava justamente porque la a
    # chave da nota vai junto.
    chave_nf = _nfe_chave_do_pedido(ped)
    if chave_nf:
        body['options']['invoice'] = {'key': chave_nf}
        if ped.get('nfe_numero'):
            body['options']['invoice']['number'] = str(ped['nfe_numero'])
        body['invoice'] = body['options']['invoice']
    else:
        log.warning("pedido %s sem chave de NF-e — envio vai como declaracao "
                    "de conteudo e transportadora aerea pode recusar", pid)
    # ── Pre-checagem de saldo ────────────────────────────────────────────
    # O checkout so falha DEPOIS do cart criado, o que deixa um envio pendente
    # no painel do ME e devolvia "checkout ME falhou" sem dizer o motivo.
    # Cotando antes da pra recusar na hora dizendo exatamente quanto falta.
    try:
        preco_srv = None
        for o in (me_cotar(cep_destino, itens) or []):
            if str(o.get('id')) == str(service_id):
                preco_srv = float(o.get('valor') or 0)
                servico_nome = servico_nome or o.get('servico') or ''
                break
        saldo = me_saldo_atual()
        if preco_srv and saldo is not None and saldo < preco_srv:
            def _rs(v):
                return f'{v:.2f}'.replace('.', ',')
            return False, {'erro':
                f'Saldo insuficiente no Melhor Envio. A etiqueta '
                f'{servico_nome or ""} custa R$ {_rs(preco_srv)} e você tem '
                f'R$ {_rs(saldo)} — faltam R$ {_rs(preco_srv - saldo)}. '
                f'Recarregue em app.melhorenvio.com.br/melhor-carteira '
                f'e tente de novo.',
                'saldo': saldo, 'preco': preco_srv}
    except Exception as e:
        # Pre-checagem e conveniencia: se falhar, segue e deixa o ME decidir.
        log.warning("pre-checagem de saldo pedido %s: %s", pid, e)

    try:
        r = me_request('POST', '/api/v2/me/cart', json_body=body)
        if not r.ok:
            motivo = me_erro_texto(r)
            # Aprende: o cliente nao pode escolher de novo um frete que a
            # transportadora recusa. O cart e quem valida as regras dela —
            # a cotacao aceita tudo.
            me_registrar_bloqueio(service_id, cep_destino, motivo, servico_nome)
            return False, {'erro': 'Melhor Envio recusou o envio — ' + motivo,
                           'detalhe': r.text[:500]}
        cart = r.json()
        # Preco REAL que o ME vai debitar. A coluna melhorenvio_valor existia e
        # era lida no painel, mas ninguem gravava — entao nao dava pra comparar
        # o frete cobrado do cliente com o que a etiqueta custou, nem perceber
        # que uma etiqueta saiu mais cara que o cotado.
        try:
            preco_etiqueta = float(cart.get('price')
                                   or cart.get('custom_price') or 0) or None
        except (TypeError, ValueError):
            preco_etiqueta = None
        order_id = cart.get('id')
        if not order_id:
            return False, {'erro': 'Melhor Envio nao devolveu id do envio',
                           'detalhe': cart}
        r2 = me_request('POST', '/api/v2/me/shipment/checkout',
                        json_body={'orders': [order_id]})
        if not r2.ok:
            return False, {'erro': 'Pagamento da etiqueta recusado — '
                                   + me_erro_texto(r2),
                           'detalhe': r2.text[:500]}
        r3 = me_request('POST', '/api/v2/me/shipment/generate',
                        json_body={'orders': [order_id]})
        if not r3.ok:
            return False, {'erro': 'Etiqueta paga mas nao gerada — '
                                   + me_erro_texto(r3),
                           'detalhe': r3.text[:500]}
        url_pdf = None
        r4 = me_request('POST', '/api/v2/me/shipment/print',
                        json_body={'mode': 'private', 'orders': [order_id]})
        if r4.ok:
            try: url_pdf = r4.json().get('url')
            except Exception: pass
        rastreio = None
        try:
            r5 = me_request('POST', '/api/v2/me/shipment/tracking',
                            json_body={'orders': [order_id]})
            if r5.ok:
                t = r5.json()
                if isinstance(t, dict) and t.get(order_id):
                    rastreio = t[order_id].get('tracking')
        except Exception:
            pass
        db_execute("""UPDATE pedidos SET
                        melhorenvio_etiqueta_id  = %s,
                        melhorenvio_etiqueta_url = %s,
                        melhorenvio_rastreio     = %s,
                        melhorenvio_servico_id   = %s,
                        melhorenvio_servico_nome = %s,
                        melhorenvio_valor        = %s,
                        melhorenvio_pago_em      = NOW(),
                        atualizado_em            = NOW()
                       WHERE id=%s""",
                   [order_id, url_pdf, rastreio, str(service_id),
                    (servico_nome or '')[:80], preco_etiqueta, pid])
        if preco_etiqueta:
            log.info("etiqueta pedido %s: %s custou R$ %.2f (frete cobrado do "
                     "cliente: R$ %.2f)", pid, servico_nome or service_id,
                     preco_etiqueta, float(ped.get('frete') or 0))
        return True, {'ok': True, 'etiqueta_id': order_id,
                      'rastreio': rastreio, 'pdf_url': url_pdf}
    except Exception as e:
        log.exception("ME etiqueta")
        return False, {'erro': str(e)}


@app.route('/api/admin/pedidos/<int:pid>/etiqueta', methods=['POST'])
def admin_pedido_gerar_etiqueta(pid):
    """Rota admin — wrapper sobre _gerar_etiqueta_me."""
    if not _admin_ou_api_key():
        return jsonify({'erro': 'unauthorized'}), 401
    d = request.get_json() or {}
    ok, res = _gerar_etiqueta_me(pid, d.get('service_id'),
                                  servico_nome=d.get('servico_nome', ''))
    return jsonify(res), (200 if ok else 400)


# ─── Cliente API PDV Pro (cache 60s) ──────────────────────────────────────────
_PDV_CACHE = {}


def pdv_get(path, params=None, ttl=60):
    """Chama a API de integração do PDV Pro com cache em memória."""
    key = (path, tuple(sorted((params or {}).items())))
    now = time.time()
    cached = _PDV_CACHE.get(key)
    if cached and (now - cached['t']) < ttl:
        return cached['data']
    if not PDVPRO_API_KEY:
        return None
    try:
        r = requests.get(
            PDVPRO_URL + path,
            params=params or {},
            headers={'X-API-Key': PDVPRO_API_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            # Reinício do PDV Pro devolve 502 por uns 30s. Antes isso virava
            # None e a loja ficava SEM CATÁLOGO pra quem entrasse naquela
            # janela — vitrine vazia perde a venda inteira. Catálogo de um
            # minuto atrás é melhor que catálogo nenhum, então serve o cache
            # velho, como o caminho da exceção já fazia.
            if cached:
                log.warning("PDV Pro %s → %s; servindo cache de %.0fs atrás",
                            path, r.status_code, now - cached['t'])
                return cached['data']
            log.error("PDV Pro %s → %s (sem cache pra servir)", path, r.status_code)
            return None
        data = r.json()
        _PDV_CACHE[key] = {'t': now, 'data': data}
        return data
    except Exception as e:
        log.error("pdv_get %s: %s", path, e)
        if cached:
            return cached['data']  # serve stale em caso de erro
        return None


_FILTROS_VALIDOS = ('departamento', 'grupo', 'subgrupo', 'marca', 'faixa_etaria', 'destaque')


def slug_produto(descricao):
    """'BONECA BABY ALIVE COMIDINHA' -> 'boneca-baby-alive-comidinha'.

    Vai no fim da URL do produto. /produto/1234 nao dizia nada pro buscador;
    /produto/1234-boneca-baby-alive-comidinha carrega a palavra que a pessoa
    digita. O id continua na frente, entao a rota nao depende do texto — se a
    descricao mudar no PDV, a URL velha so redireciona pra nova.
    """
    txt = unicodedata.normalize('NFKD', (descricao or '').lower())
    txt = txt.encode('ascii', 'ignore').decode('ascii')
    txt = re.sub(r'[^a-z0-9]+', '-', txt).strip('-')
    return txt[:70].rstrip('-')


def url_produto(p):
    """URL canonica do produto. Usada nos links, no sitemap e no JSON-LD."""
    pid = p.get('id') or p.get('produto_id')
    s = slug_produto(p.get('descricao'))
    return f"/produto/{pid}-{s}" if s else f"/produto/{pid}"


app.jinja_env.globals['url_produto'] = url_produto


def listar_produtos(busca=None, categoria=None, limite=24, offset=0, ordem=None,
                    **filtros):
    """`categoria` ainda é aceito como alias de `departamento` pra compat.
    Filtros extras (departamento/grupo/subgrupo/marca/faixa_etaria) podem ser
    string única ou lista — viram CSV pro PDV."""
    p = {'limite': limite, 'offset': offset}
    if busca:
        p['busca'] = busca
    if ordem:
        p['ordem'] = ordem
    if categoria:
        p['categoria'] = categoria
    for k in _FILTROS_VALIDOS:
        v = filtros.get(k)
        if not v:
            continue
        if isinstance(v, (list, tuple, set)):
            v = ','.join(str(x) for x in v if x)
        if v:
            p[k] = v
    r = pdv_get('/api/integracao/produtos', p) or {}
    return r.get('produtos', []), r.get('total', 0)


def buscar_produto(produto_id):
    r = pdv_get(f'/api/integracao/produtos/{produto_id}') or {}
    return r.get('produto')


def listar_categorias():
    r = pdv_get('/api/integracao/categorias') or {}
    return r.get('categorias', [])


# ─── Vitrines da home (mais vendidos / mais visitados / mais procurados) ──────
_VITRINE_CACHE = {}


def _vitrine_cache(chave, ttl, fn):
    """Memoiza a vitrine por `ttl` segundos. Em erro devolve o valor velho
    (ou vazio) — home nunca cai por causa de vitrine."""
    agora = time.time()
    c = _VITRINE_CACHE.get(chave)
    if c and (agora - c['t']) < ttl:
        return c['v']
    try:
        v = fn() or []
    except Exception as e:
        log.error("vitrine %s: %s", chave, e)
        return (c or {}).get('v') or []
    _VITRINE_CACHE[chave] = {'t': agora, 'v': v}
    return v


def produtos_por_ids(ids, so_com_estoque=True):
    """Busca produtos no PDV em UM request (?ids=) preservando a ordem do
    ranking. Filtra pelos ids pedidos porque PDV antigo (sem suporte a
    ?ids=) devolveria o catálogo inteiro."""
    ids = [int(i) for i in ids if i][:24]
    if not ids:
        return []
    r = pdv_get('/api/integracao/produtos',
                {'ids': ','.join(str(i) for i in ids), 'limite': len(ids)},
                ttl=300) or {}
    achados = {}
    for p in r.get('produtos', []):
        if p.get('id') in set(ids):
            if so_com_estoque and float(p.get('estoque_atual') or 0) <= 0:
                continue
            achados[p['id']] = p
    return [achados[i] for i in ids if i in achados]


def produtos_mais_vendidos(limite=8, dias=90):
    """Ranking REAL de vendas dos últimos `dias`, nesta ordem de fonte:
    1) vendas da loja no PDV (loja física + PDV vendem muito mais que o
       site, então é o ranking mais honesto),
    2) pedidos do próprio site,
    3) a flag "mais vendido" que o lojista marca no PDV.
    Cada fonte só completa o que faltou pra fechar `limite`."""
    def _calc():
        prods = []
        rs_pdv = pdv_get('/api/integracao/mais-vendidos',
                         {'dias': dias, 'limite': limite * 2}, ttl=600) or {}
        ids_pdv = [p['id'] for p in rs_pdv.get('produtos', []) if p.get('id')]
        if ids_pdv:
            prods = produtos_por_ids(ids_pdv)[:limite]
        if len(prods) >= limite:
            return prods
        vistos = {p['id'] for p in prods}
        for p in _mais_vendidos_do_site(limite, dias):
            if p['id'] not in vistos:
                vistos.add(p['id'])
                prods.append(p)
                if len(prods) >= limite:
                    return prods
        extra, _ = listar_produtos(limite=limite * 2, destaque='mais_vendido')
        for p in extra:
            if p['id'] in vistos or float(p.get('estoque_atual') or 0) <= 0:
                continue
            prods.append(p)
            if len(prods) >= limite:
                break
        return prods
    return _vitrine_cache(f'vendidos:{limite}:{dias}', 600, _calc)


def _mais_vendidos_do_site(limite, dias):
    """Ranking pelos pedidos feitos no próprio site."""
    def _calc():
        rows = db_execute("""
            SELECT pi.produto_pdv_id AS pid, SUM(pi.quantidade) AS qtd
              FROM pedido_itens pi
              JOIN pedidos p ON p.id = pi.pedido_id
             WHERE pi.produto_pdv_id IS NOT NULL
               AND p.status NOT IN ('aguardando_pagto', 'cancelado')
               AND p.criado_em >= NOW() - %s::interval
             GROUP BY pi.produto_pdv_id
             ORDER BY qtd DESC
             LIMIT %s""", [f"{dias} days", limite * 3], fetch='all') or []
        return produtos_por_ids([r['pid'] for r in rows])[:limite]
    return _vitrine_cache(f'vendidos-site:{limite}:{dias}', 600, _calc)


def produtos_mais_visitados(limite=8, dias=30):
    """Ranking pelos pageviews de /produto/<id> (site_visitas), contando
    visitantes únicos pra uma pessoa só não inflar o card."""
    def _calc():
        rows = db_execute("""
            SELECT substring(path from '^/produto/([0-9]+)')::int AS pid,
                   COUNT(DISTINCT ip_hash) AS unicos,
                   COUNT(*) AS views
              FROM site_visitas
             WHERE NOT is_bot
               AND ts >= NOW() - %s::interval
               AND path ~ '^/produto/[0-9]+'
             GROUP BY pid
             ORDER BY unicos DESC, views DESC
             LIMIT %s""", [f"{dias} days", limite * 3], fetch='all') or []
        return produtos_por_ids([r['pid'] for r in rows])[:limite]
    return _vitrine_cache(f'visitados:{limite}:{dias}', 600, _calc)


def produtos_mais_procurados(limite=8, dias=30):
    """Vitrine que gira o estoque em cima do que as pessoas PROCURAM:
    pega os termos mais buscados (site + Luquizinha), busca produtos com
    estoque de cada um e rotaciona a ordem a cada 15 min pra a home não
    ficar sempre igual e mais produtos pegarem vitrine."""
    termos = db_execute("""
        SELECT termo_norm, COUNT(*) AS n
          FROM site_buscas
         WHERE ts >= NOW() - %s::interval
           AND resultados > 0
         GROUP BY termo_norm
         ORDER BY n DESC, MAX(ts) DESC
         LIMIT 15""", [f"{dias} days"], fetch='all') or []
    termos = [t['termo_norm'] for t in termos]
    if not termos:
        return []
    # rotação: janela de 15 min desloca quais termos abrem a vitrine
    giro = int(time.time() // 900) % len(termos)
    termos = termos[giro:] + termos[:giro]

    def _calc():
        prods, vistos = [], set()
        for termo in termos[:6]:
            achados, _ = listar_produtos(busca=termo, limite=6)
            for p in achados:
                if p['id'] in vistos or float(p.get('estoque_atual') or 0) <= 0:
                    continue
                vistos.add(p['id'])
                p = dict(p)
                p['termo_busca'] = termo
                prods.append(p)
                break  # 1 produto por termo primeiro, pra variar a vitrine
            if len(prods) >= limite:
                break
        if len(prods) < limite:
            for termo in termos[:6]:
                achados, _ = listar_produtos(busca=termo, limite=6)
                for p in achados:
                    if p['id'] in vistos or float(p.get('estoque_atual') or 0) <= 0:
                        continue
                    vistos.add(p['id'])
                    p = dict(p)
                    p['termo_busca'] = termo
                    prods.append(p)
                    if len(prods) >= limite:
                        break
                if len(prods) >= limite:
                    break
        return prods[:limite]
    return _vitrine_cache(f'procurados:{limite}:{giro}', 900, _calc)


def _dedupe_por_slug(items):
    """Junta items com mesmo slug somando qtd. Usado pra remover
    duplicacao quando um grupo/subgrupo aparece em mais de um departamento
    (ex: o endpoint do PDV agrupa por (grupo, depto), entao "FAZENDA E
    ANIMAIS" pode vir 2x se tiver em 2 deptos). Mantem o primeiro 'pai'."""
    if not items:
        return []
    acc = {}
    ordem = []
    for it in items:
        s = it.get('slug') or ''
        if s not in acc:
            acc[s] = dict(it)
            ordem.append(s)
        else:
            acc[s]['qtd'] = (acc[s].get('qtd') or 0) + (it.get('qtd') or 0)
    return [acc[s] for s in ordem]


def _verifica_api_key_pdv():
    """Valida X-API-Key vindo do PDV Pro pra rotas de integracao reversa."""
    recv = (request.headers.get('X-API-Key') or '').strip()
    return recv and PDVPRO_API_KEY and recv == PDVPRO_API_KEY


@app.route('/api/integracao/pedidos-site/fila')
def integracao_pedidos_fila():
    """Contador da fila pra alerta ativo do PDV Pro.
    Retorna: pendentes (pago e ainda nao aceito), em_separacao (aceito e
    nao pronto), prontos_retira (pronto e frete_servico=Retirar). Polling
    cada 15-20s desde o PDV Pro pra mostrar badge pulsante."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    r = db_execute("""
        SELECT
          COUNT(*) FILTER (WHERE status='pago' AND aceito_em IS NULL) AS pendentes,
          COUNT(*) FILTER (WHERE status='pago' AND aceito_em IS NOT NULL AND pronto_em IS NULL) AS em_separacao,
          COUNT(*) FILTER (WHERE status='pago' AND pronto_em IS NOT NULL AND frete_servico ILIKE 'Retirar%%') AS prontos_retira,
          MAX(criado_em) FILTER (WHERE status='pago' AND aceito_em IS NULL) AS ultimo_pago_em
        FROM pedidos
    """, fetch='one') or {}
    ultimo = r.get('ultimo_pago_em')
    return jsonify({
        'pendentes': int(r.get('pendentes') or 0),
        'em_separacao': int(r.get('em_separacao') or 0),
        'prontos_retira': int(r.get('prontos_retira') or 0),
        'ultimo_pago_em': ultimo.isoformat() if ultimo else None,
    })


@app.route('/api/integracao/pedidos-site/<int:pid>/aceitar', methods=['POST'])
def integracao_pedido_aceitar(pid):
    """Marca pedido como aceito por um operador da caixa. Tira da fila de
    alerta dos outros caixas. Idempotente — se já aceito, retorna o que
    está no banco."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    d = request.get_json() or {}
    quem = (d.get('operador') or 'caixa').strip()[:80]
    r = db_execute("""
        UPDATE pedidos
        SET aceito_em = COALESCE(aceito_em, NOW()),
            aceito_por = COALESCE(aceito_por, %s)
        WHERE id=%s AND status='pago'
        RETURNING id, aceito_em, aceito_por
    """, [quem, pid], fetch='one')
    if not r:
        return jsonify({'erro': 'pedido não encontrado ou não está pago'}), 404
    return jsonify({'ok': True, 'aceito_em': r['aceito_em'].isoformat() if r['aceito_em'] else None,
                    'aceito_por': r['aceito_por']})


@app.route('/api/integracao/pedidos-site/<int:pid>/mudar-status', methods=['POST'])
def integracao_pedido_mudar_status(pid):
    """Atualiza status do pedido a partir do PDV Pro (sem precisar abrir o
    admin do site). Aceita: preparando, pronto_retirada, enviado, entregue.
    Dispara WhatsApp pro cliente com a mensagem adequada."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    d = request.get_json() or {}
    novo = (d.get('status') or '').strip()
    rastreio = (d.get('rastreio') or '').strip() or None
    permitidos = {'preparando', 'pronto_retirada', 'enviado', 'entregue'}
    if novo not in permitidos:
        return jsonify({'erro': f'status inválido (use {sorted(permitidos)})'}), 400
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'pedido não encontrado'}), 404
    db_execute("""UPDATE pedidos SET status=%s,
                  melhorenvio_rastreio=COALESCE(%s, melhorenvio_rastreio),
                  pronto_em = CASE WHEN %s='pronto_retirada'
                                   THEN COALESCE(pronto_em, NOW()) ELSE pronto_em END,
                  atualizado_em=NOW() WHERE id=%s""",
               [novo, rastreio, novo, pid])
    # Sem rastreio digitado, usa o que a etiqueta ja gravou. Quando a emissao
    # e automatica (webhook do pagamento) o codigo esta no banco desde entao —
    # e a mensagem de "saiu pra entrega" saia sem rastreio porque so olhava o
    # que veio no request.
    rastreio = rastreio or (p.get('melhorenvio_rastreio') or '').strip() or None
    if not rastreio and novo == 'enviado':
        rastreio = me_backfill_rastreio(p)
    try:
        primeiro = (p.get('nome') or 'amigo(a)').split()[0]
        msgs = {
            'preparando': (f"📦 Oi {primeiro}! Seu *Pedido #{pid}* está sendo "
                           f"preparado com muito carinho 💛"),
            'pronto_retirada': (
                f"🏪 Oi {primeiro}! Seu *Pedido #{pid}* já está *pronto pra retirar* "
                f"na Luqui Brinquedos! 💛\n\n"
                f"📍 R. Eng. Rebouças, 2053 — Cascavel/PR\n"
                f"🕐 Seg a Sex: 8h-18h · Sáb: 9h-13h\n\n"
                f"Leva um documento com foto. Te esperamos! 🧸"),
            'enviado': (f"🚚 Oi {primeiro}! Seu *Pedido #{pid}* "
                        f"acabou de sair pra entrega!"
                        + (f"\n\n*Rastreio:* {rastreio}" if rastreio else "")
                        + f"\n\nAcompanhe: https://www.luquibrinquedos.com.br/pedido/{pid}/tracking?t={p.get('token','')}"),
            'entregue': (f"💛 *Pedido #{pid} entregue!* Esperamos que ame!\n\n"
                         f"Que tal nos avaliar? "
                         f"https://www.luquibrinquedos.com.br/pedido/{pid}/tracking?t={p.get('token','')}"),
        }
        if p.get('telefone') and novo in msgs:
            enviar_whatsapp(p['telefone'], msgs[novo])
    except Exception as e:
        log.warning(f"WA mudar-status pedido {pid}: {e}")
    return jsonify({'ok': True, 'status': novo})


@app.route('/api/integracao/pedidos-site/<int:pid>/marcar-pronto', methods=['POST'])
def integracao_pedido_marcar_pronto(pid):
    """Marca pedido como pronto pra retirar/postar. Dispara WhatsApp pro
    cliente avisando."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    r = db_execute("""
        UPDATE pedidos
        SET pronto_em = COALESCE(pronto_em, NOW()),
            aceito_em = COALESCE(aceito_em, NOW())
        WHERE id=%s AND status='pago'
        RETURNING id, telefone, nome, frete_servico
    """, [pid], fetch='one')
    if not r:
        return jsonify({'erro': 'pedido não encontrado ou não está pago'}), 404
    # WhatsApp pro cliente avisando
    try:
        is_retira = (r.get('frete_servico') or '').lower().startswith('retirar')
        if is_retira:
            msg = (f"Oi {(r['nome'] or '').split()[0]}! 💛 Seu pedido #{pid} "
                   f"ja esta pronto pra retirar na Luqui Brinquedos!\n\n"
                   f"📍 R. Eng. Reboucas, 2053 - Cascavel/PR\n"
                   f"🕐 Seg a Sex: 8h-18h · Sab: 9h-13h\n\n"
                   f"Leva um documento com foto. Te esperamos! 🧸")
        else:
            msg = (f"Oi {(r['nome'] or '').split()[0]}! 💛 Seu pedido #{pid} "
                   f"esta separado e indo pro Correios. Em breve te mando o "
                   f"codigo de rastreio! 📦")
        if r.get('telefone'):
            enviar_whatsapp(r['telefone'], msg)
    except Exception as e:
        log.warning(f"whatsapp pronto pedido {pid}: {e}")
    return jsonify({'ok': True})


@app.route('/api/integracao/pedidos-site')
def integracao_pedidos_site():
    """Lista pedidos do site pro PDV Pro consumir (tela centralizada).
    Filtros: status (pago,aguardando_pagto,enviado,entregue,cancelado),
    limit (default 50). Autenticado por X-API-Key = PDVPRO_API_KEY."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    status = (request.args.get('status') or '').strip()
    try:
        limit = max(1, min(200, int(request.args.get('limit') or 50)))
    except ValueError:
        limit = 50
    where = []
    params = []
    if status:
        where.append('status=%s')
        params.append(status)
    sql_where = ('WHERE ' + ' AND '.join(where)) if where else ''
    rows = db_execute(f"""
        SELECT id, nome, email, telefone, cpf, cidade, uf, total, frete,
               forma_pagto, status, criado_em, pago_em, pdv_venda_id,
               nfe_ref, nfe_numero,
               melhorenvio_etiqueta_url, melhorenvio_servico_nome,
               melhorenvio_servico_id, melhorenvio_valor,
               cep, endereco, numero, bairro, complemento,
               entrega_agendada, embrulho_presente, embrulho_tipo,
               embrulho_mensagem,
               aceito_em, aceito_por, pronto_em, frete_servico
        FROM pedidos {sql_where}
        ORDER BY criado_em DESC LIMIT %s""",
        params + [limit], fetch='all') or []
    out = []
    for r in rows:
        d = dict(r)
        for k, v in list(d.items()):
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
            elif hasattr(v, '__float__') and not isinstance(v, (bool, int)):
                d[k] = float(v)
        out.append(d)
    return jsonify({'pedidos': out, 'total': len(out)})


@app.route('/api/integracao/pedidos-site/<int:pid>/itens')
def integracao_pedido_itens(pid):
    """Itens do pedido pra PDV listar/cotar ME."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    itens = db_execute("""SELECT * FROM pedido_itens WHERE pedido_id=%s
                          ORDER BY id""", [pid], fetch='all') or []
    out = []
    for r in itens:
        d = dict(r)
        for k, v in list(d.items()):
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
            elif hasattr(v, '__float__') and not isinstance(v, (bool, int)):
                d[k] = float(v)
        out.append(d)
    return jsonify({'itens': out})


@app.route('/api/integracao/pedidos-site/<int:pid>/cotar-me')
def integracao_cotar_me(pid):
    """Devolve opcoes de frete ME pra esse pedido. Usado pelo PDV Pro."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'pedido nao encontrado'}), 404
    itens_db = db_execute("SELECT * FROM pedido_itens WHERE pedido_id=%s",
                          [pid], fetch='all') or []
    itens_full = []
    for it in itens_db:
        try:
            prod = buscar_produto(it.get('produto_pdv_id')) or {}
        except Exception:
            prod = {}
        itens_full.append({
            'produto': prod, 'qtd': float(it.get('quantidade') or 1),
            'preco': float(it.get('preco_unitario') or 0)})
    try:
        ops = me_cotar(p.get('cep') or '', itens_full)
        return jsonify({'opcoes': ops})
    except Exception as e:
        return jsonify({'erro': str(e)}), 502


def _gerar_etiqueta_avulsa(service_id, cep_destino, destinatario, itens_full,
                            valor_seguro=0.0, servico_nome=''):
    """Cart→checkout→generate→print no Melhor Envio pra destinatario
    avulso (sem vinculo com pedido do site). Salva em etiquetas_avulsas.
    Retorna (ok, dict)."""
    if not service_id:
        return False, {'erro': 'service_id obrigatorio'}
    if not itens_full:
        return False, {'erro': 'sem itens'}
    cep_destino = ''.join(c for c in (cep_destino or '') if c.isdigit())
    if len(cep_destino) != 8:
        return False, {'erro': 'cep_destino invalido'}
    obrig = ['nome', 'doc', 'endereco', 'numero', 'bairro', 'cidade', 'uf']
    faltam = [k for k in obrig if not (destinatario or {}).get(k)]
    if faltam:
        return False, {'erro': 'destinatario incompleto: ' + ', '.join(faltam)}

    vol = me_volume_dos_itens(itens_full)
    vol_resumo = [{'height': v['height'], 'width': v['width'],
                   'length': v['length'], 'weight': v['weight']} for v in vol]
    produtos_carrinho = [
        {'name': ((it.get('produto') or {}).get('descricao')
                  or it.get('descricao') or 'Produto')[:80],
         'quantity': int(float(it.get('qtd') or 1)),
         'unitary_value': float(it.get('preco') or 0)}
        for it in itens_full
    ]
    body = {
        'service': service_id,
        'from':    me_remetente_dict(),
        'to': {
            'name':        destinatario.get('nome') or '',
            'phone':       ''.join(c for c in (destinatario.get('fone') or '')
                                   if c.isdigit()),
            'email':       destinatario.get('email') or '',
            'document':    ''.join(c for c in (destinatario.get('doc') or '')
                                   if c.isdigit()),
            'address':     destinatario.get('endereco') or '',
            'complement':  destinatario.get('complemento') or '',
            'number':      destinatario.get('numero') or '',
            'district':    destinatario.get('bairro') or '',
            'city':        destinatario.get('cidade') or '',
            'state_abbr':  destinatario.get('uf') or '',
            'country_id':  'BR',
            'postal_code': cep_destino,
        },
        'products': produtos_carrinho,
        'volumes':  vol_resumo,
        'options': {
            'insurance_value': float(valor_seguro or 0),
            'receipt': False, 'own_hand': False,
            'reverse': False, 'non_commercial': False,
        },
    }
    try:
        r = me_request('POST', '/api/v2/me/cart', json_body=body)
        if not r.ok:
            return False, {'erro': 'cart falhou', 'detalhe': r.text[:500]}
        order_id = (r.json() or {}).get('id')
        if not order_id:
            return False, {'erro': 'sem id do envio'}
        r2 = me_request('POST', '/api/v2/me/shipment/checkout',
                        json_body={'orders': [order_id]})
        if not r2.ok:
            return False, {'erro': 'checkout ME falhou — confira saldo',
                           'detalhe': r2.text[:500]}
        r3 = me_request('POST', '/api/v2/me/shipment/generate',
                        json_body={'orders': [order_id]})
        if not r3.ok:
            return False, {'erro': 'generate falhou', 'detalhe': r3.text[:500]}
        url_pdf = None
        r4 = me_request('POST', '/api/v2/me/shipment/print',
                        json_body={'mode': 'private', 'orders': [order_id]})
        if r4.ok:
            try: url_pdf = (r4.json() or {}).get('url')
            except Exception: pass
        rastreio = None
        try:
            r5 = me_request('POST', '/api/v2/me/shipment/tracking',
                            json_body={'orders': [order_id]})
            if r5.ok:
                t = r5.json()
                if isinstance(t, dict) and t.get(order_id):
                    rastreio = t[order_id].get('tracking')
        except Exception:
            pass
        try:
            db_execute("""INSERT INTO etiquetas_avulsas
                (origem, cep_destino, destinatario_nome, destinatario_doc,
                 destinatario_fone, destinatario_email,
                 endereco, numero, complemento, bairro, cidade, uf,
                 servico_id, servico_nome, valor_frete, valor_seguro,
                 itens_json, me_etiqueta_id, me_etiqueta_url, me_rastreio)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s)""",
                [
                    'pdv-calc', cep_destino,
                    (destinatario.get('nome') or '')[:120],
                    ''.join(c for c in (destinatario.get('doc') or '') if c.isdigit())[:20],
                    (destinatario.get('fone') or '')[:30],
                    (destinatario.get('email') or '')[:120],
                    (destinatario.get('endereco') or '')[:200],
                    (destinatario.get('numero') or '')[:20],
                    (destinatario.get('complemento') or '')[:100],
                    (destinatario.get('bairro') or '')[:100],
                    (destinatario.get('cidade') or '')[:100],
                    (destinatario.get('uf') or '')[:2],
                    str(service_id)[:20],
                    (servico_nome or '')[:80],
                    float(destinatario.get('valor_frete') or 0),
                    float(valor_seguro or 0),
                    json.dumps([{'nome': p['name'], 'qtd': p['quantity'],
                                 'valor': p['unitary_value']} for p in produtos_carrinho])[:8000],
                    str(order_id), url_pdf, rastreio,
                ])
        except Exception as e:
            log.warning("etiquetas_avulsas insert falhou: %s", e)
        return True, {'ok': True, 'etiqueta_id': order_id,
                      'rastreio': rastreio, 'pdf_url': url_pdf}
    except Exception as e:
        log.exception("ME etiqueta avulsa")
        return False, {'erro': str(e)}


@app.route('/api/integracao/emitir-etiqueta-avulsa', methods=['POST'])
def integracao_emitir_etiqueta_avulsa():
    """Emite etiqueta ME a partir da calculadora do PDV Pro (sem pedido)."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    d = request.get_json(silent=True) or {}
    cep = d.get('cep_destino') or ''
    sid = d.get('service_id')
    dest = d.get('destinatario') or {}
    itens_in = d.get('itens') or []
    # Reaproveita lógica de calcular-frete pra resolver dimensões a partir
    # de produto_id, OU usar dimensões diretas se vierem
    itens_full = []
    for it in itens_in:
        pid = it.get('produto_id')
        prod = {}
        if pid:
            try:
                prod = buscar_produto(pid) or {}
            except Exception:
                prod = {}
        for k_in, k_out in (('peso_kg', 'peso_bruto'),
                            ('largura_cm', 'largura_cm'),
                            ('altura_cm', 'altura_cm'),
                            ('comprimento_cm', 'comprimento_cm')):
            if it.get(k_in) not in (None, '', 0, '0'):
                prod[k_out] = it[k_in]
        # Descrição: vinda do PDV (it.descricao) ou do produto (prod.descricao)
        if it.get('descricao') and not prod.get('descricao'):
            prod['descricao'] = it.get('descricao')
        itens_full.append({
            'produto': prod,
            'qtd':   float(it.get('qtd') or 1),
            'preco': float(it.get('preco') or prod.get('preco') or 0),
            'descricao': prod.get('descricao') or it.get('descricao') or '',
        })
    valor_seguro = sum((it['preco'] or 0) * (it['qtd'] or 1) for it in itens_full)
    ok, res = _gerar_etiqueta_avulsa(
        sid, cep, dest, itens_full,
        valor_seguro=valor_seguro,
        servico_nome=d.get('servico_nome') or '',
    )
    return jsonify(res), (200 if ok else 400)


@app.route('/api/integracao/calcular-frete', methods=['POST'])
def integracao_calcular_frete():
    """Cotação avulsa Melhor Envio pra calculadora do PDV Pro.
    Body: { cep_destino, itens:[{produto_id, qtd, preco?}, ...] }
    Itens podem alternativamente trazer dimensões diretas
    (peso_kg/largura_cm/altura_cm/comprimento_cm) pra cota sem produto
    cadastrado. Retorna {opcoes:[{id, servico, valor, prazo, company}]}."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    d = request.get_json(silent=True) or {}
    cep = ''.join(c for c in (d.get('cep_destino') or '') if c.isdigit())
    if len(cep) != 8:
        return jsonify({'erro': 'cep_destino invalido'}), 400
    itens_in = d.get('itens') or []
    if not itens_in:
        return jsonify({'erro': 'itens vazio'}), 400
    itens_full = []
    for it in itens_in:
        pid = it.get('produto_id')
        prod = {}
        if pid:
            try:
                prod = buscar_produto(pid) or {}
            except Exception:
                prod = {}
        # Override: se body trouxe dimensões/peso direto, usa esses.
        for k_in, k_out in (('peso_kg', 'peso_bruto'),
                            ('largura_cm', 'largura_cm'),
                            ('altura_cm', 'altura_cm'),
                            ('comprimento_cm', 'comprimento_cm')):
            if it.get(k_in) not in (None, '', 0, '0'):
                prod[k_out] = it[k_in]
        itens_full.append({
            'produto': prod,
            'qtd':   float(it.get('qtd') or 1),
            'preco': float(it.get('preco') or prod.get('preco') or 0),
        })
    try:
        ops = me_cotar(cep, itens_full)
        return jsonify({'opcoes': ops, 'cep_destino': cep})
    except Exception as e:
        return jsonify({'erro': str(e)}), 502


@app.route('/api/integracao/me-saldo')
def integracao_me_saldo():
    """Saldo do Melhor Envio pra calculadora do PDV Pro mostrar quanto
    tem em conta antes de emitir etiqueta. Tenta /me/balance primeiro
    (escopo users-read) e cai pra /me (devolve balance também) se 403.

    Sempre retorna HTTP 200 — Cloudflare substitui body 5xx pela própria
    página de erro, então a gente sinaliza falha via campo `erro` no JSON."""
    if not _verifica_api_key_pdv():
        return jsonify({'erro': 'unauthorized'}), 401
    try:
        r = me_request('GET', '/api/v2/me/balance')
        if r.status_code == 403:
            # Token sem escopo users-read → tenta /me que costuma vir junto
            r2 = me_request('GET', '/api/v2/me')
            if r2.ok:
                d = r2.json() or {}
                saldo = float(d.get('balance') or 0)
                return jsonify({'saldo': saldo, 'moeda': 'BRL',
                                'origem': '/me'})
            log.warning("ME saldo /me/balance 403 e /me %s: %s",
                        r2.status_code, (r2.text or '')[:200])
            return jsonify({'erro': 'Token Melhor Envio sem permissão pra ler '
                                    'saldo. Reconecte em /admin/melhorenvio.'})
        if not r.ok:
            log.warning("ME saldo /me/balance %s: %s", r.status_code,
                        (r.text or '')[:200])
            return jsonify({'erro': f'ME respondeu {r.status_code}'})
        d = r.json() or {}
        return jsonify({'saldo': float(d.get('balance') or 0),
                        'moeda': d.get('currency') or 'BRL'})
    except Exception as e:
        log.exception("integracao_me_saldo: %s", e)
        return jsonify({'erro': str(e)})


def pdv_buscar_cliente_cpf(cpf):
    """Busca cliente completo no PDV pelo CPF — endereço, contato, pontos.
    Usado no checkout pra oferecer pré-preenchimento quando cliente já
    é da loja física. Retorna None se não achou."""
    cpf = ''.join(c for c in (cpf or '') if c.isdigit())
    if not cpf or len(cpf) != 11 or not PDVPRO_API_KEY:
        return None
    try:
        r = requests.get(PDVPRO_URL + '/api/integracao/cliente/buscar-por-cpf',
                         params={'cpf': cpf},
                         headers={'X-API-Key': PDVPRO_API_KEY}, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
        return d if d.get('cliente_existe') else None
    except Exception as e:
        log.error("pdv_buscar_cliente_cpf %s", e)
        return None


def pdv_consultar_pontos(cpf):
    """Consulta saldo de pontos no PDV pelo CPF. Sem cache pq muda
    frequentemente. Retorna dict {saldo, valor_disponivel, ...} ou None."""
    cpf = ''.join(c for c in (cpf or '') if c.isdigit())
    if not cpf or not PDVPRO_API_KEY:
        return None
    try:
        r = requests.get(PDVPRO_URL + '/api/integracao/cliente/saldo-pontos',
                         params={'cpf': cpf},
                         headers={'X-API-Key': PDVPRO_API_KEY}, timeout=8)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        log.error("pdv_consultar_pontos %s", e)
        return None


def pdv_resgatar_pontos(cpf, pontos, pedido_id):
    """Debita pontos no PDV. Retorna (ok, msg)."""
    cpf = ''.join(c for c in (cpf or '') if c.isdigit())
    if not cpf or pontos <= 0 or not PDVPRO_API_KEY:
        return False, 'cpf/pontos invalidos'
    try:
        r = requests.post(PDVPRO_URL + '/api/integracao/cliente/resgatar-pontos',
                          json={'cpf': cpf, 'pontos': float(pontos),
                                'pedido_externo_ref': f'site-pedido-{pedido_id}'},
                          headers={'X-API-Key': PDVPRO_API_KEY}, timeout=10)
        if r.status_code == 200:
            return True, 'ok'
        try:
            return False, (r.json().get('erro') or f'HTTP {r.status_code}')
        except Exception:
            return False, f'HTTP {r.status_code}'
    except Exception as e:
        log.error("pdv_resgatar_pontos %s", e)
        return False, str(e)


def listar_filtros():
    """Hierarquia departamento > grupo > subgrupo + marca + faixa etária.
    Retorna RAW com pai_slug — categoria.html usa pra filtrar grupos do
    departamento atual. Pra paginas "planas" (busca/produtos/destaques),
    use listar_filtros_planos() pra remover duplicatas."""
    r = pdv_get('/api/integracao/filtros') or {}
    return {
        'departamentos':  r.get('departamentos', []),
        'grupos':         r.get('grupos', []),
        'subgrupos':      r.get('subgrupos', []),
        'marcas':         r.get('marcas', []),
        'faixas_etarias': r.get('faixas_etarias', []),
    }


def _ordenar_faixas_etarias(faixas):
    """Ordena faixas pelo PRIMEIRO numero do nome E marca cada uma como
    bebe ('em_meses=True') ou crianca, pro template separar em 2 sub-grupos.
    Ex: '2+ anos' (2) vem antes de '10+ anos' (10)."""
    import re
    out = []
    for f in faixas:
        nome_low = (f.get('nome') or '').lower()
        f2 = dict(f)
        f2['em_meses'] = ('mes' in nome_low or 'mês' in nome_low)
        out.append(f2)
    def _chave(f):
        m = re.search(r'\d+', f.get('nome') or '')
        return int(m.group()) if m else 9999
    return sorted(out, key=_chave)


def listar_filtros_planos():
    """Versao dedupada pra paginas sem hierarquia (busca, /produtos,
    /novidades, /mais-vendidos, /liquida-luqui). Junta grupos com mesmo
    slug somando qtd."""
    f = listar_filtros()
    return {
        'departamentos':  f['departamentos'],
        'grupos':         _dedupe_por_slug(f['grupos']),
        'subgrupos':      _dedupe_por_slug(f['subgrupos']),
        'marcas':         f['marcas'],
        'faixas_etarias': _ordenar_faixas_etarias(f['faixas_etarias']),
    }


def filtros_da_querystring(req):
    """Lê ?departamento=&grupo=&subgrupo=&marca=&faixa_etaria= (multi-valor)
    do request e devolve dict pronto pra `listar_produtos(**dict)`."""
    out = {}
    for k in _FILTROS_VALIDOS:
        vs = [v for v in req.args.getlist(k) if v]
        if vs:
            out[k] = vs
    return out


# ─── Carrinho na sessão ───────────────────────────────────────────────────────
def carrinho_ler():
    return session.get('carrinho') or []


def carrinho_salvar(itens):
    session['carrinho'] = itens
    session.modified = True


def carrinho_total(itens):
    sub = sum(float(it['preco']) * float(it['qtd']) for it in itens)
    qtd = sum(float(it['qtd']) for it in itens)
    return sub, qtd


# ─── Rotas públicas ───────────────────────────────────────────────────────────
PAGINAS_LEGAIS = {
    'trocas-devolucoes': {
        'titulo': 'Trocas e devoluções',
        'conteudo': """
<h3>Direito de arrependimento (7 dias)</h3>
<p>Conforme o <b>Código de Defesa do Consumidor (CDC, art. 49)</b>, você tem até
<b>7 dias corridos</b> a partir do recebimento pra desistir da compra e pedir
o reembolso integral, sem necessidade de justificativa.</p>

<h3>Produto com defeito</h3>
<p>Se o brinquedo chegou quebrado ou com algum problema de fabricação, você tem
<b>30 dias</b> pra entrar em contato. Vamos trocar ou devolver o valor pago
(o que você preferir).</p>

<h3>Como fazer a troca</h3>
<ol>
  <li>Manda mensagem no WhatsApp <b>(45) 99111-9800</b> em até 7 dias do recebimento</li>
  <li>Envie o produto pelos Correios (nós cobrimos o frete da troca/devolução em caso de defeito)</li>
  <li>Após receber e conferir, processamos a troca ou estorno em até 7 dias úteis</li>
</ol>

<h3>O que NÃO podemos trocar</h3>
<ul>
  <li>Produtos lacrados que foram abertos (por higiene), exceto se houver defeito</li>
  <li>Brinquedos com uso evidente (sujos, riscados, sem embalagem)</li>
  <li>Itens personalizados sob encomenda</li>
</ul>

<h3>Reembolso</h3>
<p>O estorno é feito pela mesma forma de pagamento original:</p>
<ul>
  <li><b>PIX/Boleto:</b> em até 7 dias úteis após recebermos o produto</li>
  <li><b>Cartão de crédito:</b> em até 2 faturas dependendo da operadora</li>
</ul>
""",
    },
    'entregas': {
        'titulo': 'Entregas e prazos',
        'conteudo': """
<h3>Entrega local Cascavel</h3>
<p>Cascavel/PR: <b>R$ 10 fixo</b>, entrega em 1 a 2 dias úteis. Toledo/PR e demais cidades: frete calculado no checkout pelo Melhor Envio.</p>

<h3>Demais regiões do Paraná</h3>
<ul>
  <li><b>PAC</b>: R$ 24,90 · 3 a 5 dias úteis</li>
  <li><b>SEDEX</b>: R$ 38,90 · 2 a 3 dias úteis</li>
</ul>

<h3>Resto do Brasil</h3>
<ul>
  <li><b>PAC</b>: R$ 39,90 · 5 a 9 dias úteis</li>
  <li><b>SEDEX</b>: R$ 62,90 · 2 a 4 dias úteis</li>
</ul>
<p><i>Em breve cotação automática via Melhor Envio com mais transportadoras.</i></p>

<h3>Prazos</h3>
<p>O prazo conta a partir da <b>confirmação do pagamento</b>, não do pedido.
Pagamentos PIX confirmam na hora; boleto leva 1-2 dias úteis pra compensar.</p>

<h3>Acompanhar pedido</h3>
<p>Você recebe e-mail e WhatsApp com o código de rastreio assim que postamos
seu pedido. Pode acompanhar também em <a href="/minha-conta">Minha conta</a>.</p>

<h3>Retirada na loja física</h3>
<p>Mora em Cascavel? Pode retirar grátis em nossa loja na
<b>R. Engenheiro Rebouças, 2053 — Centro</b>. Estacionamento gratuito em frente!
Horário: segunda a sexta 9h-18h · sábado 9h-13h.</p>
""",
    },
    'formas-pagamento': {
        'titulo': 'Formas de pagamento',
        'conteudo': """
<h3>💳 Cartão de crédito</h3>
<p>Aceitamos as principais bandeiras: Visa, Mastercard, Elo, Hipercard, Amex.</p>
<ul>
  <li>À vista (1x) <b>sem juros</b></li>
  <li>Parcelado em <b>até 12x</b> com juros (Tabela Price)</li>
  <li>Pagamento processado com segurança via <b>Asaas</b></li>
  <li>Aprovação imediata na maioria dos casos</li>
</ul>

<h3>📱 PIX</h3>
<p>Forma mais rápida e com <b>3% de desconto</b>!</p>
<ul>
  <li>Desconto aplicado automaticamente no checkout</li>
  <li>Confirmação em segundos</li>
  <li>Pedido entra em separação na hora</li>
</ul>

<h3>📄 Boleto bancário</h3>
<p>Também tem <b>3% de desconto</b>:</p>
<ul>
  <li>Vencimento em 3 dias úteis</li>
  <li>Compensa em até 2 dias úteis após o pagamento</li>
  <li>Pode pagar no app do banco, lotérica ou agência</li>
</ul>

<h3>🔒 Segurança</h3>
<p>Não armazenamos dados do seu cartão. Todo o processamento é feito pela
plataforma Asaas, certificada PCI-DSS. Os dados trafegam por HTTPS com
certificado SSL válido.</p>
""",
    },
    'privacidade': {
        'titulo': 'Política de privacidade',
        'conteudo': """
<h3>Quem somos</h3>
<p><b>Luqui Brinquedos LTDA</b> (CNPJ 32.650.888/0001-02) — R. Engenheiro Rebouças,
2053 — Centro — Cascavel/PR. E-mail: contato@luquibrinquedos.com.br.</p>

<h3>Dados que coletamos</h3>
<ul>
  <li><b>Cadastrais:</b> nome, CPF, e-mail, telefone, endereço (necessários pra entrega e emissão de nota fiscal)</li>
  <li><b>De compra:</b> histórico de pedidos, formas de pagamento usadas, produtos visitados</li>
  <li><b>Técnicos:</b> IP, navegador, cookies de sessão (pra manter o carrinho e seu login funcionando)</li>
</ul>

<h3>Pra que usamos</h3>
<ul>
  <li>Processar pedidos, emitir nota e fazer a entrega</li>
  <li>Enviar atualizações do pedido por e-mail e WhatsApp</li>
  <li>Atender solicitações e dúvidas</li>
  <li>Cumprir obrigações legais (notas fiscais, prazo de guarda de 5 anos)</li>
  <li><b>Marketing</b> apenas se você optar (e-mail de promoções) — você pode descadastrar a qualquer momento</li>
</ul>

<h3>Com quem compartilhamos</h3>
<ul>
  <li><b>Asaas</b>: processamento dos pagamentos</li>
  <li><b>Correios / Melhor Envio</b>: entrega das encomendas</li>
  <li><b>Resend</b>: envio de e-mails transacionais</li>
  <li><b>SEFAZ</b>: emissão de NFC-e/NF-e quando obrigatório</li>
</ul>
<p>Não vendemos nem cedemos seus dados pra terceiros sem relação com a compra.</p>

<h3>Seus direitos (LGPD)</h3>
<p>Conforme a <b>Lei 13.709/2018 (LGPD)</b>, você pode:</p>
<ul>
  <li>Pedir confirmação dos dados que temos</li>
  <li>Acessar, corrigir ou atualizar seus dados</li>
  <li>Pedir a eliminação (exceto os que somos obrigados a guardar por lei fiscal)</li>
  <li>Revogar consentimento de marketing a qualquer momento</li>
</ul>
<p>Pra exercer qualquer direito, mande e-mail pra
<a href="mailto:privacidade@luquibrinquedos.com.br">privacidade@luquibrinquedos.com.br</a>
ou WhatsApp <b>(45) 99111-9800</b>. Respondemos em até 15 dias.</p>

<h3>Cookies</h3>
<p>Usamos cookies essenciais (sessão, carrinho) e analíticos (entender quais
produtos são mais vistos). Você pode desabilitar no seu navegador, mas algumas
funções podem parar de funcionar.</p>

<h3>Segurança</h3>
<p>Site protegido por HTTPS (SSL). Senhas armazenadas com hash criptográfico
(nunca em texto puro). Acesso aos dados restrito à equipe Luqui.</p>
""",
    },
    'termos': {
        'titulo': 'Termos de uso',
        'conteudo': """
<h3>1. Aceite</h3>
<p>Ao usar luquibrinquedos.com.br você concorda com estes termos. Se não
concordar, por favor não use o site.</p>

<h3>2. Cadastro</h3>
<ul>
  <li>Você precisa ter <b>18 anos ou mais</b> pra fazer compras (ou autorização dos pais/responsáveis)</li>
  <li>Os dados informados devem ser verdadeiros e atualizados</li>
  <li>Você é responsável por manter sua senha em segredo</li>
</ul>

<h3>3. Preços e disponibilidade</h3>
<p>Preços e estoque podem variar. O preço válido é o que aparece no momento da
finalização do pedido. Em caso de erro grosseiro (ex.: brinquedo de R$ 200
listado por R$ 2), reservamos o direito de cancelar o pedido devolvendo
integralmente o valor pago.</p>

<h3>4. Clube Caixa Misteriosa</h3>
<ul>
  <li>Cobrança mensal automática via Asaas</li>
  <li>Sem fidelidade — cancele quando quiser na sua área "Minha conta" ou pelo WhatsApp</li>
  <li>A primeira caixa é despachada em até 7 dias após confirmação do 1º pagamento</li>
  <li>Brinquedos são <b>selecionados pela equipe Luqui</b> conforme o plano escolhido — não há possibilidade de escolher itens específicos</li>
  <li>Em caso de falha no pagamento, a entrega do mês fica suspensa até regularização</li>
</ul>

<h3>5. Propriedade intelectual</h3>
<p>Logos, fotos, descrições e código do site pertencem à Luqui Brinquedos.
Marcas de produtos pertencem aos respectivos fabricantes.</p>

<h3>6. Limitação de responsabilidade</h3>
<p>Não nos responsabilizamos por:</p>
<ul>
  <li>Atrasos causados pelos Correios/transportadora</li>
  <li>Endereço incorreto informado pelo cliente</li>
  <li>Uso inadequado do brinquedo (siga sempre a faixa etária recomendada)</li>
</ul>

<h3>7. Foro</h3>
<p>Eventuais conflitos serão resolvidos no foro da comarca de Cascavel/PR.</p>
""",
    },
}


# ─── CMS de páginas estáticas ────────────────────────────────────────────────
# Slug → (titulo padrão, HTML padrão). Usado como fallback quando o admin
# ainda não editou; o conteúdo do banco SOBREESCREVE quando existe.
PAGINAS_DEFAULTS = dict(PAGINAS_LEGAIS)
PAGINAS_DEFAULTS['retirar-na-loja'] = {
    'titulo': '🏪 Retire na loja',
    'conteudo': (
        '<p>Compre no site e <b>retire grátis</b> na nossa loja em Cascavel/PR.</p>'
        '<h3>Como funciona</h3>'
        '<ol>'
        '<li>Faça o pedido no site e escolha <b>Retirar na loja</b> no frete.</li>'
        '<li>Aguarde a confirmação por WhatsApp — em geral em até 1 dia útil o pedido fica pronto.</li>'
        '<li>Vá até a loja com um documento com foto pra retirar.</li>'
        '</ol>'
        '<h3>Endereço</h3>'
        '<p>R. Engenheiro Rebouças, 2053 — Centro — Cascavel/PR<br>'
        'Estacionamento gratuito em frente</p>'
        '<h3>Horário</h3>'
        '<p>Seg a sex: 09:00 às 18:00<br>'
        'Sábado: 09:00 às 13:00<br>'
        'Domingo: fechado</p>'
        '<p>Dúvidas? <a href="https://wa.me/5545991119800">Fale com a gente no WhatsApp 💚</a></p>'
    ),
}
PAGINAS_DEFAULTS['sobre'] = {
    'titulo': 'Sobre a Luqui',
    'conteudo': (
        '<p>A <b>Luqui Brinquedos</b> é uma loja de brinquedos em Cascavel/PR. '
        'Trabalhamos com as melhores marcas — Hot Wheels, Barbie, Mattel, Estrela — '
        'com preços justos e atendimento próximo.</p>'
        '<p><b>Endereço:</b> R. Engenheiro Rebouças, 2053 — Centro — Cascavel/PR<br>'
        '<b>Telefone:</b> (45) 99111-9800</p>'
    ),
}
PAGINAS_DEFAULTS['clube-sobre'] = {
    'titulo': '🎁 Clube Luqui',
    'conteudo': (
        '<p>Junta pontos comprando na loja física da Luqui e usa pra abater nas '
        'compras do site.</p>'
        '<p>A cada R$ 1,00 gasto na loja você ganha pontos. No site, na hora de '
        'fechar a compra, escolha quantos pontos usar — até 25% do valor do pedido.</p>'
    ),
}

def _pagina_get(slug):
    """Retorna (titulo, conteudo) lendo do banco; cai pra default se não tem."""
    try:
        row = db_execute("SELECT titulo, conteudo FROM paginas_cms WHERE slug=%s",
                         [slug], fetch='one')
    except Exception:
        row = None
    if row:
        return row['titulo'], row['conteudo']
    d = PAGINAS_DEFAULTS.get(slug)
    if not d:
        return None, None
    return d['titulo'], d['conteudo']

def _render_pagina_legal(slug):
    titulo, conteudo = _pagina_get(slug)
    if not titulo:
        abort(404)
    return render_template('pagina.html',
                           titulo=titulo, conteudo=conteudo,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/sobre')
def pag_sobre():
    return render_template('sobre.html',
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/retirar-na-loja')
def pag_retirar_na_loja():
    return _render_pagina_legal('retirar-na-loja')


@app.route('/api/produto/<int:pid>/avaliacao', methods=['POST'])
def avaliacao_criar(pid):
    c = cliente_logado()
    d = request.get_json() or {}
    estrelas = max(1, min(5, int(d.get('estrelas') or 0)))
    titulo = (d.get('titulo') or '')[:120]
    comentario = (d.get('comentario') or '')[:2000]
    foto_url = (d.get('foto_url') or '').strip()[:500] or None
    if not comentario.strip():
        return jsonify({'erro': 'Escreve algo no comentário'}), 400
    aprovado = False
    pedido_id = None
    if c:
        ja = db_execute("""SELECT pi.pedido_id FROM pedido_itens pi
            JOIN pedidos p ON p.id=pi.pedido_id
            WHERE pi.produto_pdv_id=%s AND p.cliente_id=%s AND p.status='pago'
            LIMIT 1""", [pid, c['id']], fetch='one')
        if ja:
            aprovado = True
            pedido_id = ja['pedido_id']
    db_execute("""INSERT INTO avaliacoes
        (produto_pdv_id, cliente_id, pedido_id, estrelas, titulo, comentario,
         aprovado, foto_url)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        [pid, c['id'] if c else None, pedido_id, estrelas,
         titulo or None, comentario, aprovado, foto_url])
    return jsonify({'ok': True, 'aprovado_auto': aprovado})


@app.route('/admin/avaliacoes')
@requer_admin
def admin_avaliacoes():
    rows = db_execute("""SELECT a.*, c.nome AS cliente_nome
        FROM avaliacoes a LEFT JOIN clientes_site c ON c.id=a.cliente_id
        ORDER BY a.aprovado, a.criado_em DESC LIMIT 200""", fetch='all') or []
    return render_template('admin_avaliacoes.html', avaliacoes=rows)


@app.route('/admin/avaliacoes/<int:aid>/aprovar', methods=['POST'])
@requer_admin
def admin_avaliacao_aprovar(aid):
    db_execute("UPDATE avaliacoes SET aprovado=true WHERE id=%s", [aid])
    return redirect(url_for('admin_avaliacoes'))


@app.route('/admin/avaliacoes/<int:aid>/excluir', methods=['POST'])
@requer_admin
def admin_avaliacao_excluir(aid):
    db_execute("DELETE FROM avaliacoes WHERE id=%s", [aid])
    return redirect(url_for('admin_avaliacoes'))


RETENCAO_VISITAS_DIAS = int(os.environ.get('RETENCAO_VISITAS_DIAS', '90'))


@app.route('/cron/limpeza-visitas')
def cron_limpeza_visitas():
    """Apaga pageview cru com mais de RETENCAO_VISITAS_DIAS dias.

    A contagem diária de bot (site_visitas_bots_diario) NÃO é apagada — ela é
    minúscula e é o histórico longo. O que sai é linha crua de humano, que só
    serve pras consultas de janela curta do dashboard.
    """
    if not _cron_token_ok():
        return 'unauthorized', 401
    corte = RETENCAO_VISITAS_DIAS
    antes = db_execute('SELECT COUNT(*) AS n FROM site_visitas', fetch='one') or {}
    db_execute("DELETE FROM site_visitas WHERE ts < NOW() - %s::interval",
               [f'{corte} days'])
    depois = db_execute('SELECT COUNT(*) AS n FROM site_visitas', fetch='one') or {}
    apagadas = (antes.get('n') or 0) - (depois.get('n') or 0)
    app.logger.info('limpeza-visitas: %s linhas apagadas (corte %sd)', apagadas, corte)
    return jsonify({'ok': True, 'apagadas': apagadas,
                    'restantes': depois.get('n') or 0, 'corte_dias': corte})


@app.route('/cron/aniversariantes')
def cron_aniversariantes():
    """Gera cupom personalizado pros aniversariantes do dia + WA + email."""
    if not _cron_token_ok():
        return 'unauthorized', 401
    if not CUPOM_ANIVERSARIO_ATIVO:
        return jsonify({'ok': True, 'desligado': True, 'gerados': 0})
    rows = db_execute("""
        SELECT * FROM clientes_site
         WHERE data_nascimento IS NOT NULL
           AND EXTRACT(MONTH FROM data_nascimento) = EXTRACT(MONTH FROM CURRENT_DATE)
           AND EXTRACT(DAY FROM data_nascimento) = EXTRACT(DAY FROM CURRENT_DATE)
    """, fetch='all') or []
    gerados = 0
    for c in rows:
        codigo = f'ANIVER{c["id"]}-{secrets.token_hex(3).upper()}'[:40]
        try:
            db_execute("""INSERT INTO cupons (codigo, tipo, valor, valor_min,
                          usos_max, valido_ate, ativo)
                          VALUES (%s,'pct',15,0,1,
                                  CURRENT_DATE + INTERVAL '14 days', true)""",
                       [codigo])
            enviar_whatsapp(c['telefone'],
                f"🎂 Feliz Aniversário, {c['nome'].split()[0]}! 💛\n\n"
                f"Pra comemorar, te demos um cupom de *15% OFF* válido por 14 dias:\n"
                f"`{codigo}`\n\n"
                f"Aproveita: https://www.luquibrinquedos.com.br")
            enviar_email(c['email'],
                f'🎂 Feliz aniversário, {c["nome"].split()[0]}!',
                f"""<p>🎉 Parabéns!</p>
<p>Hoje é seu dia especial e a Luqui preparou um <b>cupom de 15% OFF</b> pra você:</p>
<p style="background:#FEF3C7;padding:14px 24px;border-radius:8px;font-size:22px;
          font-weight:900;color:#1652C7;text-align:center;letter-spacing:2px">{codigo}</p>
<p>Válido por <b>14 dias</b>. Use no checkout.</p>
<p><a href="https://www.luquibrinquedos.com.br" style="background:#FFC700;color:#1652C7;padding:12px 28px;border-radius:8px;font-weight:900;text-decoration:none">🎁 Aproveitar</a></p>""")
            gerados += 1
        except Exception as e:
            log.error("aniver %s: %s", c['id'], e)
    return jsonify({'ok': True, 'gerados': gerados})


@app.route('/api/checkout/cupom-primeira', methods=['POST'])
def cupom_primeira_compra():
    """Pra cliente novo logado que nunca usou cupom de primeira compra."""
    if not CUPOM_PRIMEIRA_COMPRA_ATIVO:
        return jsonify({'pode': False})
    c = cliente_logado()
    if not c or c.get('ganhou_primeira'):
        return jsonify({'pode': False})
    return jsonify({'pode': True, 'codigo': 'PRIMEIRO10', 'desconto_pct': 10})


@app.route('/cron/carrinho-abandonado')
def cron_carrinho_abandonado():
    """Pedidos aguardando_pagto há 24-48h: dispara WhatsApp/email lembrando."""
    if not _cron_token_ok():
        return 'unauthorized', 401
    rows = db_execute("""
        SELECT * FROM pedidos
         WHERE status='aguardando_pagto'
           AND criado_em < NOW() - INTERVAL '24 hours'
           AND criado_em > NOW() - INTERVAL '48 hours'
           AND COALESCE(observacao,'') NOT LIKE %s
        LIMIT 50""", ['%[lembrete-enviado]%'], fetch='all') or []
    enviados = 0
    for p in rows:
        try:
            primeiro = ((p.get('nome') or '').strip().split() or ['amigo(a)'])[0]
            enviar_whatsapp(p['telefone'],
                f"💛 Oi {primeiro}! "
                f"Vi que você começou um pedido aqui na Luqui mas ainda não finalizou.\n\n"
                f"Total: *{rs(p['total'])}*\n\n"
                f"Tá tudo certinho? Quer finalizar?\n"
                f"👉 https://www.luquibrinquedos.com.br/pedido/{p['id']}/pagamento?t={p.get('token','')}")
            enviar_email(p['email'],
                f'Esqueceu de finalizar seu pedido #{p["id"]}?',
                f"""<p>Oi {p['nome'].split()[0]}! 💛</p>
<p>Notamos que você começou um pedido aqui na Luqui mas ainda não finalizou o pagamento.</p>
<p><b>Total:</b> {rs(p['total'])}</p>
<p><a href="https://www.luquibrinquedos.com.br/pedido/{p['id']}/pagamento?t={p.get('token','')}"
   style="background:#FFC700;color:#1652C7;padding:12px 24px;border-radius:8px;
          font-weight:900;text-decoration:none;display:inline-block">
  💛 Finalizar pedido
</a></p>
<p>Se mudou de ideia, sem problema. Mas se foi distração, é só clicar e a gente despacha tudinho! 🧸</p>""")
            db_execute("""UPDATE pedidos SET observacao=COALESCE(observacao,'')
                          || ' [lembrete-enviado]' WHERE id=%s""", [p['id']])
            enviados += 1
        except Exception as e:
            log.error("carrinho abandonado %s: %s", p['id'], e)
    return jsonify({'ok': True, 'enviados': enviados})


RECUP_LIMITE_DIA = 3


@app.route('/cron/recuperar-pedidos-parados')
def cron_recuperar_pedidos_parados():
    """Pedidos parados há 48h+: manda o link da fatura hospedada do Asaas.

    Complementa /cron/carrinho-abandonado, que só pega a janela de 24-48h e
    devolve o cliente pro checkout transparente — o mesmo que recusou o cartão
    dele. Aqui mandamos o caminho que aprova 88% (ver o fallback em
    /api/pedido/<id>/pagar-cartao).

    Três travas, todas deliberadas:
      1. Máximo de RECUP_LIMITE_DIA por dia — a Z-API restringe disparo em
         massa, então isso NÃO pode virar fila de 50.
      2. Dedupe por telefone: quem tentou 5 vezes recebe UMA mensagem, sobre o
         pedido de maior valor, e os irmãos são marcados junto.
      3. Pula quem já tem qualquer pedido pago — cliente que voltou e comprou
         por outro caminho não pode ser cobrado de novo.
      4. Pula pedido com risco >= RISCO_LIMITE que ninguém liberou. Sem isso o
         cron ia buscar de volta justamente o que o antifraude marcou: em
         31/07/2026 ele ressuscitou o pedido #96 (Rio de Janeiro, "6x no
         cartão" + mesmo endereço de entrega de outro CPF), feito na rajada de
         27/07 e parado desde então. Pedido liberado na mão (risco_liberado_em)
         volta a ser recuperável — a decisão humana manda.
    """
    if not _cron_token_ok():
        return 'unauthorized', 401
    hoje = datetime.now(SP_TZ).date().isoformat()
    marca_hoje = f'[recuperacao-enviada:{hoje}]'
    ja_hoje = db_execute(
        "SELECT COUNT(*) AS n FROM pedidos WHERE COALESCE(observacao,'') LIKE %s",
        [f'%{marca_hoje}%'], fetch='one') or {}
    restam = RECUP_LIMITE_DIA - int(ja_hoje.get('n') or 0)
    if restam <= 0:
        return jsonify({'ok': True, 'enviados': 0, 'motivo': 'limite diário'})

    # Filtro de risco: mesmo limiar que segura a etiqueta automática, pra não
    # existirem dois critérios de "pedido suspeito" divergindo com o tempo.
    _RISCO_SQL = (" AND (COALESCE(p.risco_score,0) < %s"
                  "      OR p.risco_liberado_em IS NOT NULL)")
    _BASE_SQL = """
          FROM pedidos p
         WHERE p.status='aguardando_pagto'
           AND p.criado_em < NOW() - INTERVAL '48 hours'
           AND COALESCE(p.observacao,'') NOT LIKE %s
           AND NOT EXISTS (
                 SELECT 1 FROM pedidos q
                  WHERE regexp_replace(COALESCE(q.telefone,''), '\\D', '', 'g')
                      = regexp_replace(COALESCE(p.telefone,''), '\\D', '', 'g')
                    AND regexp_replace(COALESCE(p.telefone,''), '\\D', '', 'g') <> ''
                    AND q.status IN """ + _SQL_PAGOS + ")"
    cands = db_execute("SELECT *" + _BASE_SQL + _RISCO_SQL + " ORDER BY p.total DESC",
                       ['%[recuperacao-enviada%', RISCO_LIMITE], fetch='all') or []
    # Quantos ficaram de fora POR RISCO. Vai na resposta pro log do cron mostrar
    # — filtro que corta em silêncio vira "não tinha ninguém pra recuperar".
    _pulados = db_execute(
        "SELECT COUNT(*) AS n" + _BASE_SQL
        + " AND COALESCE(p.risco_score,0) >= %s AND p.risco_liberado_em IS NULL",
        ['%[recuperacao-enviada%', RISCO_LIMITE], fetch='one') or {}
    pulados_risco = int(_pulados.get('n') or 0)

    vistos, enviados, detalhe = set(), 0, []
    for p in cands:
        if enviados >= restam:
            break
        tel_norm = ''.join(c for c in (p.get('telefone') or '') if c.isdigit())
        if not tel_norm or tel_norm in vistos:
            continue
        vistos.add(tel_norm)
        try:
            # Garante uma fatura hospedada (o caminho que o emissor aprova)
            link = (p.get('asaas_link') or '')
            if not link.startswith('http'):
                cust = link.split(':', 1)[1] if link.startswith('customer:') else None
                if not cust:
                    cust = asaas_criar_customer(p['nome'], p['email'],
                                                p['cpf'], p['telefone'])
                cob = asaas_criar_cobranca(
                    cust, p['total'], 'CREDIT_CARD',
                    f'Luqui Brinquedos — Pedido #{p["id"]}',
                    parcelas=p.get('parcelas') or 1,
                    externa_ref=f'pedido-{p["id"]}') if cust else None
                link = (cob or {}).get('invoiceUrl') or ''
                if link:
                    db_execute("""UPDATE pedidos SET asaas_cobranca_id=%s,
                                  asaas_link=%s WHERE id=%s""",
                               [(cob or {}).get('id'), link, p['id']])
            if not link:
                log.error("recuperacao %s: sem link de pagamento", p['id'])
                continue
            primeiro = ((p.get('nome') or '').strip().split() or ['amigo(a)'])[0]
            enviar_whatsapp(p['telefone'],
                f"💛 Oi {primeiro}! Aqui é da *Luqui Brinquedos*.\n\n"
                f"Vi que seu pagamento do pedido #{p['id']} "
                f"({rs(p['total'])}) não passou. Isso costuma ser o banco "
                f"barrando compra pela internet, não é problema no seu cartão.\n\n"
                f"Separei um link seguro que resolve:\n{link}\n\n"
                f"Seus produtos estão guardados. Qualquer dúvida, é só chamar aqui! 🧸")
            enviar_email(p['email'],
                f'Seu pedido #{p["id"]} está guardado — link de pagamento',
                f"""<p>Oi {primeiro}! 💛</p>
<p>Notamos que o pagamento do seu pedido não foi concluído. Na maioria das
vezes é o banco barrando a compra pela internet — não é problema no seu cartão.</p>
<p><b>Pedido #{p['id']} — Total: {rs(p['total'])}</b></p>
<p><a href="{link}"
   style="background:#FFC700;color:#1652C7;padding:12px 24px;border-radius:8px;
          font-weight:900;text-decoration:none;display:inline-block">
  🔐 Pagar pela página segura
</a></p>
<p>Seus produtos continuam guardados. Se precisar de ajuda, é só responder. 🧸</p>""")
            # Marca todos os pedidos parados do mesmo telefone
            db_execute("""UPDATE pedidos SET observacao=COALESCE(observacao,'')
                          || %s
                          WHERE status='aguardando_pagto'
                            AND regexp_replace(COALESCE(telefone,''),'\\D','','g')=%s""",
                       [f' {marca_hoje}', tel_norm])
            enviados += 1
            detalhe.append({'pedido': p['id'], 'total': float(p['total'])})
            # A Z-API engole mensagem quando o disparo é rápido demais; com
            # 3 envios por rodada, esperar 12s entre eles é barato.
            if enviados < restam:
                time.sleep(12)
        except Exception as e:
            log.error("recuperacao %s: %s", p['id'], e)
    if pulados_risco:
        log.info("recuperacao: %s pedido(s) fora por risco >= %s (nao liberados)",
                 pulados_risco, RISCO_LIMITE)
    return jsonify({'ok': True, 'enviados': enviados,
                    'restantes_hoje': restam - enviados,
                    'pulados_risco': pulados_risco, 'detalhe': detalhe})


@app.route('/promocoes')
def pag_promocoes():
    """Página única de ofertas: promoção vigente do PDV + o que era o
    LiquidaLuqui (flag `liquida` no PDV), num só lugar. Ordena por maior
    desconto — quem chega vê primeiro a oferta que mais vale a pena."""
    ordem = request.args.get('ordem', 'desconto')
    itens, vistos = [], set()

    rs_promos = pdv_get('/api/integracao/promocoes') or {}
    for p in rs_promos.get('promocoes', []):
        pid = p.get('produto_id')
        if not pid or pid in vistos:
            continue
        vistos.add(pid)
        itens.append({
            'id': pid,
            'descricao': p.get('descricao'),
            'foto_url': p.get('foto'),
            'preco_venda': float(p.get('preco_venda') or 0),
            'preco_promo': float(p.get('preco_promo') or 0) or None,
            'estoque_atual': float(p.get('estoque_atual') or 0),
            'tag': 'promo',
        })

    # LiquidaLuqui: produtos marcados como liquida no PDV entram na mesma
    # vitrine (sem repetir quem já está em promoção vigente).
    liquida, _ = listar_produtos(limite=48, destaque='liquida')
    for p in liquida:
        if p['id'] in vistos:
            continue
        vistos.add(p['id'])
        itens.append({
            'id': p['id'],
            'descricao': p.get('descricao'),
            'foto_url': p.get('foto_url'),
            'preco_venda': float(p.get('preco_venda') or 0),
            'preco_promo': (float(p['preco_promo'])
                            if p.get('preco_promo') else None),
            'estoque_atual': float(p.get('estoque_atual') or 0),
            'tag': 'liquida',
        })

    for it in itens:
        cheio, promo = it['preco_venda'], it['preco_promo']
        it['economia'] = (cheio - promo) if (promo and cheio > promo) else 0
        it['desconto_pct'] = int(round(it['economia'] / cheio * 100)) if (
            it['economia'] and cheio) else 0
        it['preco_final'] = promo or cheio

    # esgotado sempre por último, independentemente da ordenação
    chaves = {
        'desconto': lambda i: (-i['desconto_pct'], i['preco_final']),
        'menor-preco': lambda i: (i['preco_final'],),
        'maior-preco': lambda i: (-i['preco_final'],),
    }
    chave = chaves.get(ordem, chaves['desconto'])
    itens.sort(key=lambda i: (i['estoque_atual'] <= 0,) + tuple(chave(i)))

    economia_max = max([i['economia'] for i in itens], default=0)
    return render_template('promocoes.html',
                           itens=itens,
                           ordem=ordem if ordem in chaves else 'desconto',
                           economia_max=economia_max,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/api/avise-me', methods=['POST'])
def avise_me():
    d = request.get_json() or {}
    pid = int(d.get('produto_id') or 0)
    email = (d.get('email') or '').strip().lower()
    tel = (d.get('telefone') or '').strip()
    if not pid or '@' not in email:
        return jsonify({'erro': 'Dados inválidos'}), 400
    db_execute("""INSERT INTO avise_me (produto_pdv_id, email, telefone)
                  VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
               [pid, email, tel or None])
    return jsonify({'ok': True})


@app.route('/cron/email-pos-compra')
def cron_email_pos_compra():
    """Roda diário: pedidos pagos há ~7 dias e ainda sem email de avaliação."""
    if not _cron_token_ok():
        return 'unauthorized', 401
    candidatos = db_execute("""
        SELECT * FROM pedidos
         WHERE status IN """ + _SQL_PAGOS + """
           AND pago_em IS NOT NULL
           AND pago_em < NOW() - INTERVAL '7 days'
           AND pago_em > NOW() - INTERVAL '14 days'
           AND COALESCE(observacao,'') NOT LIKE %s
        LIMIT 50""", ['%[avaliacao-enviada]%'], fetch='all') or []
    enviados = 0
    for p in candidatos:
        try:
            primeiro = ((p.get('nome') or '').strip().split() or ['amigo(a)'])[0]
            enviar_email(p['email'],
                f'Como foi seu pedido #{p["id"]}? 💛',
                f"""<p>Oi {primeiro}! Tudo bem?</p>
<p>Faz uma semana que seu pedido <b>#{p['id']}</b> foi confirmado.
Esperamos que tudo tenha chegado certinho! 🧸</p>
<p>Que tal contar pra gente o que você achou? Sua avaliação ajuda outras famílias
a escolherem com confiança!</p>
<p><a href="https://www.luquibrinquedos.com.br/pedido/{p['id']}/tracking?t={p.get('token','')}"
     style="background:#FFC700;color:#1652C7;padding:12px 24px;border-radius:8px;
            font-weight:900;text-decoration:none;display:inline-block">
  ⭐ Avaliar produtos
</a></p>
<p>Abraço,<br>Luqui Brinquedos 💛</p>""")
            db_execute("""UPDATE pedidos SET observacao=COALESCE(observacao,'')
                       || ' [avaliacao-enviada]' WHERE id=%s""", [p['id']])
            enviados += 1
        except Exception as e:
            log.error("pos-compra %s: %s", p['id'], e)
    return jsonify({'ok': True, 'enviados': enviados})


@app.route('/cron/avise-me')
def cron_avise_me():
    """Roda periódico (~30min): pra cada produto com cadastro pendente em
    avise_me, checa o estoque no PDV. Se voltou (estoque > 0), dispara
    email (Resend) + WhatsApp (Z-API) pra todos cadastrados naquele
    produto e marca notificado_em=NOW()."""
    if not _cron_token_ok():
        return 'unauthorized', 401
    # Pega produto_ids únicos com cadastro pendente
    pendentes_prods = db_execute("""
        SELECT DISTINCT produto_pdv_id FROM avise_me
         WHERE notificado_em IS NULL
         LIMIT 100""", fetch='all') or []
    if not pendentes_prods:
        return jsonify({'ok': True, 'verificados': 0, 'disparados': 0})

    disparados, verificados = 0, 0
    for pp in pendentes_prods:
        pid = pp['produto_pdv_id']
        verificados += 1
        try:
            prod = buscar_produto(pid) or {}
        except Exception as e:
            log.warning("avise-me prod %s: %s", pid, e)
            continue
        if float(prod.get('estoque_atual') or 0) <= 0:
            continue  # Ainda sem estoque, aguarda próxima rodada

        # Voltou! Dispara pra todos cadastrados nesse produto
        cadastros = db_execute("""
            SELECT id, email, telefone FROM avise_me
             WHERE produto_pdv_id=%s AND notificado_em IS NULL""",
            [pid], fetch='all') or []
        nome = prod.get('descricao') or f'Produto #{pid}'
        url = SITE_URL + url_produto(prod)
        preco = float(prod.get('preco_promo') or prod.get('preco_venda') or 0)
        for c in cadastros:
            try:
                html = f"""<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
<h2 style="color:#1652C7">🎉 Boa notícia!</h2>
<p>Olá! O brinquedo que você pediu pra avisar voltou ao estoque na Luqui Brinquedos:</p>
<div style="background:#F1F5F9;padding:16px;border-radius:10px;margin:14px 0">
  <div style="font-weight:700;font-size:15px">{nome}</div>
  <div style="color:#16a34a;font-weight:800;font-size:22px;margin-top:6px">R$ {preco:.2f}</div>
</div>
<p>Corre que pode acabar de novo!</p>
<p style="margin:18px 0">
  <a href="{url}" style="background:#FFC700;color:#1652C7;padding:12px 22px;border-radius:8px;
       font-weight:900;text-decoration:none;display:inline-block">
    🛒 Ver produto agora
  </a>
</p>
<p style="font-size:12px;color:#64748b">Você recebeu este email porque pediu pra ser avisada quando o produto voltasse ao estoque.</p>
</div>"""
                if c.get('email'):
                    enviar_email(c['email'], f'🎉 Voltou! {nome[:50]}', html)
                if c.get('telefone'):
                    enviar_whatsapp(c['telefone'],
                        f"🎉 *Boa notícia!*\n\n"
                        f"O brinquedo que você pediu pra avisar voltou ao estoque na Luqui Brinquedos:\n\n"
                        f"*{nome}*\nR$ {preco:.2f}\n\n"
                        f"Corre que pode acabar de novo!\n{url}")
                db_execute("UPDATE avise_me SET notificado_em=NOW() WHERE id=%s",
                           [c['id']])
                disparados += 1
            except Exception as e:
                log.error("avise-me dispatch %s/%s: %s", pid, c['id'], e)
    log.info("avise-me cron: %d produtos verificados, %d avisos disparados",
             verificados, disparados)
    return jsonify({'ok': True, 'verificados': verificados,
                    'disparados': disparados})


# LUQUIZINHA_SYSTEM antigo + rota /api/luquizinha removidos.
# Substituidos pelo chatbot completo com tools (buscar_produtos, registrar_lead)
# em /api/luquizinha/chat. Ver mais abaixo neste mesmo arquivo.


@app.route('/api/newsletter', methods=['POST'])
def newsletter_signup():
    email = ((request.get_json() or {}).get('email') or '').strip().lower()
    nome = ((request.get_json() or {}).get('nome') or '').strip()
    if '@' not in email or '.' not in email:
        return jsonify({'erro': 'E-mail inválido'}), 400
    novo = db_execute("""INSERT INTO newsletter (email, nome) VALUES (%s, %s)
                  ON CONFLICT (email) DO UPDATE SET ativo=true,
                  nome=COALESCE(EXCLUDED.nome, newsletter.nome)
                  RETURNING (xmax = 0) AS inserido""",
               [email, nome or None], fetch='one')
    if novo and novo.get('inserido'):
        total = db_execute("SELECT COUNT(*) AS n FROM newsletter WHERE ativo",
                           fetch='one')['n']
        try:
            enviar_whatsapp(ADMIN_WHATSAPP,
                f"📧 *Nova inscrição na newsletter*\n\n"
                f"E-mail: {email}\n"
                f"Total de inscritos: {total}")
        except Exception as e:
            log.error("WA newsletter: %s", e)
    return jsonify({'ok': True})


@app.route('/trocas-devolucoes')
def pag_trocas():
    return _render_pagina_legal('trocas-devolucoes')


@app.route('/entregas')
def pag_entregas():
    return _render_pagina_legal('entregas')


@app.route('/formas-pagamento')
def pag_formas_pagto():
    return _render_pagina_legal('formas-pagamento')


@app.route('/privacidade')
def pag_privacidade():
    return _render_pagina_legal('privacidade')


@app.route('/termos')
def pag_termos():
    return _render_pagina_legal('termos')


@app.route('/robots.txt')
def robots_txt():
    # Duas categorias diferentes que antes estavam no mesmo balaio:
    #  - TREINO de LLM: raspa, não devolve visita. Continua bloqueado.
    #  - BUSCA COM CITAÇÃO (ChatGPT/Perplexity/Claude respondendo pergunta e
    #    linkando a fonte): manda clique de gente real procurando brinquedo.
    #    Bloquear isso era abrir mão de um canal inteiro. Liberado.
    txt = """# Treino de LLM — bloqueado (raspa e não traz cliente)
User-agent: GPTBot
Disallow: /
User-agent: anthropic-ai
Disallow: /
User-agent: Google-Extended
Disallow: /
User-agent: Applebot-Extended
Disallow: /
User-agent: meta-externalagent
Disallow: /
User-agent: meta-externalfetcher
Disallow: /
User-agent: Amazonbot
Disallow: /
User-agent: cohere-ai
Disallow: /
User-agent: Bytespider
Disallow: /
User-agent: CCBot
Disallow: /
User-agent: Diffbot
Disallow: /
User-agent: Omgilibot
Disallow: /
User-agent: Omgili
Disallow: /

# Busca com citação — LIBERADO: aparece na resposta com link pra loja
User-agent: OAI-SearchBot
Allow: /
User-agent: ChatGPT-User
Allow: /
User-agent: PerplexityBot
Allow: /
User-agent: Perplexity-User
Allow: /
User-agent: ClaudeBot
Allow: /
User-agent: FacebookBot
Allow: /

# SEO scrapers de concorrente
User-agent: AhrefsBot
Disallow: /
User-agent: SemrushBot
Disallow: /
User-agent: MJ12bot
Disallow: /
User-agent: DotBot
Disallow: /
User-agent: BLEXBot
Disallow: /
User-agent: PetalBot
Disallow: /
User-agent: YandexBot
Disallow: /

# Regras gerais — buscas legítimas (Google/Bing/Apple) seguem indexando.
# Sem Crawl-delay: o Google ignora, mas o Bing obedecia — 5s por página com
# 800+ produtos deixava o catálogo levar mais de uma hora por varredura.
User-agent: *
Allow: /
Disallow: /admin
Disallow: /api/
Disallow: /pedido/
Disallow: /carrinho
Disallow: /checkout
Disallow: /minha-conta
Disallow: /favoritos
Disallow: /login
Disallow: /cadastrar
Disallow: /produtos?
# Filtro e ordenação NÃO entram aqui de propósito: essas páginas mandam
# noindex no HTML, e bloquear no robots impediria o Google de ler justamente
# esse noindex — a URL ficaria no índice sem conteúdo, que é pior.

Sitemap: {SITEMAP}
""".replace('{SITEMAP}', SITE_URL + '/sitemap.xml')
    from flask import Response
    return Response(txt, mimetype='text/plain')


SITEMAP_TTL = int(os.environ.get('SITEMAP_TTL', '3600'))
_SITEMAP_CACHE = {}


@app.route('/sitemap.xml')
def sitemap_xml():
    """Sitemap dinâmico: estáticas + categorias visíveis + catálogo inteiro."""
    from flask import Response
    agora = time.time()
    c = _SITEMAP_CACHE.get('xml')
    if c and (agora - c['t']) < SITEMAP_TTL:
        return Response(c['v'], mimetype='application/xml')
    base = SITE_URL
    urls = [
        (base + '/', '1.0', 'daily'),
        (base + '/sobre', '0.7', 'monthly'),
    ]
    if CLUBE_LUQUI_ATIVO:
        urls.append((base + '/clube', '0.9', 'weekly'))
    urls += [
        (base + '/promocoes', '0.9', 'daily'),
        (base + '/novidades', '0.8', 'weekly'),
        (base + '/mais-vendidos', '0.8', 'weekly'),
        (base + '/trocas-devolucoes', '0.5', 'yearly'),
        (base + '/entregas', '0.5', 'yearly'),
        (base + '/formas-pagamento', '0.5', 'yearly'),
        (base + '/privacidade', '0.4', 'yearly'),
        (base + '/termos', '0.4', 'yearly'),
    ]
    for c in (listar_categorias() or []):
        urls.append((f"{base}/categoria/{c['slug']}", '0.8', 'weekly'))
    # Catálogo inteiro. Antes era uma chamada só, limite=100 e offset=0: o
    # comentário prometia 1000 mas o sitemap saía com 100 produtos de ~830 —
    # o resto o Google não tinha como descobrir. _feed_produtos() já pagina até
    # o fim e deduplica por id (a ordenação do PDV troca de página em página).
    for p in _feed_produtos():
        urls.append((base + url_produto(p), '0.6', 'weekly'))
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for u, prio, freq in urls:
        # & em slug de categoria quebraria o XML inteiro — o Google descarta o
        # arquivo, não a linha.
        loc = u.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        xml += f'  <url><loc>{loc}</loc><priority>{prio}</priority><changefreq>{freq}</changefreq></url>\n'
    xml += '</urlset>'
    _SITEMAP_CACHE['xml'] = {'t': agora, 'v': xml}
    return Response(xml, mimetype='application/xml')


@app.errorhandler(404)
def pag_404(e):
    sugestoes, _ = listar_produtos(limite=8)
    return render_template('404.html',
                           sugestoes=sugestoes or [],
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler()), 404


# ─── Feed de produtos (catalogo Meta / Google Shopping) ───────────────────────
# Anuncio de PRODUTO (catalogo) precisa de um feed: e ele que diz pra Meta o
# que existe, por quanto e se tem estoque. Sem isso so da pra anunciar imagem
# solta, sem o produto certo pra pessoa certa nem retargeting por item.
# Formato: RSS 2.0 com namespace g: — o mesmo que Meta e Google Shopping leem.
FEED_TTL = int(os.environ.get('FEED_TTL', '1800'))   # 30 min
_FEED_CACHE = {}


FEED_IMG_MIN = 500          # minimo que a Meta aceita no catalogo
_IMG_CACHE = {}             # pid -> (t, bytes|None)  None = usar o original


def _img_para_catalogo(bruto):
    """Devolve JPEG >=500x500 ou None se o original ja serve.

    Foto pequena NAO e esticada: esticar 270x270 pra 500 borra e fica feio no
    anuncio. Em vez disso a imagem vai centralizada numa moldura branca — sem
    perda de qualidade, e e o padrao de e-commerce.
    """
    from PIL import Image
    im = Image.open(io.BytesIO(bruto))
    if im.width >= FEED_IMG_MIN and im.height >= FEED_IMG_MIN:
        return None
    im = im.convert('RGB')
    lado = max(FEED_IMG_MIN, im.width, im.height)
    fundo = Image.new('RGB', (lado, lado), (255, 255, 255))
    fundo.paste(im, ((lado - im.width) // 2, (lado - im.height) // 2))
    saida = io.BytesIO()
    fundo.save(saida, 'JPEG', quality=88, optimize=True)
    return saida.getvalue()


@app.route('/pimg/<int:pid>.jpg')
def produto_img_catalogo(pid):
    """Imagem do produto no tamanho que o catalogo da Meta exige.

    Foto grande -> redireciona pro original (nao gasta banda nossa).
    Foto pequena -> devolve com moldura branca ate 500x500.
    Existe porque 71 produtos tinham foto abaixo de 500px e ficavam de fora do
    catalogo; corrigir um por um no PDV nao era viavel.
    """
    agora = time.time()
    c = _IMG_CACHE.get(pid)
    if c and (agora - c[0]) < 86400:
        if c[1] is None:
            return redirect(c[2], code=302)
        return Response(c[1], mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    try:
        p = buscar_produto(pid)
        url = (p or {}).get('foto_url')
        if not url:
            abort(404)
        if not str(url).startswith('http'):
            url = SITE_URL.rstrip('/') + '/' + str(url).lstrip('/')
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        nova = _img_para_catalogo(r.content)
    except Exception as e:
        log.error("pimg %s: %s", pid, str(e)[:120])
        abort(404)
    _IMG_CACHE[pid] = (agora, nova, url)
    if nova is None:
        return redirect(url, code=302)
    return Response(nova, mimetype='image/jpeg',
                    headers={'Cache-Control': 'public, max-age=86400'})


def _xml_escape(t):
    return (str(t or '').replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _feed_produtos():
    """Pagina a API do PDV ate o fim. Cacheado: o feed e lido por robo, nao
    faz sentido bater no PDV a cada request."""
    # dedup por id: a ordenacao do PDV nao e estavel entre paginas, entao um
    # produto podia cair em duas paginas e aparecer duas vezes no feed (achados:
    # 109549, 67008, 84226). ID repetido faz a Meta descartar o item.
    vistos, itens, offset = set(), [], 0
    while True:
        lote, total = listar_produtos(limite=100, offset=offset)
        if not lote:
            break
        for p in lote:
            pid = p.get('id')
            if pid and pid not in vistos:
                vistos.add(pid)
                itens.append(p)
        offset += len(lote)
        if offset >= (total or 0) or offset >= 5000:
            break
    return itens


@app.route('/feed.xml')
def feed_xml():
    agora = time.time()
    c = _FEED_CACHE.get('xml')
    if c and (agora - c['t']) < FEED_TTL:
        return Response(c['v'], mimetype='application/xml; charset=utf-8')
    try:
        produtos = _feed_produtos()
    except Exception as e:
        log.error("feed.xml: %s", e)
        if c:                      # entrega o feed velho em vez de quebrar
            return Response(c['v'], mimetype='application/xml; charset=utf-8')
        return Response('<rss version="2.0"/>', mimetype='application/xml'), 503

    base = SITE_URL.rstrip('/')
    linhas = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
              '<channel>',
              f'<title>{_xml_escape("Luqui Brinquedos")}</title>',
              f'<link>{base}</link>',
              '<description>Catalogo de produtos</description>']
    incluidos = 0
    for p in produtos:
        preco = p.get('preco_promo') or p.get('preco_venda')
        foto = p.get('foto_url')
        titulo = (p.get('descricao') or '').strip()
        # sem preco, foto ou titulo a Meta rejeita o item — nao adianta mandar
        if not (preco and foto and titulo and p.get('id')):
            continue
        # sempre pelo /pimg: foto grande so redireciona pro original, pequena
        # ganha moldura branca ate 500x500 (exigencia do catalogo da Meta).
        foto = f"{base}/pimg/{p['id']}.jpg"
        estoque = p.get('estoque_atual')
        disp = 'in stock' if (estoque is None or float(estoque) > 0) else 'out of stock'
        desc = (p.get('descricao_longa') or titulo).strip()[:4900]
        linhas.append('<item>')
        linhas.append(f"<g:id>{p['id']}</g:id>")
        linhas.append(f'<g:title>{_xml_escape(titulo[:150])}</g:title>')
        linhas.append(f'<g:description>{_xml_escape(desc)}</g:description>')
        linhas.append(f"<g:link>{base}{url_produto(p)}</g:link>")
        linhas.append(f'<g:image_link>{_xml_escape(foto)}</g:image_link>')
        linhas.append(f'<g:availability>{disp}</g:availability>')
        linhas.append(f'<g:price>{float(preco):.2f} BRL</g:price>')
        if p.get('preco_promo') and p.get('preco_venda'):
            linhas.append(f"<g:sale_price>{float(p['preco_promo']):.2f} BRL</g:sale_price>")
        linhas.append('<g:condition>new</g:condition>')
        linhas.append(f'<g:brand>{_xml_escape(p.get("marca") or "Luqui Brinquedos")}</g:brand>')
        if p.get('codigo_barras'):
            linhas.append(f'<g:gtin>{_xml_escape(p["codigo_barras"])}</g:gtin>')
        if p.get('departamento'):
            linhas.append(f'<g:product_type>{_xml_escape(p["departamento"])}</g:product_type>')
        linhas.append('</item>')
        incluidos += 1
    linhas.append('</channel></rss>')
    xml = '\n'.join(linhas)
    _FEED_CACHE['xml'] = {'t': agora, 'v': xml}
    log.info("feed.xml: %s produtos no feed (de %s lidos)", incluidos, len(produtos))
    return Response(xml, mimetype='application/xml; charset=utf-8')


@app.route('/__capi')
def __capi_status():
    """Diagnostico do Purchase mandado pra Meta. Existe porque a API de
    estatisticas do Facebook atrasa ~5h — sem isto nao da pra saber se o
    evento saiu sem esperar a tarde inteira.
    Autentica pelo SHA-256 do META_CAPI_TOKEN: quem ja tem o segredo consegue
    calcular, e o segredo em si nunca viaja na URL (nem vai pro access log)."""
    esperado = hashlib.sha256((META_CAPI_TOKEN or 'x').encode()).hexdigest()
    tok = (request.args.get('t') or '').strip()
    if not META_CAPI_TOKEN or not secrets.compare_digest(tok, esperado):
        return jsonify({'erro': 'nao autorizado'}), 401
    try:
        rows = db_execute("""SELECT id, total, status, pago_em, capi_em, capi_resposta
                             FROM pedidos
                             WHERE capi_em IS NOT NULL OR pago_em IS NOT NULL
                             ORDER BY COALESCE(capi_em, pago_em) DESC LIMIT 10""",
                          fetch='all') or []
        return jsonify({
            'token_configurado': bool(META_CAPI_TOKEN),
            'pixel': META_PIXEL_ID,
            'ultimos': [{
                'pedido': r['id'],
                'total': float(r['total'] or 0),
                'status': r['status'],
                'pago_em': r['pago_em'].isoformat() if r['pago_em'] else None,
                'capi_em': r['capi_em'].isoformat() if r['capi_em'] else None,
                'capi_resposta': r['capi_resposta'],
            } for r in rows],
        })
    except Exception as e:
        return jsonify({'erro': str(e)[:200]}), 500


@app.route('/healthz')
def healthz():
    try:
        db_execute("SELECT 1", fetch='one')
        return 'ok', 200
    except Exception:
        return 'down', 500


_APP_PY_MTIME = int(os.path.getmtime(__file__))


@app.route('/__version')
def __version():
    """Identifica a versão atual em produção pra deploy.sh confirmar que
    o auto-deploy realmente subiu. Usa mtime do app.py (muda sempre que
    o container é redeployado, sem depender de env var do Railway)."""
    sha = (os.getenv('RAILWAY_GIT_COMMIT_SHA')
           or os.getenv('SOURCE_COMMIT')
           or '')
    return jsonify({'sha': (sha[:12] if sha else 'dev'),
                    'full': sha or '',
                    'mtime': _APP_PY_MTIME,
                    'branch': os.getenv('RAILWAY_GIT_BRANCH') or 'main'})


@app.route('/')
def home():
    # ordem=recentes: a vitrine de baixo mostrava sempre os mesmos produtos
    # do começo do alfabeto (ABC, A CASA...). Agora é o que chegou por último.
    produtos, _ = listar_produtos(limite=12, ordem='recentes')
    categorias = listar_categorias()
    banners = db_execute(
        "SELECT * FROM banners WHERE ativo ORDER BY ordem", fetch='all') or []
    if not CLUBE_LUQUI_ATIVO:
        banners = [b for b in banners
                   if '/clube' not in (b.get('link') or '')
                   and 'clube' not in (b.get('titulo') or '').lower()]
    return render_template('home.html',
                           produtos=produtos,
                           categorias=categorias,
                           banners=banners,
                           mais_vendidos=produtos_mais_vendidos(8),
                           mais_visitados=produtos_mais_visitados(8),
                           mais_procurados=produtos_mais_procurados(8),
                           desconto_pix_pct=float(cfg('desconto_pix_pct', '3')),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/categoria/<slug>')
def categoria(slug):
    pagina = max(1, int(request.args.get('p', 1)))
    por_pagina = 24
    ordem = request.args.get('ordem', 'destaque')
    so_promo = request.args.get('promo') == '1'
    extras = filtros_da_querystring(request)
    produtos, total = listar_produtos(
        categoria=slug, limite=por_pagina,
        offset=(pagina - 1) * por_pagina, **extras)
    if so_promo:
        produtos = [p for p in produtos if p.get('preco_promo')]
    if ordem == 'barato':
        produtos.sort(key=lambda p: float(p.get('preco_promo') or p.get('preco_venda') or 0))
    elif ordem == 'caro':
        produtos.sort(key=lambda p: float(p.get('preco_promo') or p.get('preco_venda') or 0),
                      reverse=True)
    elif ordem == 'novidade':
        produtos.sort(key=lambda p: p.get('id', 0), reverse=True)
    elif ordem == 'promo':
        produtos.sort(key=lambda p: 0 if p.get('preco_promo') else 1)
    categorias = listar_categorias()
    filtros = listar_filtros()
    cat_nome = next((c['nome'] for c in categorias if c['slug'] == slug), slug)
    return render_template('categoria.html',
                           produtos=produtos,
                           total=total,
                           pagina=pagina,
                           por_pagina=por_pagina,
                           categorias=categorias,
                           categoria_nome=cat_nome,
                           categoria_slug=slug,
                           ordem=ordem, so_promo=so_promo,
                           filtros=filtros,
                           filtros_ativos=extras,
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/buscar')
def buscar():
    q = (request.args.get('q') or '').strip()
    extras = filtros_da_querystring(request)
    produtos, total = (listar_produtos(busca=q, limite=48, **extras)
                       if (q or extras) else ([], 0))
    if q:
        log_busca(q, resultados=total, origem='site')
    return render_template('busca.html',
                           produtos=produtos, total=total,
                           termo=q or 'Busca', termo_q=q,
                           categorias=listar_categorias(),
                           filtros=listar_filtros_planos(),
                           filtros_ativos=extras,
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


def _pagina_destaque(tag, titulo, fallback=None):
    """Renderiza /novidades e /mais-vendidos usando o template de busca.
    A flag de destaque no PDV é opcional: se ninguém marcou produto nenhum,
    a página cairia vazia — então `fallback` monta a lista com dado real
    (cadastro mais recente / ranking de venda) em vez de mostrar nada."""
    extras = filtros_da_querystring(request)
    # garante que o destaque sempre fica fixado mesmo que o usuario clique filtros
    extras['destaque'] = [tag]
    produtos, total = listar_produtos(limite=48, **extras)
    if not produtos and fallback:
        extras.pop('destaque', None)
        if fallback == 'vendidos' and not extras:
            produtos = produtos_mais_vendidos(24)
            total = len(produtos)
        else:
            produtos, total = listar_produtos(
                limite=48, ordem='recentes' if fallback == 'recentes' else None,
                **extras)
    return render_template('busca.html',
                           produtos=produtos, total=total,
                           termo=titulo, termo_q='',
                           categorias=listar_categorias(),
                           filtros=listar_filtros_planos(),
                           filtros_ativos={k: v for k, v in extras.items() if k != 'destaque'},
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/produtos')
def pag_todos_produtos():
    """Lista todos os produtos da vitrine, sem filtro de categoria."""
    extras = filtros_da_querystring(request)
    produtos, total = listar_produtos(limite=48, **extras)
    return render_template('busca.html',
                           produtos=produtos, total=total,
                           termo='Todos os produtos', termo_q='',
                           categorias=listar_categorias(),
                           filtros=listar_filtros_planos(),
                           filtros_ativos=extras,
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/novidades')
def pag_novidades():
    return _pagina_destaque('novidade', '✨ Novidades', fallback='recentes')


@app.route('/mais-vendidos')
def pag_mais_vendidos():
    return _pagina_destaque('mais_vendido', '⭐ Mais vendidos',
                            fallback='vendidos')


@app.route('/liquida-luqui')
def pag_liquida_luqui():
    """LiquidaLuqui virou parte de /promocoes — mantido só como redirect
    301 pros links antigos (Instagram, Google, WhatsApp) não morrerem."""
    return redirect('/promocoes', code=301)


@app.route('/produto/<int:pid>')
@app.route('/produto/<int:pid>-<slug>')
def produto(pid, slug=None):
    p = buscar_produto(pid)
    if not p:
        abort(404)
    # O id manda; o slug e enfeite pro buscador. Se veio sem slug (link antigo)
    # ou com o slug velho (descricao mudou no PDV), 301 pra forma canonica —
    # senao o mesmo produto existiria em varias URLs.
    canonica = url_produto(p)
    if request.path != canonica:
        return redirect(canonica, code=301)
    avals = db_execute("""SELECT a.*, c.nome AS cliente_nome
        FROM avaliacoes a LEFT JOIN clientes_site c ON c.id=a.cliente_id
        WHERE a.produto_pdv_id=%s AND a.aprovado=true
        ORDER BY a.criado_em DESC""", [pid], fetch='all') or []
    media = None
    if avals:
        media = round(sum(a['estrelas'] for a in avals) / len(avals), 1)
    # Upsell: produtos que mais aparecem em pedidos junto com esse
    rel_ids = []
    rows = db_execute("""
        SELECT pi2.produto_pdv_id, COUNT(*) AS qtd
          FROM pedido_itens pi
          JOIN pedido_itens pi2 ON pi2.pedido_id = pi.pedido_id
                                AND pi2.produto_pdv_id != pi.produto_pdv_id
         WHERE pi.produto_pdv_id = %s
         GROUP BY pi2.produto_pdv_id
         ORDER BY qtd DESC, RANDOM()
         LIMIT 8""", [pid], fetch='all') or []
    for r in rows:
        rel_ids.append(r['produto_pdv_id'])
    # Fallback: produtos da mesma categoria (departamento)
    relacionados = []
    for rid in rel_ids:
        rp = buscar_produto(rid)
        if rp:
            relacionados.append(rp)
        if len(relacionados) >= 4:
            break
    if len(relacionados) < 4 and p.get('departamento'):
        slug = p['departamento'].lower().replace(' ', '-').replace('/', '-')
        mais, _ = listar_produtos(categoria=slug, limite=8)
        for rp in mais or []:
            if rp['id'] != pid and rp['id'] not in [r['id'] for r in relacionados]:
                relacionados.append(rp)
            if len(relacionados) >= 4:
                break
    # Config de pagamento — vem das settings (mesmas usadas no checkout)
    pix_pct = float(cfg('desconto_pix_pct', '3'))
    parc_max = int(cfg('parcelamento_max', '12'))
    parc_sj = int(cfg('parcelas_sem_juros_max', '1'))
    juros_am = float(cfg('juros_parcelamento_am', '2.49'))
    preco_final = float(p.get('preco_promo') or p.get('preco_venda') or 0)
    # Calcula valor da parcela com juros pra n=parcelamento_max (price)
    # PMT = PV * (i * (1+i)^n) / ((1+i)^n - 1)
    def _parc_com_juros(pv, n, juros_pct_am):
        if n <= 0 or pv <= 0:
            return 0
        i = juros_pct_am / 100.0
        if i <= 0:
            return pv / n
        fator = (1 + i) ** n
        return pv * (i * fator) / (fator - 1)
    parcela_max_valor = _parc_com_juros(preco_final, parc_max, juros_am)
    parcela_sj_valor = preco_final / parc_sj if parc_sj > 0 else preco_final
    # Prova social baseada em dado REAL: pessoas únicas que viram esse produto
    # nos últimos 30 dias. Usado pra gerar confiança ("X pessoas viram esse
    # brinquedo recentemente"). Só mostra se >= 3 pra não parecer pouco.
    try:
        # Duas formas de path: a antiga (/produto/123) e a com slug
        # (/produto/123-boneca-...). O hifen separa — sem ele, LIKE '/produto/12%'
        # contaria as visitas do produto 1234 junto.
        row = db_execute("""SELECT COUNT(DISTINCT ip_hash) AS n
                            FROM site_visitas
                            WHERE (path = %s OR path LIKE %s)
                              AND ts > NOW() - INTERVAL '30 days'
                              AND NOT COALESCE(is_bot, false)""",
                         [f'/produto/{pid}', f'/produto/{pid}-%'], fetch='one') or {}
        visitas_30d = int(row.get('n') or 0)
    except Exception:
        visitas_30d = 0
    # priceValidUntil: o Google avisa quando falta. Data de validade do preço
    # anunciado — 1 ano à frente, já que o preço vem do PDV e é relido a cada
    # carregamento de qualquer jeito.
    preco_valido_ate = (datetime.now(SP_TZ) + timedelta(days=365)).strftime('%Y-%m-%d')
    return render_template('produto.html',
                           p=p, avaliacoes=avals, media_estrelas=media,
                           preco_valido_ate=preco_valido_ate,
                           relacionados=relacionados[:4],
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler(),
                           desconto_pix_pct=pix_pct,
                           parcelamento_max=parc_max,
                           parcelas_sem_juros_max=parc_sj,
                           parcela_sj_valor=parcela_sj_valor,
                           parcela_max_valor=parcela_max_valor,
                           juros_parcelamento_am=juros_am,
                           visitas_30d=visitas_30d)


@app.route('/api/carrinho/add', methods=['POST'])
def carrinho_add():
    d = request.get_json() or {}
    pid = int(d.get('produto_id') or 0)
    qtd = max(1, int(d.get('quantidade') or 1))
    if not pid:
        return jsonify({'erro': 'Produto inválido'}), 400
    prod = buscar_produto(pid)
    if not prod:
        return jsonify({'erro': 'Produto não encontrado'}), 404
    estoque = float(prod.get('estoque_atual') or 0)
    if estoque <= 0:
        return jsonify({'erro': 'Produto indisponível no momento'}), 400
    preco = float(prod.get('preco_promo') or prod.get('preco_venda') or 0)
    itens = carrinho_ler()
    achei = next((i for i in itens if i['produto_id'] == pid), None)
    # Não deixa qtd no carrinho ultrapassar o estoque disponível
    qtd_atual = achei['qtd'] if achei else 0
    if qtd_atual + qtd > estoque:
        return jsonify({'erro': f'Estoque insuficiente — disponível: {int(estoque)} un.'}), 400
    if achei:
        achei['qtd'] += qtd
    else:
        itens.append({
            'produto_id': pid,
            'descricao': prod.get('descricao'),
            'codigo_barras': prod.get('codigo_barras'),
            'preco': preco,
            'qtd': qtd,
            'foto_url': prod.get('foto_url'),
        })
    carrinho_salvar(itens)
    sub, qtot = carrinho_total(itens)
    return jsonify({'ok': True, 'subtotal': sub, 'qtd_total': qtot,
                    'itens': itens, 'item_subtotal': preco * qtd})


@app.route('/api/carrinho/remove', methods=['POST'])
def carrinho_remove():
    pid = int((request.get_json() or {}).get('produto_id') or 0)
    itens = [i for i in carrinho_ler() if i['produto_id'] != pid]
    carrinho_salvar(itens)
    sub, qtot = carrinho_total(itens)
    return jsonify({'ok': True, 'subtotal': sub, 'qtd_total': qtot,
                    'itens': itens})


@app.route('/api/carrinho/qtd', methods=['POST'])
def carrinho_qtd():
    d = request.get_json() or {}
    pid = int(d.get('produto_id') or 0)
    qtd = max(0, int(d.get('quantidade') or 0))
    itens = carrinho_ler()
    if qtd == 0:
        itens = [i for i in itens if i['produto_id'] != pid]
    else:
        for i in itens:
            if i['produto_id'] == pid:
                i['qtd'] = qtd
                break
    carrinho_salvar(itens)
    sub, qtot = carrinho_total(itens)
    return jsonify({'ok': True, 'subtotal': sub, 'qtd_total': qtot,
                    'itens': itens})


@app.route('/carrinho')
def carrinho_view():
    itens = carrinho_ler()
    sub, qtot = carrinho_total(itens)
    return render_template('carrinho.html',
                           itens=itens, subtotal=sub, qtd_total=qtot,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=itens)


@app.route('/checkout')
def checkout_view():
    itens = carrinho_ler()
    if not itens:
        return redirect(url_for('carrinho_view'))
    sub, _ = carrinho_total(itens)
    # Consulta saldo de pontos no PDV (se cliente logado e tem CPF)
    cli = cliente_logado()
    pontos_info = None
    if cli and cli.get('cpf'):
        pontos_info = pdv_consultar_pontos(cli['cpf'])
    resp = render_template('checkout.html',
                           itens=itens, subtotal=sub,
                           categorias=listar_categorias(),
                           cliente=cli,
                           carrinho=itens,
                           desconto_pix_pct=float(cfg('desconto_pix_pct', '3')),
                           desconto_boleto_pct=float(cfg('desconto_boleto_pct', '3')),
                           parcelamento_max=int(cfg('parcelamento_max', '12')),
                           parcelas_sem_juros_max=int(cfg('parcelas_sem_juros_max', '1')),
                           parcela_minima=float(cfg('parcela_minima', '50')),
                           juros_parcelamento_am=float(cfg('juros_parcelamento_am', '2.49')),
                           minimo_cartao=valor_minimo_para('cartao'),
                           minimo_outros=valor_minimo_para('pix'),
                           pontos_info=pontos_info)
    # Sem cache: garante que o cliente veja sempre a versao mais nova do
    # checkout (sem isso, Safari/PWA pode segurar HTML antigo por horas).
    from flask import make_response
    r = make_response(resp)
    r.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    r.headers['Pragma'] = 'no-cache'
    return r


@app.route('/api/checkout/consultar-pontos')
def checkout_consultar_pontos():
    """Consulta pontos pelo CPF informado no formulario (cliente sem login
    ou pra revalidar). Retorna saldo e valor disponivel."""
    if not rate_limit_ok('cpf_lookup', _rl_ip(), 20, 300):
        return jsonify({'erro': 'Muitas consultas. Aguarde um pouco.'}), 429
    cpf = (request.args.get('cpf') or '').strip()
    info = pdv_consultar_pontos(cpf)
    if not info:
        return jsonify({'cliente_existe': False, 'saldo': 0, 'valor_disponivel': 0})
    return jsonify(info)


@app.route('/api/checkout/buscar-cliente-cpf')
def checkout_buscar_cliente_cpf():
    """Busca cliente da loja física pelo CPF — usado pra oferecer
    pré-preenchimento de dados no checkout quando o CPF digitado é de
    alguém que já comprou na loja física. Retorna nome/contato/endereço
    + saldo do Clube. Frontend mostra um aviso 'Encontramos seu cadastro'."""
    # Este endpoint devolve nome, telefone e ENDEREÇO COMPLETO de qualquer
    # cliente da loja física a partir do CPF, sem login. Com uma lista de CPFs
    # vazada (o que não falta no Brasil) e um pool de proxies — que os golpistas
    # de 22-24/07 comprovadamente têm — dava pra enriquecer a base inteira.
    # Dois freios: por IP e por CPF. O de CPF é o que importa, porque trocar de
    # IP é barato e trocar o CPF alvo não adianta pro atacante.
    cpf = (request.args.get('cpf') or '').strip()
    cpf_digs = _so_digitos(cpf)
    if not cpf_valido(cpf_digs):
        return jsonify({'cliente_existe': False})
    if not rate_limit_ok('cpf_lookup', _rl_ip(), 8, 600):
        return jsonify({'erro': 'Muitas consultas. Aguarde um pouco.'}), 429
    if not rate_limit_ok('cpf_alvo', cpf_digs, 5, 86400):
        log.warning("buscar-cliente-cpf: CPF %s consultado demais (ip=%s)",
                    cpf_digs[:3] + '********', _rl_ip())
        return jsonify({'cliente_existe': False})
    dados = pdv_buscar_cliente_cpf(cpf)
    if not dados:
        return jsonify({'cliente_existe': False})
    return jsonify(dados)


def _busca_cep_provedores(cep):
    """Endereço do CEP, tentando mais de uma fonte.

    Em 28/07/2026 o ViaCEP parou de responder DE DENTRO do container:
    "Network is unreachable" (Errno 101). O IPv6 dele é da DigitalOcean
    (2604:a880::) e o Railway não roteia pra lá; o que está atrás da
    Cloudflare (2606:4700::) funciona normalmente — por isso Asaas e
    Pagar.me seguiram OK enquanto o autopreenchimento do checkout quebrava
    calado. Uma fonte só, hospedada fora da Cloudflare, virou ponto único
    de falha bem no meio do funil de venda.
    """
    fontes = [
        ('viacep', f'https://viacep.com.br/ws/{cep}/json/',
         lambda d: None if d.get('erro') else {
             'endereco': d.get('logradouro'), 'bairro': d.get('bairro'),
             'cidade': d.get('localidade'), 'uf': d.get('uf')}),
        ('brasilapi', f'https://brasilapi.com.br/api/cep/v1/{cep}',
         lambda d: {'endereco': d.get('street'), 'bairro': d.get('neighborhood'),
                    'cidade': d.get('city'), 'uf': d.get('state')}
         if d.get('city') else None),
        ('awesomeapi', f'https://cep.awesomeapi.com.br/json/{cep}',
         lambda d: {'endereco': d.get('address'), 'bairro': d.get('district'),
                    'cidade': d.get('city'), 'uf': d.get('state')}
         if d.get('city') else None),
    ]
    erros = []
    for nome, url, extrai in fontes:
        try:
            r = requests.get(url, headers={'User-Agent': 'LuquiShop/1.0'},
                             timeout=6)
            if r.status_code != 200:
                erros.append(f'{nome}:{r.status_code}')
                continue
            out = extrai(r.json() or {})
            if out:
                return out, nome
            erros.append(f'{nome}:nao_encontrado')
        except Exception as e:
            erros.append(f'{nome}:{type(e).__name__}')
    log.warning("CEP %s falhou em todas as fontes: %s", cep, ', '.join(erros))
    return None, ','.join(erros)


@app.route('/api/checkout/cartao-regiao')
def checkout_cartao_regiao():
    """O cartão vale pra esse CEP? Usado pela tela pra esconder a opção ANTES
    de a pessoa preencher tudo.

    Endpoint próprio (e não o /cep) porque a tela passou a consultar o ViaCEP
    direto do navegador — o backend deixou de ser chamado no caminho normal e
    o aviso de região nunca chegava ao cliente.
    """
    cep = _so_digitos(request.args.get('cep'))
    retira = (request.args.get('retira') or '') in ('1', 'true', 'sim')
    # `tipo` diz POR QUE está bloqueado — a tela mostra textos diferentes.
    # Dizer "só atendemos Cascavel" pra quem é de Cascavel, quando na verdade
    # o cartão está em manutenção, é pior que não explicar nada.
    if cfg('cartao_ativo', '1') != '1':
        return jsonify({'liberado': False, 'tipo': 'manutencao',
                        'motivo': cfg('cartao_aviso_manutencao', '')})
    if not retira and len(cep) != 8:
        return jsonify({'liberado': True, 'indefinido': True})
    try:
        lib, _, _ = cartao_liberado_para(cep, retira)
    except Exception as e:
        log.warning("cartao-regiao %s: %s", cep, e)
        return jsonify({'liberado': True, 'indefinido': True})
    info = {} if retira else cep_info(cep)
    return jsonify({'liberado': bool(lib), 'tipo': 'regiao' if not lib else '',
                    'cidade': info.get('cidade'), 'uf': info.get('uf')})


@app.route('/api/checkout/cep')
def checkout_cep():
    cep = (request.args.get('cep') or '').replace('-', '').replace('.', '')
    if len(cep) != 8 or not cep.isdigit():
        return jsonify({'erro': 'CEP inválido'}), 400
    d, fonte = _busca_cep_provedores(cep)
    if not d:
        return jsonify({'erro': 'CEP não encontrado'}), 404
    # Avisa já aqui se o cartão vale pra esse CEP, pra a pessoa escolher a
    # forma de pagamento certa antes de preencher tudo — descobrir isso só
    # no botão final é o jeito mais rápido de perder a venda.
    try:
        lib, km, _ = cartao_liberado_para(cep, False)
    except Exception:
        lib, km = True, None
    return jsonify({'ok': True, **d, 'fonte': fonte,
                    'cartao_liberado': bool(lib),
                    'distancia_km': round(km) if km is not None else None})


def _slots_entrega_local():
    """Gera horarios disponiveis pra entrega local (Cascavel).
    Regras:
    - Cliente compra ate 12:00 -> primeiro slot eh hoje 14:00.
    - Compra depois das 12:00 -> primeiro slot eh amanha (primeiro horario do dia).
    - Seg-sex: 09, 10, 11, 14, 15, 16, 17 (pula 12-13 do almoco).
    - Sab: 09, 10, 11, 12.
    - Dom: fechado.
    Retorna lista de ate ~15 slots dos proximos 7 dias.
    """
    HORARIOS = {
        0: [9, 10, 11, 14, 15, 16, 17],  # segunda
        1: [9, 10, 11, 14, 15, 16, 17],
        2: [9, 10, 11, 14, 15, 16, 17],
        3: [9, 10, 11, 14, 15, 16, 17],
        4: [9, 10, 11, 14, 15, 16, 17],  # sexta
        5: [9, 10, 11, 12],              # sabado
        6: [],                            # domingo fechado
    }
    DIA_LABEL = ['Segunda', 'Terça', 'Quarta', 'Quinta',
                 'Sexta', 'Sábado', 'Domingo']
    agora = datetime.now(SP_TZ)
    hoje = agora.date()
    # Se ja passou do meio-dia, comeca amanha
    comeca_amanha = agora.hour >= 12
    slots = []
    for delta in range(0, 8):
        dia = hoje + timedelta(days=delta)
        dow = dia.weekday()
        horas = HORARIOS.get(dow, [])
        if not horas:
            continue
        for h in horas:
            if delta == 0:
                if comeca_amanha:
                    continue
                # Pra hoje, so aceita slots a partir das 14h (tempo pra preparar)
                if h < 14:
                    continue
            if delta == 0:
                label = f"Hoje ({dia.strftime('%d/%m')}) entre {h}h e {h+1}h"
            elif delta == 1:
                label = f"Amanhã ({dia.strftime('%d/%m')}) entre {h}h e {h+1}h"
            else:
                label = f"{DIA_LABEL[dow]} ({dia.strftime('%d/%m')}) entre {h}h e {h+1}h"
            slots.append({
                'value': f"{dia.strftime('%Y-%m-%d')} {h:02d}:00",
                'label': label,
            })
        if len(slots) >= 15:
            break
    return slots[:15]


@app.route('/api/checkout/frete')
def checkout_frete():
    """Cota o frete pelo Melhor Envio. Cascavel (PR) tem entrega local
    com VALOR FIXO (configurável em frete_fixo_cascavel, default 10).
    Demais cidades — inclusive Toledo — usam cotação real do ME."""
    cidade = (request.args.get('cidade') or '').strip().lower()
    uf = (request.args.get('uf') or '').upper()
    cep = (request.args.get('cep') or '').strip()
    opcoes = []
    # Retirar na loja — sempre primeira opção quando ativa
    if cfg('retirada_loja_ativa', '1') == '1':
        opcoes.append({
            'id': 'RETIRA',
            'servico': 'Retirar na loja',
            'valor': 0,
            'prazo': 'Pronto em até '+cfg('loja_tempo_separacao_min', '30')+' min',
            'endereco': cfg('loja_endereco_completo', ''),
            'horario': cfg('loja_horario_funcionamento', ''),
            'tempo_min': cfg('loja_tempo_separacao_min', '30'),
        })
    # Cidades com frete fixo (entrega local). Default: só Cascavel.
    # Toledo SAIU — agora cota pelo ME normalmente.
    try:
        fixo_valor = float(cfg('frete_fixo_cascavel', '10') or 10)
    except (TypeError, ValueError):
        fixo_valor = 10.0
    cidades_fixo = [c.strip().lower() for c in
                    cfg('frete_fixo_cidades', 'Cascavel').split(',') if c.strip()]
    uf_fixo = cfg('frete_fixo_uf', 'PR')
    if uf == uf_fixo and cidade in cidades_fixo:
        slots = _slots_entrega_local()
        opcoes.append({'servico': 'Entrega Luqui (local)',
                       'valor': fixo_valor,
                       'prazo': slots[0]['label'] if slots else 'Agendar',
                       'id': 'LOCAL',
                       'agendamento': slots})
    # Tenta Melhor Envio se tiver CEP + carrinho + conexão
    itens_sess = carrinho_ler() or []
    if cep and itens_sess and cfg('me_access_token'):
        # Enriquece com produto pra ter dimensão
        itens_full = []
        for it in itens_sess:
            try:
                p = buscar_produto(it.get('produto_id')) or {}
            except Exception:
                p = {}
            itens_full.append({
                'produto': p,
                'qtd':     it.get('qtd') or 1,
                'preco':   it.get('preco') or 0,
            })
        try:
            ops_me = me_cotar(cep, itens_full)
            for o in ops_me:
                opcoes.append({
                    'id':      o.get('id'),
                    'servico': o.get('servico'),
                    'valor':   o.get('valor'),
                    'prazo':   o.get('prazo'),
                })
        except Exception as e:
            log.warning("ME cotação falhou (fallback hardcode): %s", e)
    # Fallback hardcode (sem ME ou sem CEP) — sempre devolve algo.
    # RETIRA/LOCAL nao contam como "opcao de envio" — sao complementares.
    # Sem fallback, cliente fora de Cascavel ficava preso so com Retirar na loja.
    if not [o for o in opcoes if (o.get('id') or '') not in ('LOCAL', 'RETIRA')]:
        if uf == 'PR':
            opcoes += [
                {'servico': 'PAC',   'valor': 24.90, 'prazo': '3-5 dias úteis'},
                {'servico': 'SEDEX', 'valor': 38.90, 'prazo': '2-3 dias úteis'},
            ]
        else:
            opcoes += [
                {'servico': 'PAC',   'valor': 39.90, 'prazo': '5-9 dias úteis'},
                {'servico': 'SEDEX', 'valor': 62.90, 'prazo': '2-4 dias úteis'},
            ]
    return jsonify({'opcoes': opcoes})


# ─── Clube de assinatura ──────────────────────────────────────────────────────
@app.route('/clube')
def clube_view():
    planos = db_execute(
        "SELECT * FROM clube_planos WHERE ativo ORDER BY ordem",
        fetch='all') or []
    return render_template('clube.html',
                           planos=planos,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/clube/assinatura/<int:aid>/pagamento')
def clube_pagamento(aid):
    c = cliente_logado()
    if not c:
        return redirect(url_for('login', next=request.path))
    ass = db_execute("""SELECT a.*, p.nome AS plano_nome, p.beneficios_json,
                               p.preco_mensal
                        FROM clube_assinaturas a
                        JOIN clube_planos p ON p.id=a.plano_id
                        WHERE a.id=%s AND a.cliente_id=%s""",
                     [aid, c['id']], fetch='one')
    if not ass:
        abort(404)
    # Lazy-fetch do QR PIX se faltou na criação (assinatura antiga)
    if (ass.get('forma_pagto') == 'pix'
        and ass.get('asaas_cobranca_id')
        and not ass.get('asaas_pix_qr_image')):
        try:
            pix = asaas_buscar_pix_qr(ass['asaas_cobranca_id']) or {}
            img = pix.get('encodedImage', '')
            payload = pix.get('payload', '')
            if img or payload:
                db_execute("""UPDATE clube_assinaturas
                              SET asaas_pix_qr_image=%s, asaas_pix_qrcode=%s
                              WHERE id=%s""", [img, payload, aid])
                ass['asaas_pix_qr_image'] = img
                ass['asaas_pix_qrcode'] = payload
        except Exception as e:
            log.warning("lazy-fetch pix clube %s: %s", aid, e)
    return render_template('clube_assinatura_pagamento.html',
                           a=ass,
                           categorias=listar_categorias(),
                           cliente=c,
                           carrinho=carrinho_ler())


@app.route('/api/clube/assinatura/<int:aid>/status')
def clube_assinatura_status(aid):
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'login'}), 401
    a = db_execute("""SELECT status FROM clube_assinaturas
                      WHERE id=%s AND cliente_id=%s""",
                   [aid, c['id']], fetch='one')
    if not a:
        return jsonify({'erro': 'não encontrada'}), 404
    return jsonify({'status': a['status']})


@app.route('/clube/assinar/<slug>')
def clube_assinar(slug):
    plano = db_execute("SELECT * FROM clube_planos WHERE slug=%s AND ativo",
                       [slug], fetch='one')
    if not plano:
        abort(404)
    c = cliente_logado()
    if not c:
        return redirect(url_for('login', next=request.path))
    # Já tem assinatura ativa nesse plano?
    ja = db_execute("""SELECT * FROM clube_assinaturas
                       WHERE cliente_id=%s AND status IN ('ativa','aguardando_pagto')
                       ORDER BY id DESC LIMIT 1""",
                    [c['id']], fetch='one')
    return render_template('clube_assinar.html',
                           plano=plano,
                           ja_assinou=ja,
                           categorias=listar_categorias(),
                           cliente=c,
                           carrinho=carrinho_ler())


@app.route('/api/clube/assinar', methods=['POST'])
def clube_assinar_post():
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'Faça login primeiro'}), 401
    d = request.get_json() or {}
    slug = d.get('plano_slug')
    forma = d.get('forma_pagto') or 'pix'  # pix, boleto, cartao
    if forma not in ('pix', 'boleto', 'cartao'):
        return jsonify({'erro': 'Forma de pagamento inválida'}), 400
    plano = db_execute("SELECT * FROM clube_planos WHERE slug=%s AND ativo",
                       [slug], fetch='one')
    if not plano:
        return jsonify({'erro': 'Plano inválido'}), 404
    ja = db_execute("""SELECT id FROM clube_assinaturas
                       WHERE cliente_id=%s AND status='ativa'""",
                    [c['id']], fetch='one')
    if ja:
        return jsonify({'erro': 'Você já tem uma assinatura ativa. '
                                 'Cancele a atual antes de trocar.'}), 400
    if not c.get('cpf'):
        return jsonify({'erro': 'Preencha seu CPF antes de assinar'}), 400
    # Validação de endereço de envio (obrigatório — clube manda brinquedo todo mês)
    obrig = ['cep', 'endereco', 'numero', 'bairro', 'cidade', 'uf']
    for k in obrig:
        if not (d.get(k) or '').strip():
            return jsonify({'erro': f'Campo {k} obrigatório'}), 400
    cep = (d.get('cep') or '').strip()
    endereco = (d.get('endereco') or '').strip()
    numero = (d.get('numero') or '').strip()
    complemento = (d.get('complemento') or '').strip() or None
    bairro = (d.get('bairro') or '').strip()
    cidade = (d.get('cidade') or '').strip()
    uf = (d.get('uf') or '').strip().upper()
    valor = float(plano['preco_mensal'])
    # Atualiza endereço do cliente (auto-preenche próximo checkout)
    try:
        db_execute("""UPDATE clientes_site SET
                        cep=%s, endereco=%s, numero=%s, complemento=%s,
                        bairro=%s, cidade=%s, uf=%s
                      WHERE id=%s""",
                   [cep, endereco, numero, complemento,
                    bairro, cidade, uf, c['id']])
    except Exception as e:
        log.warning("atualizar endereço cliente %s: %s", c['id'], e)
    # Cria assinatura local
    nova = db_execute("""
        INSERT INTO clube_assinaturas
            (cliente_id, plano_id, status, proximo_envio,
             forma_pagto, valor, cep, endereco, numero, complemento,
             bairro, cidade, uf)
        VALUES (%s,%s,'aguardando_pagto', CURRENT_DATE + INTERVAL '7 days',
                %s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id""",
        [c['id'], plano['id'], forma, valor,
         cep, endereco, numero, complemento, bairro, cidade, uf],
        fetch='one')
    aid = nova['id']
    descricao = f'Clube Luqui — {plano["nome"]}'
    externa = f'clube-{aid}'
    customer_id = asaas_criar_customer(c['nome'], c['email'],
                                       c['cpf'], c.get('telefone'))
    if not customer_id:
        db_execute("UPDATE clube_assinaturas SET status='erro_asaas' WHERE id=%s",
                   [aid])
        return jsonify({'erro': 'Falha ao criar cliente no gateway. '
                                 'Chama no WhatsApp pra ativar manualmente.'}), 502

    # ── CARTÃO: checkout transparente (dados inline, primeira parcela já é cobrada)
    if forma == 'cartao':
        num = ''.join(ch for ch in (d.get('cc_numero') or '') if ch.isdigit())
        mes = (d.get('cc_validade_mes') or '').strip().zfill(2)
        ano = (d.get('cc_validade_ano') or '').strip()
        if len(ano) == 2:
            ano = '20' + ano
        ccv = ''.join(ch for ch in (d.get('cc_ccv') or '') if ch.isdigit())
        holder_nome = (d.get('cc_titular_nome') or '').strip().upper()[:80]
        holder_cpf = ''.join(ch for ch in (d.get('cc_titular_cpf') or '')
                             if ch.isdigit())
        if not (12 <= len(num) <= 19):
            return jsonify({'erro': 'Número do cartão inválido'}), 400
        if not (3 <= len(ccv) <= 4):
            return jsonify({'erro': 'CVV inválido'}), 400
        try:
            if not (1 <= int(mes) <= 12) or int(ano) < datetime.now().year:
                raise ValueError
        except ValueError:
            return jsonify({'erro': 'Validade do cartão inválida'}), 400
        if len(holder_nome) < 3:
            return jsonify({'erro': 'Nome do titular obrigatório'}), 400
        if len(holder_cpf) not in (11, 14):
            return jsonify({'erro': 'CPF/CNPJ do titular obrigatório'}), 400
        cc = {'holderName': holder_nome, 'number': num,
              'expiryMonth': mes, 'expiryYear': ano, 'ccv': ccv}
        holder = {
            'name': holder_nome, 'email': c['email'], 'cpfCnpj': holder_cpf,
            'postalCode': ''.join(ch for ch in cep if ch.isdigit()) or '00000000',
            'addressNumber': (numero or 'S/N')[:10],
            'phone': ''.join(ch for ch in (c.get('telefone') or '')
                             if ch.isdigit())[:11] or '0000000000',
        }
        remote_ip = (request.headers.get('X-Forwarded-For')
                     or request.remote_addr or '0.0.0.0').split(',')[0].strip()
        code, resp = asaas_criar_assinatura_cartao(
            customer_id, valor, descricao, externa, cc, holder, remote_ip)
        if code not in (200, 201):
            msg = 'Não foi possível processar o pagamento'
            try:
                errs = (resp or {}).get('errors') or []
                if errs and errs[0].get('description'):
                    msg = errs[0]['description']
            except Exception:
                pass
            db_execute("UPDATE clube_assinaturas SET status='erro_asaas' WHERE id=%s",
                       [aid])
            return jsonify({'erro': msg}), 402
        sub_id = resp.get('id')
        db_execute("""UPDATE clube_assinaturas SET asaas_assinatura_id=%s,
                                                    status='ativa', pago_em=NOW()
                      WHERE id=%s""", [sub_id, aid])
        return jsonify({'ok': True, 'assinatura_id': aid, 'status': 'pago',
                        'pagamento_url': f'/clube/assinatura/{aid}/pagamento'})

    # ── PIX / BOLETO: cria subscription e mostra QR/linha digitável da 1ª cobrança
    billing = 'PIX' if forma == 'pix' else 'BOLETO'
    sub = asaas_criar_assinatura(customer_id, valor, descricao,
                                 billing_type=billing, externa_ref=externa)
    if not sub:
        db_execute("UPDATE clube_assinaturas SET status='erro_asaas' WHERE id=%s",
                   [aid])
        return jsonify({'erro': 'Falha ao criar assinatura no Asaas'}), 502
    sub_id = sub.get('id')
    cob_id, link = None, ''
    pix_payload, pix_image = '', ''
    boleto_url, boleto_barcode = None, None
    try:
        r = requests.get(f'{ASAAS_BASE}/subscriptions/{sub_id}/payments',
                         headers=_asaas_headers(), timeout=10)
        if r.status_code == 200:
            payments = (r.json().get('data') or [])
            if payments:
                first = payments[0]
                cob_id = first.get('id')
                link = first.get('invoiceUrl') or ''
                if forma == 'pix':
                    pix = asaas_buscar_pix_qr(cob_id) or {}
                    pix_payload = pix.get('payload', '')
                    pix_image = pix.get('encodedImage', '')
                else:
                    boleto_url = first.get('bankSlipUrl')
                    info = asaas_buscar_boleto_info(cob_id) or {}
                    boleto_barcode = info.get('identificationField') or ''
    except Exception as e:
        log.error("buscar payments da subscription: %s", e)
    db_execute("""UPDATE clube_assinaturas
                  SET asaas_assinatura_id=%s, asaas_cobranca_id=%s, asaas_link=%s,
                      asaas_pix_qrcode=%s, asaas_pix_qr_image=%s,
                      asaas_boleto_url=%s, asaas_boleto_barcode=%s
                  WHERE id=%s""",
               [sub_id, cob_id, link, pix_payload, pix_image,
                boleto_url, boleto_barcode, aid])
    return jsonify({'ok': True, 'assinatura_id': aid,
                    'pagamento_url': f'/clube/assinatura/{aid}/pagamento'})


@app.route('/api/clube/pausar', methods=['POST'])
def clube_pausar():
    """Marca a assinatura como 'pausada' até a próxima data informada (default +30d).
    Asaas continua a cobrança porque o controle é nosso — quando reativar, o
    próximo envio é recalculado."""
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'Faça login'}), 401
    ass = db_execute("""SELECT * FROM clube_assinaturas
                        WHERE cliente_id=%s AND status='ativa'
                        ORDER BY id DESC LIMIT 1""",
                     [c['id']], fetch='one')
    if not ass:
        return jsonify({'erro': 'Sem assinatura ativa'}), 404
    db_execute("""UPDATE clube_assinaturas
                  SET status='pausada',
                      proximo_envio=CURRENT_DATE + INTERVAL '30 days'
                  WHERE id=%s""", [ass['id']])
    return jsonify({'ok': True})


@app.route('/api/clube/reativar', methods=['POST'])
def clube_reativar():
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'Faça login'}), 401
    db_execute("""UPDATE clube_assinaturas SET status='ativa',
                  proximo_envio=CURRENT_DATE + INTERVAL '7 days'
                  WHERE cliente_id=%s AND status='pausada'""", [c['id']])
    return jsonify({'ok': True})


@app.route('/api/clube/trocar-plano', methods=['POST'])
def clube_trocar_plano():
    """Troca o plano da assinatura ativa: cancela no Asaas, cria nova
    subscription com o plano novo. Não duplica cobrança porque o cancel é
    pre-pago (Asaas cobra prorata)."""
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'Faça login'}), 401
    novo_slug = (request.get_json() or {}).get('plano_slug')
    plano = db_execute("SELECT * FROM clube_planos WHERE slug=%s AND ativo",
                       [novo_slug], fetch='one')
    if not plano:
        return jsonify({'erro': 'Plano inválido'}), 404
    atual = db_execute("""SELECT * FROM clube_assinaturas
                          WHERE cliente_id=%s AND status IN ('ativa','pausada')
                          ORDER BY id DESC LIMIT 1""",
                       [c['id']], fetch='one')
    if not atual:
        return jsonify({'erro': 'Sem assinatura ativa pra trocar'}), 404
    if atual['plano_id'] == plano['id']:
        return jsonify({'erro': 'Você já está nesse plano'}), 400
    # Cancela a antiga
    if atual.get('asaas_assinatura_id'):
        asaas_cancelar_assinatura(atual['asaas_assinatura_id'])
    db_execute("""UPDATE clube_assinaturas SET status='cancelada',
                  cancelada_em=NOW() WHERE id=%s""", [atual['id']])
    # Cria a nova
    customer_id = asaas_criar_customer(c['nome'], c['email'],
                                       c['cpf'], c.get('telefone'))
    if not customer_id:
        return jsonify({'erro': 'Falha no gateway'}), 502
    sub = asaas_criar_assinatura(
        customer_id, float(plano['preco_mensal']),
        descricao=f'Clube Luqui — {plano["nome"]}',
        billing_type='PIX',
        externa_ref=f'clube-pending',
    )
    if not sub:
        return jsonify({'erro': 'Falha ao criar assinatura'}), 502
    nv = db_execute("""INSERT INTO clube_assinaturas
        (cliente_id, plano_id, status, proximo_envio, asaas_assinatura_id)
        VALUES (%s,%s,'aguardando_pagto', CURRENT_DATE + INTERVAL '7 days', %s)
        RETURNING id""", [c['id'], plano['id'], sub.get('id')], fetch='one')
    # Atualiza ref do Asaas pro id correto
    return jsonify({'ok': True, 'assinatura_id': nv['id']})


@app.route('/api/clube/cancelar', methods=['POST'])
def clube_cancelar():
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'Faça login'}), 401
    ass = db_execute("""SELECT * FROM clube_assinaturas
                        WHERE cliente_id=%s AND status='ativa'
                        ORDER BY id DESC LIMIT 1""",
                     [c['id']], fetch='one')
    if not ass:
        return jsonify({'erro': 'Sem assinatura ativa'}), 404
    if ass.get('asaas_assinatura_id'):
        asaas_cancelar_assinatura(ass['asaas_assinatura_id'])
    db_execute("""UPDATE clube_assinaturas SET status='cancelada',
                  cancelada_em=NOW() WHERE id=%s""", [ass['id']])
    return jsonify({'ok': True})


# ─── Login/cadastro do cliente ────────────────────────────────────────────────
# ─── OAuth Google ────────────────────────────────────────────────────────────
# Login com conta Google. Setup:
# 1. Google Cloud Console → APIs & Services → Credentials → Create OAuth client ID
# 2. Tipo: Web application
# 3. Authorized redirect URI: https://www.luquibrinquedos.com.br/auth/google/callback
# 4. Pegar Client ID + Client Secret, colocar em GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET
def adotar_pedidos_convidado(cliente_id, email, cpf=None, email_verificado=False):
    """Liga pedidos feitos como visitante a uma conta e completa o cadastro.

    O checkout nao exige login, entao o pedido nasce com cliente_id NULL. Quem
    comprava e SO DEPOIS criava conta ficava com "Meus pedidos" vazio e cadastro
    em branco -- foi o que aconteceu com o pedido #35 (R$ 1.060,77): o cliente
    entrou no /minha-conta 4 min depois de pagar e nao viu compra nenhuma.

    Casamento por e-mail so vale quando o e-mail foi VERIFICADO (login Google).
    O cadastro por e-mail/senha nao confirma o endereco, entao alguem podia se
    registrar com o e-mail de alguem que comprou como visitante e herdar CPF,
    telefone e endereco do outro. Nesse caso exige tambem o CPF bater.
    """
    email = (email or '').strip().lower()
    cpf = ''.join(c for c in (cpf or '') if c.isdigit())
    if not email:
        return 0
    if email_verificado:
        cond, params = "LOWER(email)=%s", [email]
    elif len(cpf) == 11:
        cond, params = ("LOWER(email)=%s AND "
                        "regexp_replace(COALESCE(cpf,''), '\\D', '', 'g')=%s",
                        [email, cpf])
    else:
        return 0
    try:
        pedidos = db_execute(
            f"SELECT * FROM pedidos WHERE cliente_id IS NULL AND {cond} "
            f"ORDER BY criado_em DESC", params, fetch='all') or []
        if not pedidos:
            return 0
        db_execute(f"UPDATE pedidos SET cliente_id=%s "
                   f"WHERE cliente_id IS NULL AND {cond}",
                   [cliente_id] + params)
        # Completa o cadastro pelo pedido mais recente, sem sobrescrever o que
        # o cliente ja tenha preenchido na conta.
        p = pedidos[0]
        campos = {'cpf': p.get('cpf'), 'telefone': p.get('telefone'),
                  'cep': p.get('cep'), 'endereco': p.get('endereco'),
                  'numero': p.get('numero'), 'complemento': p.get('complemento'),
                  'bairro': p.get('bairro'), 'cidade': p.get('cidade'),
                  'uf': p.get('uf')}
        campos = {k: v for k, v in campos.items() if (v or '').strip()}
        if campos:
            sets = ', '.join(f"{k} = COALESCE(NULLIF({k}, ''), %s)" for k in campos)
            db_execute(f"UPDATE clientes_site SET {sets} WHERE id=%s",
                       list(campos.values()) + [cliente_id])
        log.info("conta %s adotou %d pedido(s) de visitante (%s)",
                 cliente_id, len(pedidos), email)
        return len(pedidos)
    except Exception as e:
        # Nunca bloqueia o login por causa disso.
        log.warning("adotar_pedidos_convidado(%s): %s", cliente_id, e)
        return 0


GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
GOOGLE_OAUTH_HABILITADO = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
app.jinja_env.globals['GOOGLE_OAUTH_HABILITADO'] = GOOGLE_OAUTH_HABILITADO


@app.route('/auth/google/start')
def auth_google_start():
    """Inicia o fluxo OAuth: gera state, redireciona pro Google."""
    if not GOOGLE_OAUTH_HABILITADO:
        return 'Login com Google não configurado', 503
    state = secrets.token_urlsafe(24)
    session['google_oauth_state'] = state
    next_url = request.args.get('next') or url_for('home')
    session['google_oauth_next'] = next_url
    redirect_uri = url_for('auth_google_callback', _external=True, _scheme='https')
    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'access_type': 'online',
        'prompt': 'select_account',
    }
    import urllib.parse
    url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode(params)
    return redirect(url)


@app.route('/auth/google/callback')
def auth_google_callback():
    """Volta do Google com code + state. Troca code por token, busca perfil,
    loga ou cria cliente automaticamente."""
    if not GOOGLE_OAUTH_HABILITADO:
        return 'Login com Google não configurado', 503
    code = request.args.get('code')
    state = request.args.get('state')
    if not code or not state or state != session.pop('google_oauth_state', None):
        return render_template('login.html',
                               erro='Sessão expirada ou inválida — tente entrar de novo.',
                               categorias=listar_categorias(),
                               carrinho=carrinho_ler()), 400
    next_url = session.pop('google_oauth_next', None) or url_for('home')
    redirect_uri = url_for('auth_google_callback', _external=True, _scheme='https')
    # Troca code por access token
    try:
        tok = requests.post('https://oauth2.googleapis.com/token', data={
            'code': code,
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        }, timeout=15).json()
        access_token = tok.get('access_token')
        if not access_token:
            raise RuntimeError(f"token error: {tok.get('error_description') or tok}")
        # Busca perfil
        perfil = requests.get('https://www.googleapis.com/oauth2/v3/userinfo',
                              headers={'Authorization': f'Bearer {access_token}'},
                              timeout=15).json()
        sub = perfil.get('sub')
        email = (perfil.get('email') or '').strip().lower()
        nome = (perfil.get('name') or perfil.get('given_name') or 'Cliente').strip()
        foto = perfil.get('picture') or None
        if not sub or not email:
            raise RuntimeError('perfil sem sub/email')
    except Exception as e:
        print(f'[OAuth Google] erro: {e}', flush=True)
        return render_template('login.html',
                               erro='Falha ao conectar com o Google. Tente de novo.',
                               categorias=listar_categorias(),
                               carrinho=carrinho_ler()), 502
    # Acha cliente: por google_sub primeiro, senao por email
    c = db_execute("SELECT * FROM clientes_site WHERE google_sub=%s", [sub], fetch='one')
    if not c:
        c = db_execute("SELECT * FROM clientes_site WHERE LOWER(email)=%s",
                       [email], fetch='one')
        if c:
            # Cliente ja existia com esse email (cadastro tradicional) — vincula Google
            db_execute("UPDATE clientes_site SET google_sub=%s, foto_url=COALESCE(foto_url,%s) "
                       "WHERE id=%s", [sub, foto, c['id']])
    if not c:
        # Cria novo cliente sem senha (so OAuth)
        nv = db_execute(
            "INSERT INTO clientes_site (nome, email, google_sub, foto_url) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            [nome[:160], email[:160], sub, foto], fetch='one')
        c = {'id': nv['id']}
    session.permanent = True
    session['cliente_id'] = c['id']
    # E-mail vem verificado pelo Google — pode casar pedidos so pelo e-mail.
    adotar_pedidos_convidado(c['id'], email, email_verificado=True)
    return redirect(next_url)


@app.route('/login', methods=['GET', 'POST'])
def login():
    erro = None
    if request.method == 'POST':
        if not rate_limit_ok('login_cliente', _rl_ip(), 12, 900):
            return render_template(
                'login.html', erro='Muitas tentativas. Aguarde alguns minutos.',
                categorias=listar_categorias(), carrinho=carrinho_ler()), 429
        email = (request.form.get('email') or '').strip().lower()
        senha = request.form.get('senha') or ''
        c = db_execute("SELECT * FROM clientes_site WHERE LOWER(email)=%s",
                       [email], fetch='one')
        if c and check_password_hash(c['senha_hash'], senha):
            session.permanent = True
            session['cliente_id'] = c['id']
            adotar_pedidos_convidado(c['id'], email, cpf=c.get('cpf'))
            return redirect(request.args.get('next') or url_for('home'))
        erro = 'E-mail ou senha incorretos.'
    return render_template('login.html', erro=erro,
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


@app.route('/cadastrar', methods=['GET', 'POST'])
def cadastrar():
    erro = None
    if request.method == 'POST':
        d = {k: (request.form.get(k) or '').strip() for k in
             ('nome', 'email', 'senha', 'telefone', 'cpf', 'data_nascimento')}
        cpf_digs = ''.join(c for c in d['cpf'] if c.isdigit())
        tel_digs = ''.join(c for c in d['telefone'] if c.isdigit())
        if not d['nome'] or not d['email'] or len(d['senha']) < 6:
            erro = 'Preencha nome, e-mail e senha (mín 6 caracteres).'
        elif not tel_digs or len(tel_digs) < 10:
            erro = 'Informe o WhatsApp com DDD.'
        elif len(cpf_digs) != 11:
            erro = 'CPF é obrigatório (precisa ter 11 dígitos).'
        elif db_execute("SELECT 1 FROM clientes_site WHERE LOWER(email)=%s",
                        [d['email'].lower()], fetch='one'):
            erro = 'Esse e-mail já está cadastrado. Faça login.'
        elif db_execute("SELECT 1 FROM clientes_site WHERE cpf=%s",
                        [cpf_digs], fetch='one'):
            erro = 'Esse CPF já está cadastrado. Faça login com o e-mail vinculado.'
        else:
            nv = db_execute(
                """INSERT INTO clientes_site
                   (nome, email, senha_hash, telefone, cpf, data_nascimento)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                [d['nome'], d['email'].lower(),
                 generate_password_hash(d['senha']),
                 tel_digs, cpf_digs,
                 d['data_nascimento'] or None],
                fetch='one')
            session.permanent = True
            session['cliente_id'] = nv['id']
            adotar_pedidos_convidado(nv['id'], d['email'].lower(), cpf=cpf_digs)
            # Respeita ?next= (ex: vindo do checkout, volta pra la com
            # os dados ja preenchidos pelo cadastro recem-criado)
            return redirect(request.args.get('next') or url_for('home'))
    return render_template('cadastrar.html', erro=erro,
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


import secrets


@app.route('/esqueci-senha', methods=['GET', 'POST'])
def esqueci_senha():
    msg = None
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        c = db_execute("SELECT id, nome FROM clientes_site WHERE LOWER(email)=%s",
                       [email], fetch='one')
        if c:
            nova = secrets.token_urlsafe(8)[:10]
            db_execute("UPDATE clientes_site SET senha_hash=%s WHERE id=%s",
                       [generate_password_hash(nova), c['id']])
            enviar_email(email, 'Sua nova senha — Luqui Brinquedos',
                f"""<p>Oi {c['nome'].split()[0]}! 💛</p>
<p>Recebemos um pedido pra resetar sua senha.</p>
<p>Sua <b>nova senha temporária</b> é: <code style='background:#FEF3C7;padding:4px 10px;border-radius:4px;font-size:18px'>{nova}</code></p>
<p>Entre em <a href='https://www.luquibrinquedos.com.br/login'>luquibrinquedos.com.br/login</a> e troque pela senha que preferir em "Minha conta".</p>
<p>Não foi você? Avisa a gente no WhatsApp (45) 99111-9800.</p>""")
        msg = ('Se esse e-mail tem cadastro, enviamos uma senha temporária. '
               'Veja seu e-mail (inclusive a caixa de spam).')
    return render_template('esqueci_senha.html', msg=msg,
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


@app.route('/api/minha-conta/trocar-senha', methods=['POST'])
def trocar_senha():
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'Faça login'}), 401
    d = request.get_json() or {}
    atual = d.get('senha_atual') or ''
    nova = d.get('senha_nova') or ''
    if not check_password_hash(c['senha_hash'], atual):
        return jsonify({'erro': 'Senha atual incorreta'}), 400
    if len(nova) < 6:
        return jsonify({'erro': 'Senha nova precisa de pelo menos 6 caracteres'}), 400
    db_execute("UPDATE clientes_site SET senha_hash=%s WHERE id=%s",
               [generate_password_hash(nova), c['id']])
    return jsonify({'ok': True})


@app.route('/sair')
def sair():
    session.pop('cliente_id', None)
    return redirect(url_for('home'))


@app.route('/api/wishlist/toggle', methods=['POST'])
def wishlist_toggle():
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'login', 'precisa_login': True}), 401
    pid = int((request.get_json() or {}).get('produto_id') or 0)
    if not pid:
        return jsonify({'erro': 'Sem produto'}), 400
    ja = db_execute("""SELECT id FROM wishlist
                       WHERE cliente_id=%s AND produto_pdv_id=%s""",
                    [c['id'], pid], fetch='one')
    if ja:
        db_execute("DELETE FROM wishlist WHERE id=%s", [ja['id']])
        return jsonify({'ok': True, 'favorito': False})
    db_execute("""INSERT INTO wishlist (cliente_id, produto_pdv_id)
                  VALUES (%s,%s) ON CONFLICT DO NOTHING""", [c['id'], pid])
    return jsonify({'ok': True, 'favorito': True})


@app.route('/favoritos')
def favoritos():
    c = cliente_logado()
    if not c:
        return redirect(url_for('login', next=request.path))
    rows = db_execute("""SELECT produto_pdv_id FROM wishlist
                         WHERE cliente_id=%s ORDER BY criado_em DESC""",
                      [c['id']], fetch='all') or []
    produtos = []
    for r in rows:
        p = buscar_produto(r['produto_pdv_id'])
        if p:
            produtos.append(p)
    return render_template('favoritos.html', produtos=produtos,
                           categorias=listar_categorias(),
                           cliente=c, carrinho=carrinho_ler())


def wishlist_ids():
    """IDs dos produtos favoritos do cliente logado (ou set vazio)."""
    c = cliente_logado()
    if not c:
        return set()
    rows = db_execute("""SELECT produto_pdv_id FROM wishlist
                         WHERE cliente_id=%s""", [c['id']], fetch='all') or []
    return {r['produto_pdv_id'] for r in rows}


app.jinja_env.globals['wishlist_ids'] = wishlist_ids


@app.route('/minha-conta')
def minha_conta():
    c = cliente_logado()
    if not c:
        return redirect(url_for('login', next=request.path))
    pedidos = db_execute(
        "SELECT * FROM pedidos WHERE cliente_id=%s ORDER BY criado_em DESC",
        [c['id']], fetch='all') or []
    assinatura = db_execute(
        """SELECT a.*, p.nome AS plano_nome, p.slug AS plano_slug, p.preco_mensal
           FROM clube_assinaturas a JOIN clube_planos p ON p.id=a.plano_id
           WHERE a.cliente_id=%s AND a.status IN ('ativa','aguardando_pagto','pausada')
           ORDER BY a.id DESC LIMIT 1""",
        [c['id']], fetch='one')
    envios = []
    if assinatura:
        envios = db_execute(
            """SELECT * FROM clube_envios
               WHERE assinatura_id=%s ORDER BY enviado_em DESC LIMIT 12""",
            [assinatura['id']], fetch='all') or []
    planos = db_execute(
        "SELECT * FROM clube_planos WHERE ativo ORDER BY ordem",
        fetch='all') or []
    pontos_info = pdv_consultar_pontos(c.get('cpf')) if c.get('cpf') else None
    return render_template('minha_conta.html',
                           cliente=c, pedidos=pedidos, assinatura=assinatura,
                           envios=envios, planos=planos,
                           pontos_info=pontos_info,
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


@app.route('/lista-aniversario')
def pag_lista_aniversario_info():
    """Landing page explicando o que e e CTA pra criar/ver listas."""
    c = cliente_logado()
    if c:
        return redirect(url_for('minhas_listas'))
    return render_template('lista_aniversario_landing.html',
                           cliente=None,
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


# ─── Lista de aniversario (wishlist publica) ─────────────────────────────────
def _lista_carregar_itens(lista_id):
    """Carrega itens da lista enriquecidos com dados de produto do PDV."""
    rows = db_execute("""
        SELECT id, produto_pdv_id, qtd, comprado_por_nome, pedido_id, comprado_em
        FROM lista_aniversario_itens
        WHERE lista_id=%s ORDER BY criado_em DESC""",
        [lista_id], fetch='all') or []
    itens = []
    for r in rows:
        try:
            p = buscar_produto(r['produto_pdv_id']) or {}
        except Exception:
            p = {}
        itens.append({
            **dict(r),
            'descricao': p.get('descricao') or 'Produto',
            'preco_venda': float(p.get('preco_venda') or 0),
            'preco_promo': p.get('preco_promo'),
            'foto_url': p.get('foto_url'),
            'estoque_atual': float(p.get('estoque_atual') or 0),
        })
    return itens


@app.route('/minhas-listas')
def minhas_listas():
    c = cliente_logado()
    if not c:
        return redirect(url_for('login', next=request.path))
    listas = db_execute(
        """SELECT l.*,
                  (SELECT COUNT(*) FROM lista_aniversario_itens
                   WHERE lista_id=l.id) AS qtd_itens,
                  (SELECT COUNT(*) FROM lista_aniversario_itens
                   WHERE lista_id=l.id AND comprado_em IS NOT NULL) AS qtd_comprados
           FROM listas_aniversario l
           WHERE cliente_id=%s AND ativo
           ORDER BY criado_em DESC""",
        [c['id']], fetch='all') or []
    return render_template('minhas_listas.html',
                           cliente=c, listas=listas,
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


@app.route('/minhas-listas/criar', methods=['POST'])
def lista_criar():
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'Faça login primeiro'}), 401
    d = request.get_json() or {}
    nome = (d.get('nome_crianca') or '').strip()[:120]
    if not nome:
        return jsonify({'erro': 'Informe o nome da criança'}), 400
    try:
        idade = int(d.get('idade') or 0) or None
    except (TypeError, ValueError):
        idade = None
    data_aniv = (d.get('data_aniversario') or '').strip() or None
    mensagem = (d.get('mensagem') or '').strip()[:1000] or None
    slug = secrets.token_urlsafe(6)[:12]
    row = db_execute("""
        INSERT INTO listas_aniversario
          (cliente_id, nome_crianca, idade, data_aniversario, slug, mensagem)
        VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, slug""",
        [c['id'], nome, idade, data_aniv, slug, mensagem], fetch='one')
    return jsonify({'ok': True, 'id': row['id'], 'slug': row['slug']})


@app.route('/minhas-listas/<int:lid>')
def lista_gerenciar(lid):
    c = cliente_logado()
    if not c:
        return redirect(url_for('login', next=request.path))
    lista = db_execute("""SELECT * FROM listas_aniversario
                          WHERE id=%s AND cliente_id=%s""",
                       [lid, c['id']], fetch='one')
    if not lista:
        return redirect(url_for('minhas_listas'))
    itens = _lista_carregar_itens(lid)
    return render_template('lista_gerenciar.html',
                           cliente=c, lista=lista, itens=itens,
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


@app.route('/api/listas/<int:lid>/add', methods=['POST'])
def lista_add_item(lid):
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'Faça login primeiro'}), 401
    lista = db_execute("""SELECT id FROM listas_aniversario
                          WHERE id=%s AND cliente_id=%s AND ativo""",
                       [lid, c['id']], fetch='one')
    if not lista:
        return jsonify({'erro': 'Lista não encontrada'}), 404
    d = request.get_json() or {}
    try:
        pid = int(d.get('produto_pdv_id') or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid:
        return jsonify({'erro': 'produto_pdv_id obrigatório'}), 400
    # Não deixa cadastrar produto sem estoque na lista — não faz sentido
    # começar lista de aniversário com item zerado.
    prod = buscar_produto(pid) or {}
    if not prod:
        return jsonify({'erro': 'Produto não encontrado'}), 404
    if float(prod.get('estoque_atual') or 0) <= 0:
        return jsonify({'erro': 'Produto indisponível no momento — '
                                'só dá pra adicionar à lista quando voltar ao estoque'}), 400
    db_execute("""INSERT INTO lista_aniversario_itens
                  (lista_id, produto_pdv_id, qtd)
                  VALUES (%s,%s,1)
                  ON CONFLICT (lista_id, produto_pdv_id) DO NOTHING""",
               [lid, pid])
    return jsonify({'ok': True})


@app.route('/api/listas/<int:lid>/remover/<int:item_id>', methods=['POST'])
def lista_remover_item(lid, item_id):
    c = cliente_logado()
    if not c:
        return jsonify({'erro': 'Faça login primeiro'}), 401
    db_execute("""DELETE FROM lista_aniversario_itens
                  WHERE id=%s AND lista_id IN
                    (SELECT id FROM listas_aniversario WHERE cliente_id=%s)""",
               [item_id, c['id']])
    return jsonify({'ok': True})


# ─── Luquizinha do site (chatbot IA) ─────────────────────────────────────────
LUQUIZINHA_SITE_PROMPT = """Voce eh a Luquizinha 🧸, atendente IA da Luqui
Brinquedos no site www.luquibrinquedos.com.br. Sua personalidade eh
calorosa, doce, brincalhona — voce trabalha numa loja de brinquedos da
familia em Cascavel/PR.

SEU TRABALHO: ajudar a pessoa (geralmente uma mae/tia/avo) a encontrar
o brinquedo perfeito. Sua META eh GARANTIR CONTATO + mostrar produtos
o mais rapido possivel.

TOM:
- Frases CURTAS (1-3 linhas). 1-2 emojis por mensagem.
- "Que delicia! 💛", "Vai amar de mais!", "Que ideia linda!"
- Espelhe a energia. NAO seja formal. NAO use markdown.

REGRA #1 — PEGAR O WHATSAPP ANTES DE TUDO:
A primeira msg do bot ja pede o WhatsApp da cliente (texto fixo do site).
O OBJETIVO eh: se a conversa morrer aqui no chat, nossa vendedora consegue
continuar o atendimento por WhatsApp. Sem isso, o lead se perde.

- Se a cliente RESPONDE com telefone (ex: "45 99999-9999", "11988887777"):
  agradeca rapido e PERGUNTE o que ela procura. Ex: "Anotado! 💛 Agora
  me conta, pra qual idade tu busca?".
- Se a cliente RESPONDE com termo de busca SEM dar o telefone
  (ex: ja vem com "barbie", "pokemon", "boneca que fala", "5 anos
  menino"): NAO ignore o pedido. Reconheca + busque IMEDIATAMENTE
  (chame buscar_produtos com o termo) E na MESMA msg, depois de
  mostrar 2-3 produtos, refor ce o pedido do WhatsApp. Ex:
  "Achei lindas opcoes de Barbie! 💛 [lista 2-3 com preco] Me passa
  teu WhatsApp com DDD pra eu garantir o contato caso a gente perca
  a conversa? ✨"
- Se a cliente RECUSA dar o telefone ("nao quero passar", "depois",
  "so olhando"): respeite, NAO insista mais que 1 vez extra. Siga
  ajudando normal e tenta de novo SO no final.
- "Barbie" NAO eh o nome da cliente. "Pokemon" NAO eh o nome da
  cliente. Marcas/franquias NUNCA sao tratadas como nome de pessoa.

ORDEM DE QUALIFICACAO (NAO trave a conversa nisso):
1. WhatsApp (REGRA #1 — sempre tenta primeiro)
2. Buscar o que ela pediu (se ja veio termo na 1a msg, busca em paralelo)
3. Idade da crianca (pra refinar)
4. Menino ou menina
- Nome NAO eh prioridade. So peca se a conversa engatar.

REGRA DE OURO — BUSCAR PRODUTOS CEDO:
- Tem TERMO? Busca AGORA, mesmo sem idade.
- Tem idade+sexo? Busca AGORA, mesmo sem termo.
- Quando a tool voltar com produtos (tipo='match' ou 'sugestao'),
  RESPONDA listando 2-3 deles no texto: "Achei lindas opcoes! 💛
  Tem a BARBIE FANTASIA por R$ 99,99 e a FROZEN ELSA E ANNA por
  R$ 135,99. Quer ver mais de perto?"
- Os cards aparecem visualmente, mas VC PRECISA mencionar 2-3 no
  texto. NUNCA termine o turno depois de buscar sem comentar.

QUANDO buscar_produtos RETORNA tipo='sem_match' (NAO tem o produto):
- NAO mostre lista aleatoria. NAO diga "passa na loja fisica" — a
  cliente JA ESTA NO SITE.
- Responda: "Hmm, [PRODUTO] eu nao tenho aqui agora, viu! 😕 Mas
  eu posso te avisar assim que chegar — me passa seu WhatsApp que
  eu mando pra voce?"
- Quando ela passar o telefone, chame registrar_lead com a obs
  "queria [PRODUTO] — avisar quando chegar".
- Tambem ofereca alternativa: "Enquanto isso, quer ver outras
  [BONECAS/CARRINHOS/etc] que sao a cara da idade dela?" e busque
  um termo generico do mesmo tipo.

REGISTRAR LEAD — chame registrar_lead assim que tiver telefone OU
assim que tiver idade+sexo (o que vier primeiro). Pessoas anonimas
SEM contato contam pouco — REGRA #1 ja prioriza o WhatsApp na
abertura, mas se mesmo assim a cliente nao deu, tente uma 2a vez
antes de fechar: "So me confirma teu WhatsApp pra eu garantir teu
atendimento mesmo se a gente desconectar 💛".
Se a cliente pediu "falar com vendedor" → registrar_lead na hora.

INFO QUE VOCE PODE DAR DIRETO:
💳 PIX 3% off, cartao 1x sem juros (2x+ tem juros, ate 12x)
🚚 Cascavel R$ 10 fixo, retire na loja gratis, outras cidades cota no checkout
📍 Rua Engenheiro Reboucas, 2053 — Cascavel/PR (so mencione se
   perguntarem ou se cliente quiser RETIRAR)
⏰ Seg-sex 9-18h · Sab 9-13h · Dom fechado

PROIBIDO:
- NUNCA mande a cliente pra "olhar no site" — ela JA ESTA no site.
- NUNCA diga "passa na loja fisica" pra desviar da venda online.
  So mencione endereco se ela quiser RETIRAR ou se perguntou.
- NUNCA comece com "Que bom te ver de novo!" / "te ver aqui" se
  voce nao tem contexto REAL anterior (so use se [CONTEXTO DA CLIENTE]
  trouxer historico).
- NUNCA invente preco/produto que nao veio da tool.
"""

LUQUIZINHA_TOOLS = [
    {
        "name": "buscar_produtos",
        "description": (
            "Busca brinquedos no catalogo. Devolve {produtos:[], "
            "termo_usado, tipo}. tipo='match' = achou o que a cliente "
            "pediu. tipo='sugestao' = mostra opcoes da faixa etaria (sem "
            "termo). tipo='sem_match' = NAO TEM o produto pedido — neste "
            "caso responda 'nao tenho [PRODUTO] hoje, me passa seu WhatsApp "
            "que aviso quando chegar' e ofereca alternativa. NUNCA mostre "
            "produtos aleatorios. Depois de achar, liste 2-3 no texto com "
            "nome+preco. Nao termine o turno em silencio."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "idade_anos": {"type": "integer", "description": "Idade da crianca em anos (ex: 5)."},
                "sexo": {"type": "string", "enum": ["menino", "menina"], "description": "Sexo da crianca, se souber."},
                "termo": {"type": "string", "description": "Tipo de brinquedo (ex: 'boneca', 'carrinho', 'jogo de tabuleiro'). Opcional."},
                "preco_max": {"type": "number", "description": "Limite de preco em reais (opcional)."},
            },
        },
    },
    {
        "name": "registrar_lead",
        "description": (
            "Marca a conversa como lead pro vendedor humano dar followup "
            "via WhatsApp. Chame ASSIM QUE TIVER TELEFONE da cliente "
            "(prioridade #1) OU assim que tiver idade+sexo da crianca. "
            "O que vier primeiro. Chama 1x so por conversa. Apos chamar, "
            "comente brevemente que vai pedir pra vendedora dar uma "
            "olhada tambem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nome": {"type": "string", "description": "Nome da cliente (mae/tia/avo). Vazio se nao pegou ainda."},
                "telefone": {"type": "string", "description": "WhatsApp da cliente (so digitos, com DDD). Vazio se nao deu."},
                "idade_crianca": {"type": "integer"},
                "sexo_crianca": {"type": "string", "enum": ["menino", "menina"]},
                "observacao": {"type": "string", "description": "Resumo curto do que a cliente procura (ex: 'menino 5 anos, gosta de carrinho hot wheels')."},
            },
        },
    },
]


def _ip_request():
    """IP do cliente (atras de proxy Railway/Cloudflare)."""
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()[:64]
    return (request.remote_addr or '')[:64]


def _luq_get_or_create_conversa(sessao_id):
    if not sessao_id:
        return None
    row = db_execute("SELECT * FROM site_chat_conversas WHERE sessao_id=%s",
                     [sessao_id], fetch='one')
    if row:
        return dict(row)
    ip = _ip_request()
    ua = (request.headers.get('User-Agent') or '')[:200]
    cli = cliente_logado()
    nv = db_execute("""INSERT INTO site_chat_conversas
                       (sessao_id, ip, user_agent, cliente_id, nome)
                       VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                    [sessao_id, ip, ua,
                     cli['id'] if cli else None,
                     cli.get('nome') if cli else None],
                    fetch='one')
    return dict(nv)


def _luq_carregar_historico(conversa_id, limit=50):
    rows = db_execute("""SELECT role, content, blocks FROM site_chat_mensagens
                          WHERE conversa_id=%s ORDER BY id ASC LIMIT %s""",
                       [conversa_id, limit], fetch='all') or []
    msgs = []
    for r in rows:
        if r.get('blocks'):
            blocks = r['blocks'] if isinstance(r['blocks'], list) else []
            msgs.append({'role': r['role'], 'content': blocks})
        elif r.get('content'):
            msgs.append({'role': r['role'], 'content': r['content']})
    return msgs


def _luq_save_msg(conversa_id, role, content=None, blocks=None):
    db_execute("""INSERT INTO site_chat_mensagens
                  (conversa_id, role, content, blocks)
                  VALUES (%s,%s,%s,%s)""",
               [conversa_id, role, content,
                json.dumps(blocks, ensure_ascii=False) if blocks else None])
    db_execute("UPDATE site_chat_conversas SET ultimo_msg_em=NOW() WHERE id=%s",
               [conversa_id])


LUQUIZINHA_SITE_MODEL = os.environ.get('LUQUIZINHA_SITE_MODEL', 'claude-sonnet-4-6')
LUQUIZINHA_MAX_TURNOS = int(os.environ.get('LUQUIZINHA_MAX_TURNOS', '8'))


_STOPWORDS_BUSCA = {'de', 'da', 'do', 'com', 'pra', 'para', 'um', 'uma',
                    'o', 'a', 'e', 'em', 'no', 'na', 'pro', 'que'}


# ── Auto-extração de qualificação ────────────────────────────────────────────
# Heurísticas regex pra preencher nome/idade/sexo da conversa SEM depender da
# IA chamar registrar_lead. Roda a cada msg do user.
_RE_IDADE = re.compile(r'\b(\d{1,2})\s*(?:anos?|aninhos?|a)\b', re.I)
_RE_IDADE_SOLTA = re.compile(r'^\s*(\d{1,2})\s*$')
_RE_MENINA = re.compile(r'\b(menina|filha|sobrinha|neta|princesinha)\b', re.I)
_RE_MENINO = re.compile(r'\b(menino|filho|sobrinho|neto|principezinho)\b', re.I)
_RE_TEL = re.compile(r'(?:\+?55\s*)?\(?(\d{2})\)?\s*9?\s*(\d{4})[-\s]?(\d{4})')
# Nome só com pista contextual — heurística "1 palavra solta = nome" pegava
# marcas (Barbie, Pokémon) e queimava a saudação como "Oi, Barbie! 💛".
_RE_NOME_EXPLICITO = re.compile(
    r'\b(?:meu nome (?:é|eh|e)|me chamo|sou (?:a|o)|aqui (?:é|eh|e)|nome (?:é|eh|e))\s+'
    r'([A-Za-zÀ-ÿ]{2,20})\b', re.I)


def _luq_extrair_da_msg(texto, ja_tem):
    """Tenta extrair nome/idade/sexo/telefone do texto do user.
    `ja_tem` = dict com o que já capturamos (não sobrescreve)."""
    out = {}
    t = texto.strip()
    if not t:
        return out
    # idade
    if not ja_tem.get('idade_crianca'):
        m = _RE_IDADE.search(t) or _RE_IDADE_SOLTA.match(t)
        if m:
            try:
                idade = int(m.group(1))
                if 0 < idade <= 18:
                    out['idade_crianca'] = idade
            except (ValueError, IndexError):
                pass
    # sexo
    if not ja_tem.get('sexo_crianca'):
        if _RE_MENINA.search(t):
            out['sexo_crianca'] = 'menina'
        elif _RE_MENINO.search(t):
            out['sexo_crianca'] = 'menino'
    # telefone
    if not ja_tem.get('lead_telefone'):
        m = _RE_TEL.search(t)
        if m:
            tel = ''.join(m.groups())
            if len(tel) >= 10:
                out['lead_telefone'] = tel[:20]
    # nome — SÓ com pista contextual ("meu nome é X", "sou a X"). Antes,
    # qualquer 1 palavra solta virava nome e a Luquizinha cumprimentava
    # "Oi, Barbie!" quando a cliente só queria ver Barbie no catálogo.
    if not ja_tem.get('nome'):
        m = _RE_NOME_EXPLICITO.search(t)
        if m:
            cand = m.group(1).strip()
            if cand and not _RE_MENINA.search(cand) and not _RE_MENINO.search(cand):
                out['nome'] = cand.capitalize()
    return out


def _luq_atualizar_qualificacao(conversa_id, extraido):
    """Atualiza colunas em site_chat_conversas com o que foi extraído."""
    if not extraido:
        return
    sets, params = [], []
    for k, v in extraido.items():
        sets.append(f"{k} = COALESCE({k}, %s)")
        params.append(v)
    params.append(conversa_id)
    db_execute(f"UPDATE site_chat_conversas SET {', '.join(sets)} WHERE id=%s",
               params)


def _luq_contexto_persistente(cli, ip):
    """Monta bloco de contexto pra system prompt com base em conversas
    anteriores (mesmo cliente_id OU mesmo IP) + listas de aniversário."""
    pedacos = []
    if cli:
        nome_cli = cli.get('nome') or ''
        if nome_cli:
            pedacos.append(f"Cliente logado: {nome_cli}.")
        # Listas de aniversario = filhos/afilhados cadastrados
        listas = db_execute("""SELECT nome_crianca, idade, data_aniversario
                               FROM listas_aniversario
                               WHERE cliente_id=%s AND ativo
                               ORDER BY criado_em DESC LIMIT 4""",
                            [cli['id']], fetch='all') or []
        if listas:
            partes = []
            for l in listas:
                p = l.get('nome_crianca') or 'crianca'
                if l.get('idade'):
                    p += f" ({l['idade']}a)"
                partes.append(p)
            pedacos.append("Criancas cadastradas: " + ', '.join(partes))
        # Pedidos recentes
        pcount = db_execute("""SELECT COUNT(*) AS n FROM pedidos
                               WHERE cliente_id=%s
                                 AND criado_em > NOW() - INTERVAL '180 days'""",
                            [cli['id']], fetch='one') or {}
        n = int((pcount or {}).get('n') or 0)
        if n > 0:
            pedacos.append(f"Ja fez {n} pedido(s) recentes. Cliente recorrente.")
    # Conversa anterior do mesmo IP (mesma pessoa em outra sessao). Só
    # vale como contexto se rolou qualificação REAL (idade da criança).
    # Conv anterior com só "nome" vinha quase sempre lixo da heurística
    # antiga (Barbie/Pokémon/Beth) — não usar.
    if ip:
        ant = db_execute("""SELECT nome, idade_crianca, sexo_crianca, criado_em
                            FROM site_chat_conversas
                            WHERE ip=%s
                              AND criado_em > NOW() - INTERVAL '60 days'
                              AND idade_crianca IS NOT NULL
                            ORDER BY criado_em DESC LIMIT 1""",
                         [ip], fetch='one')
        if ant:
            d = dict(ant)
            partes = []
            if d.get('sexo_crianca'):
                partes.append(d['sexo_crianca'])
            if d.get('idade_crianca'):
                partes.append(f"{d['idade_crianca']}a")
            if partes:
                pedacos.append("Conversou comigo antes (crianca: "
                               + ', '.join(partes) + "). "
                               + "NAO comece com 'te ver de novo' — "
                               + "apenas use essa info se ajudar.")
    return '\n'.join(pedacos)


def _fallback_termos_por_sexo(sexo):
    s = (sexo or '').lower()
    if s == 'menina':
        return ['boneca', 'pelúcia', 'kit', 'cozinha']
    if s == 'menino':
        return ['carrinho', 'lego', 'pista', 'kit']
    return ['kit', 'jogo', 'pelúcia']


# Cliente fala "boneca que fala" mas descrições usam "frases/papo/interativa".
# Sinônimos casam o vocabulário coloquial com o que de fato está no catálogo.
_SINONIMOS_BUSCA = {
    'fala': ['frases', 'papo', 'interativa'],
    'falar': ['frases', 'papo', 'interativa'],
    'falando': ['frases', 'papo', 'interativa'],
    'conversa': ['frases', 'papo', 'interativa'],
    'conversar': ['frases', 'papo', 'interativa'],
    'canta': ['música', 'musica', 'som', 'cantando'],
    'cantar': ['música', 'musica', 'som'],
    'dança': ['música', 'musica', 'som'],
    'dançar': ['música', 'musica', 'som'],
    'anda': ['caminha', 'movimento'],
    'andar': ['caminha', 'movimento'],
}


def _expandir_sinonimos(termo):
    """Pra termo do cliente com palavra coloquial, devolve termos alternativos
    que existem no catálogo. Ex: 'boneca que fala' → ['frases', 'papo']."""
    if not termo:
        return []
    palavras = [w.lower().strip('.,!?;:') for w in termo.split()]
    out = []
    for p in palavras:
        for syn in _SINONIMOS_BUSCA.get(p, []):
            if syn not in out:
                out.append(syn)
    return out


def _formatar_produtos(rows, preco_max=None):
    out = []
    for p in rows[:8]:
        preco = float(p.get('preco_promo') or p.get('preco_venda') or 0)
        if preco_max and preco > float(preco_max):
            continue
        out.append({
            'id': p.get('id'),
            'nome': p.get('descricao'),
            'preco': preco,
            'foto': p.get('foto_url') or '',
            'url': url_produto(p),
        })
    return out


def _luq_tool_buscar_produtos(args):
    """Tool: busca produtos via PDV com fallback em cascata.
    Catalogo da Luqui nao mantem todos os termos exatos — entao se a
    busca direta veio vazia, tentamos termo mais curto e depois termos
    genericos por sexo."""
    termo = (args.get('termo') or '').strip()
    sexo = (args.get('sexo') or '').strip().lower()
    preco_max = args.get('preco_max')

    # 1ª passada: tentativas que tentam casar o que a cliente PEDIU.
    # Se nenhuma achar, a 2ª passada cai em fallbacks por sexo (produtos
    # quaisquer da faixa). Diferenciamos pra IA saber se o que ela mostra
    # bate com o pedido ou se é só sugestão genérica.
    tentativas_pedido = []
    if termo:
        tentativas_pedido.append(termo)
        palavras = [w for w in termo.split() if w.lower() not in _STOPWORDS_BUSCA]
        if palavras and palavras[0].lower() != termo.lower():
            tentativas_pedido.append(palavras[0])
        sinonimos = _expandir_sinonimos(termo)
        base = palavras[0] if palavras else ''
        for s in sinonimos:
            cand = f"{base} {s}".strip() if base else s
            if cand not in tentativas_pedido:
                tentativas_pedido.append(cand)
            if s not in tentativas_pedido:
                tentativas_pedido.append(s)

    for t in tentativas_pedido:
        try:
            produtos, _ = listar_produtos(busca=t, limite=8)
        except Exception as e:
            log.error("buscar_produtos PDV: %s (termo=%r)", e, t)
            continue
        out = _formatar_produtos(produtos, preco_max=preco_max)
        if out:
            log_busca(termo, resultados=len(out), origem='luquizinha')
            return {'produtos': out, 'termo_usado': t, 'tipo': 'match'}

    # Nada bateu com o pedido. NÃO devolver lista aleatória (a IA mostra
    # "ACHA FORMAS, ALQUIMIA, ANATOMIA..." pra quem pediu Troll e parece
    # confuso). Devolve vazio com flag pra IA dizer que não tem agora e
    # pedir o WhatsApp pra avisar quando chegar.
    if termo:
        log_busca(termo, resultados=0, origem='luquizinha')
        return {'produtos': [], 'termo_usado': termo,
                'tipo': 'sem_match', 'pedido_original': termo}

    # Sem termo (cliente só deu idade/sexo) — pode mostrar genéricos.
    for t in _fallback_termos_por_sexo(sexo):
        try:
            produtos, _ = listar_produtos(busca=t, limite=8)
        except Exception as e:
            log.error("buscar_produtos PDV: %s (termo=%r)", e, t)
            continue
        out = _formatar_produtos(produtos, preco_max=preco_max)
        if out:
            return {'produtos': out, 'termo_usado': t, 'tipo': 'sugestao'}
    return {'produtos': [], 'termo_usado': None, 'tipo': 'sem_match'}


def _luq_tool_registrar_lead(conversa_id, args):
    """Marca conversa como lead + notifica vendedor via PDV."""
    nome = (args.get('nome') or '').strip()[:120]
    telefone = ''.join(c for c in (args.get('telefone') or '') if c.isdigit())[:20]
    idade = args.get('idade_crianca')
    sexo = (args.get('sexo_crianca') or '').strip().lower()
    obs = (args.get('observacao') or '').strip()[:500]
    db_execute("""UPDATE site_chat_conversas
                  SET lead_marcado=TRUE, nome=COALESCE(NULLIF(%s,''),nome),
                      lead_telefone=COALESCE(NULLIF(%s,''),lead_telefone),
                      idade_crianca=COALESCE(%s,idade_crianca),
                      sexo_crianca=COALESCE(NULLIF(%s,''),sexo_crianca)
                  WHERE id=%s""",
               [nome, telefone, idade if idade else None, sexo, conversa_id])
    # Notifica vendedor via PDV (que tem Z-API configurado)
    if PDVPRO_API_KEY:
        try:
            msg = (f"🎯 Novo lead do SITE Luquizinha\n\n"
                   f"👤 Cliente: {nome or '(sem nome)'}\n"
                   f"📱 WhatsApp: {telefone or '(nao informado)'}\n"
                   f"🧸 Crianca: {sexo or '?'} {idade or '?'} anos\n\n"
                   f"💬 {obs or '(sem observacao)'}\n\n"
                   f"Ver conversa: www.luquibrinquedos.com.br/admin/chats")
            requests.post(PDVPRO_URL + '/api/integracao/notificar-vendedores',
                          json={'mensagem': msg},
                          headers={'X-API-Key': PDVPRO_API_KEY}, timeout=8)
        except Exception as e:
            log.error("notificar vendedor: %s", e)
    return {'ok': True, 'msg': 'Lead registrado, vendedor avisado.'}


@app.route('/api/luquizinha/historico')
def luquizinha_historico():
    """GET histórico da conversa atual (pra widget retomar onde parou)."""
    sid = request.cookies.get('luqz_sid') or ''
    if not sid:
        return jsonify({'mensagens': []})
    conv = db_execute("SELECT id FROM site_chat_conversas WHERE sessao_id=%s",
                      [sid], fetch='one')
    if not conv:
        return jsonify({'mensagens': []})
    rows = db_execute("""SELECT role, content, blocks FROM site_chat_mensagens
                          WHERE conversa_id=%s ORDER BY id ASC LIMIT 100""",
                       [conv['id']], fetch='all') or []
    msgs = []
    for r in rows:
        if r['role'] not in ('user', 'assistant'):
            continue
        texto = r.get('content') or ''
        produtos = []
        if r.get('blocks'):
            blocks = r['blocks'] if isinstance(r['blocks'], list) else []
            for b in blocks:
                if b.get('type') == 'text':
                    texto = (texto + '\n' + b.get('text', '')).strip()
                elif b.get('type') == 'tool_use' and b.get('name') == 'buscar_produtos':
                    pass  # produtos vem no tool_result; ignora aqui
                elif b.get('type') == 'tool_result':
                    try:
                        d = json.loads(b.get('content') or '{}')
                        if isinstance(d, dict) and d.get('produtos'):
                            produtos = d['produtos']
                    except Exception:
                        pass
        if not texto and not produtos and r['role'] == 'user':
            continue  # tool_results do user nao mostra
        if texto or produtos:
            msgs.append({'role': r['role'], 'texto': texto, 'produtos': produtos})
    return jsonify({'mensagens': msgs})


@app.route('/api/luquizinha/chat', methods=['POST'])
def luquizinha_chat():
    """Envia 1 mensagem do cliente e devolve a resposta da IA."""
    if not ANTHROPIC_API_KEY:
        return jsonify({'erro': 'Luquizinha indisponivel agora'}), 503
    d = request.get_json() or {}
    texto = (d.get('texto') or '').strip()[:2000]
    if not texto:
        return jsonify({'erro': 'texto vazio'}), 400
    sid = request.cookies.get('luqz_sid') or ''
    if not sid:
        sid = secrets.token_urlsafe(16)[:32]
    # Rate limit: 30 msgs por IP por dia
    ip = _ip_request()
    if ip:
        cnt = db_execute("""SELECT COUNT(*) AS n FROM site_chat_mensagens m
                            JOIN site_chat_conversas c ON c.id=m.conversa_id
                            WHERE c.ip=%s AND m.role='user'
                              AND m.criado_em > NOW() - INTERVAL '24 hours'""",
                         [ip], fetch='one') or {}
        if int((cnt or {}).get('n') or 0) >= 30:
            resp = jsonify({'resposta': 'Vc ja conversou bastante hoje comigo, viu! 💛 Volta amanha que continuamos! Pra falar agora com vendedor: wa.me/5545991119800'})
            resp.set_cookie('luqz_sid', sid, max_age=60*60*24*30, httponly=False, samesite='Lax')
            return resp
    conv = _luq_get_or_create_conversa(sid)
    _luq_save_msg(conv['id'], 'user', content=texto)
    # Auto-extração: preenche nome/idade/sexo/tel direto, sem depender da IA
    extraido = _luq_extrair_da_msg(texto, dict(conv))
    if extraido:
        _luq_atualizar_qualificacao(conv['id'], extraido)
        conv.update(extraido)
    messages = _luq_carregar_historico(conv['id'])
    # Contexto persistente: o que sabemos da cliente (logada, IP recorrente)
    cli = cliente_logado()
    contexto = _luq_contexto_persistente(cli, conv.get('ip'))
    # Estado atual da qualificacao (pra IA nao re-perguntar)
    estado = []
    if conv.get('nome'):
        estado.append(f"Nome: {conv['nome']}")
    if conv.get('idade_crianca'):
        estado.append(f"Idade da crianca: {conv['idade_crianca']}a")
    if conv.get('sexo_crianca'):
        estado.append(f"Sexo: {conv['sexo_crianca']}")
    if conv.get('lead_marcado'):
        estado.append("LEAD JA REGISTRADO — nao chame registrar_lead de novo.")
    contexto_extra = ''
    if contexto:
        contexto_extra += '\n\n[CONTEXTO DA CLIENTE]\n' + contexto
    if estado:
        contexto_extra += '\n\n[JA CAPTUREI]\n' + '\n'.join(estado)
    system_blocks = [
        {'type': 'text', 'text': LUQUIZINHA_SITE_PROMPT,
         'cache_control': {'type': 'ephemeral'}},
    ]
    if contexto_extra.strip():
        system_blocks.append({'type': 'text', 'text': contexto_extra.strip()})
    # Loop tool use
    resposta_texto = ''
    produtos_exibir = []
    for _ in range(LUQUIZINHA_MAX_TURNOS):
        payload = {
            'model': LUQUIZINHA_SITE_MODEL,
            'max_tokens': 1024,
            'system': system_blocks,
            'tools': LUQUIZINHA_TOOLS,
            'messages': messages,
        }
        try:
            r = requests.post('https://api.anthropic.com/v1/messages',
                              headers={'Content-Type': 'application/json',
                                       'x-api-key': ANTHROPIC_API_KEY,
                                       'anthropic-version': '2023-06-01'},
                              json=payload, timeout=45)
            if r.status_code != 200:
                log.error("Luquizinha %s: %s", r.status_code, r.text[:200])
                break
            body = r.json()
        except Exception as e:
            log.error("Luquizinha req: %s", e)
            break
        content_blocks = body.get('content') or []
        _luq_save_msg(conv['id'], 'assistant', blocks=content_blocks)
        messages.append({'role': 'assistant', 'content': content_blocks})
        stop_reason = body.get('stop_reason')
        tool_uses = [b for b in content_blocks if b.get('type') == 'tool_use']
        if stop_reason == 'tool_use' and tool_uses:
            tool_results = []
            for tu in tool_uses:
                nm = tu.get('name')
                args = tu.get('input') or {}
                if nm == 'buscar_produtos':
                    result = _luq_tool_buscar_produtos(args)
                    produtos_exibir = result.get('produtos') or []
                elif nm == 'registrar_lead':
                    result = _luq_tool_registrar_lead(conv['id'], args)
                else:
                    result = {'erro': 'tool desconhecida'}
                tool_results.append({
                    'type': 'tool_result',
                    'tool_use_id': tu.get('id'),
                    'content': json.dumps(result, ensure_ascii=False),
                })
            _luq_save_msg(conv['id'], 'user', blocks=tool_results)
            messages.append({'role': 'user', 'content': tool_results})
            continue
        # Resposta final
        textos = [b.get('text', '') for b in content_blocks if b.get('type') == 'text']
        resposta_texto = '\n'.join(t for t in textos if t).strip()
        break
    if not resposta_texto:
        resposta_texto = 'Hmm, deu um errinho aqui! Pode tentar de novo? 💛'
    resp = jsonify({'resposta': resposta_texto, 'produtos': produtos_exibir})
    resp.set_cookie('luqz_sid', sid, max_age=60*60*24*30, httponly=False, samesite='Lax')
    return resp


# ─── Listas de aniversario continuam ─────────────────────────────────────────
@app.route('/api/listas/usuario')
def listas_do_usuario():
    """Devolve as listas do cliente logado pra dropdown no card de produto."""
    c = cliente_logado()
    if not c:
        return jsonify({'precisa_login': True, 'listas': []})
    listas = db_execute("""SELECT id, nome_crianca, slug FROM listas_aniversario
                            WHERE cliente_id=%s AND ativo
                            ORDER BY criado_em DESC""",
                        [c['id']], fetch='all') or []
    return jsonify({'listas': [dict(l) for l in listas]})


@app.route('/lista/<slug>')
def lista_publica(slug):
    """Pagina publica da lista — qualquer um abre pelo link compartilhado."""
    lista = db_execute("""SELECT l.*, cs.nome AS cliente_nome
                          FROM listas_aniversario l
                          LEFT JOIN clientes_site cs ON cs.id = l.cliente_id
                          WHERE l.slug=%s AND l.ativo""",
                       [slug], fetch='one')
    if not lista:
        return render_template('404.html'), 404
    itens = _lista_carregar_itens(lista['id'])
    return render_template('lista_publica.html',
                           lista=lista, itens=itens,
                           cliente=cliente_logado(),
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


@app.route('/api/lista/<slug>/presentear/<int:item_id>', methods=['POST'])
def lista_presentear(slug, item_id):
    """Convidada clica em "Presentear" — adiciona ao carrinho marcado."""
    lista = db_execute("""SELECT id FROM listas_aniversario
                          WHERE slug=%s AND ativo""",
                       [slug], fetch='one')
    if not lista:
        return jsonify({'erro': 'Lista não encontrada'}), 404
    item = db_execute("""SELECT * FROM lista_aniversario_itens
                          WHERE id=%s AND lista_id=%s""",
                       [item_id, lista['id']], fetch='one')
    if not item:
        return jsonify({'erro': 'Item não encontrado'}), 404
    if item.get('comprado_em'):
        return jsonify({'erro': 'Esse presente já foi comprado por outra pessoa'}), 409
    try:
        p = buscar_produto(item['produto_pdv_id']) or {}
    except Exception:
        p = {}
    if not p.get('id'):
        return jsonify({'erro': 'Produto indisponível'}), 404
    if float(p.get('estoque_atual') or 0) <= 0:
        return jsonify({'erro': 'Esse presente está indisponível no momento — '
                                'use o botão "Me avise quando voltar"'}), 400
    # Adiciona no carrinho com flag lista_item_id pra marcar como comprado depois
    itens = carrinho_ler()
    # Remove o mesmo produto se ja estiver — sobrescreve com a flag de lista
    itens = [it for it in itens if it.get('produto_id') != p['id']]
    itens.append({
        'produto_id': p['id'],
        'descricao': p.get('descricao') or 'Produto',
        'preco': float(p.get('preco_promo') or p.get('preco_venda') or 0),
        'qtd': 1,
        'foto_url': p.get('foto_url'),
        'codigo_barras': p.get('codigo_barras'),
        'lista_item_id': item['id'],
    })
    carrinho_salvar(itens)
    return jsonify({'ok': True, 'redirect': '/checkout'})


# ─── Admin (área restrita) ────────────────────────────────────────────────────
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    erro = None
    if request.method == 'POST':
        # Sem freio, o /admin/login aceitava tentativa infinita (medido: 10
        # senhas erradas seguidas, 10x HTTP 200). É a chave do painel inteiro —
        # pedidos, CPF, endereço e telefone de todo mundo.
        if not rate_limit_ok('login_admin', _rl_ip(), 8, 900):
            return render_template(
                'admin_login.html',
                erro='Muitas tentativas. Aguarde 15 minutos.'), 429
        email = (request.form.get('email') or '').strip().lower()
        senha = request.form.get('senha') or ''
        a = db_execute("SELECT * FROM admin_user WHERE LOWER(email)=%s",
                       [email], fetch='one')
        if a and check_password_hash(a['senha_hash'], senha):
            session.permanent = True
            session['admin_id'] = a['id']
            return redirect(request.args.get('next') or url_for('admin_home'))
        erro = 'E-mail ou senha incorretos.'
    return render_template('admin_login.html', erro=erro)


@app.route('/admin/sair')
def admin_sair():
    session.pop('admin_id', None)
    return redirect(url_for('admin_login'))


# ─── Admin: CMS de páginas ────────────────────────────────────────────────
_PAGINAS_PUBLICAS = [
    ('retirar-na-loja',   '🏪 Retire na loja',         '/retirar-na-loja'),
    ('clube-sobre',       '🎁 Clube Luqui (texto)',    None),
    ('trocas-devolucoes', '↩️ Trocas e devoluções',    '/trocas-devolucoes'),
    ('entregas',          '🚚 Entregas / prazos',      '/entregas'),
    ('formas-pagamento',  '💳 Formas de pagamento',    '/formas-pagamento'),
    ('privacidade',       '🔒 Política de privacidade','/privacidade'),
    ('termos',            '📜 Termos de uso',          '/termos'),
]

@app.route('/admin/paginas')
@requer_admin
def admin_paginas():
    salvas = db_execute("SELECT slug, atualizado_em FROM paginas_cms",
                        fetch='all') or []
    mapa_salvo = {r['slug']: r['atualizado_em'] for r in salvas}
    paginas = []
    for slug, titulo, url_publica in _PAGINAS_PUBLICAS:
        paginas.append({
            'slug': slug, 'titulo': titulo, 'url_publica': url_publica,
            'atualizado_em': mapa_salvo.get(slug),
        })
    return render_template('admin_paginas.html', pagina='paginas',
                           paginas=paginas)

@app.route('/admin/paginas/<slug>', methods=['GET', 'POST'])
@requer_admin
def admin_pagina_edit(slug):
    cfg = next((p for p in _PAGINAS_PUBLICAS if p[0] == slug), None)
    if not cfg:
        abort(404)
    _, titulo_default, url_publica = cfg
    msg = None
    if request.method == 'POST':
        novo_titulo = (request.form.get('titulo') or '').strip()
        novo_conteudo = sanitizar_html((request.form.get('conteudo') or '').strip())
        if not novo_titulo or not novo_conteudo:
            msg = ('erro', 'Preencha título e conteúdo')
        else:
            db_execute("""
                INSERT INTO paginas_cms (slug, titulo, conteudo, atualizado_em)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (slug) DO UPDATE
                SET titulo = EXCLUDED.titulo,
                    conteudo = EXCLUDED.conteudo,
                    atualizado_em = NOW()
            """, [slug, novo_titulo, novo_conteudo])
            msg = ('ok', 'Página salva! ' + (
                f'Confira em <a href="{url_publica}" target="_blank">{url_publica}</a>'
                if url_publica else 'Conteúdo atualizado.'
            ))
    titulo_atual, conteudo_atual = _pagina_get(slug)
    return render_template('admin_pagina_edit.html', pagina='paginas',
                           slug=slug, titulo_default=titulo_default,
                           url_publica=url_publica, msg=msg,
                           titulo=titulo_atual or titulo_default,
                           conteudo=conteudo_atual or '')

@app.route('/admin/paginas/<slug>/restaurar', methods=['POST'])
@requer_admin
def admin_pagina_restaurar(slug):
    db_execute("DELETE FROM paginas_cms WHERE slug=%s", [slug])
    return redirect(url_for('admin_pagina_edit', slug=slug))

@app.route('/admin')
@requer_admin
def admin_home():
    pedidos_recentes = db_execute(
        "SELECT * FROM pedidos ORDER BY criado_em DESC LIMIT 10",
        fetch='all') or []
    assinaturas_ativas = db_execute(
        """SELECT a.*, p.nome AS plano_nome, c.nome AS cliente_nome
           FROM clube_assinaturas a
           JOIN clube_planos p ON p.id=a.plano_id
           JOIN clientes_site c ON c.id=a.cliente_id
           WHERE a.status='ativa' ORDER BY a.proximo_envio LIMIT 10""",
        fetch='all') or []
    stats = db_execute(
        """SELECT
              (SELECT COUNT(*) FROM pedidos WHERE status NOT IN ('cancelado')) AS pedidos_total,
              (SELECT COUNT(*) FROM pedidos WHERE status='pago') AS pedidos_pagos,
              (SELECT COUNT(*) FROM pedidos WHERE status='aguardando_pagto') AS pedidos_aguardando,
              (SELECT COUNT(*) FROM pedidos WHERE status='pago' AND pdv_venda_id IS NULL) AS pedidos_sem_pdv,
              (SELECT COUNT(*) FROM clientes_site) AS clientes,
              (SELECT COUNT(*) FROM clientes_site WHERE criado_em > NOW() - INTERVAL '30 days') AS clientes_30d,
              (SELECT COUNT(*) FROM clube_assinaturas WHERE status='ativa') AS assinantes,
              (SELECT COALESCE(SUM(total),0) FROM pedidos WHERE status NOT IN ('cancelado','aguardando_pagto')) AS receita_total,
              (SELECT COALESCE(SUM(total),0) FROM pedidos WHERE status NOT IN ('cancelado','aguardando_pagto') AND criado_em > NOW() - INTERVAL '30 days') AS receita_30d
        """, fetch='one') or {}
    return render_template('admin_home.html',
                           pedidos=pedidos_recentes,
                           assinaturas=assinaturas_ativas,
                           stats=stats)


@app.route('/admin/analytics')
@requer_admin
def admin_analytics():
    """Dashboard de visitas do site público."""
    try:
        dias = int(request.args.get('dias', 14))
    except Exception:
        dias = 14
    if dias not in (1, 7, 14, 30, 90):
        dias = 14
    intervalo = f"{dias} days"

    resumo = db_execute("""
        SELECT
          COUNT(*) FILTER (WHERE ts >= NOW() - %s::interval AND NOT is_bot) AS humanos,
          COUNT(DISTINCT ip_hash) FILTER (WHERE ts >= NOW() - %s::interval AND NOT is_bot) AS unicos,
          COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '1 day' AND NOT is_bot) AS humanos_24h
        FROM site_visitas""",
        [intervalo, intervalo], fetch='one') or {}

    # Bot não tem linha crua desde 30/07/2026 — a contagem vem da tabela diária.
    bots = db_execute("""
        SELECT COALESCE(SUM(visitas), 0) AS bots
          FROM site_visitas_bots_diario
         WHERE dia >= CURRENT_DATE - %s::int""", [dias], fetch='one') or {}
    resumo = dict(resumo)
    resumo['bots'] = bots.get('bots') or 0

    por_dia = db_execute("""
        SELECT DATE(ts AT TIME ZONE 'America/Sao_Paulo') AS dia,
               COUNT(*) FILTER (WHERE NOT is_bot) AS visitas,
               COUNT(DISTINCT ip_hash) FILTER (WHERE NOT is_bot) AS unicos
        FROM site_visitas
        WHERE ts >= NOW() - %s::interval
        GROUP BY dia ORDER BY dia""", [intervalo], fetch='all') or []

    top_paginas = db_execute("""
        SELECT path, COUNT(*) AS visitas,
               COUNT(DISTINCT ip_hash) AS unicos
        FROM site_visitas
        WHERE ts >= NOW() - %s::interval AND NOT is_bot
        GROUP BY path ORDER BY visitas DESC LIMIT 20""",
        [intervalo], fetch='all') or []

    top_referrers = db_execute("""
        SELECT
          CASE
            WHEN referer IS NULL OR referer = '' THEN '(direto)'
            ELSE COALESCE(substring(referer from 'https?://([^/]+)'), '(outro)')
          END AS origem,
          COUNT(*) AS visitas
        FROM site_visitas
        WHERE ts >= NOW() - %s::interval AND NOT is_bot
        GROUP BY origem ORDER BY visitas DESC LIMIT 15""",
        [intervalo], fetch='all') or []

    ultimas = db_execute("""
        SELECT ts, path, referer, user_agent, is_bot
        FROM site_visitas
        ORDER BY ts DESC LIMIT 50""", fetch='all') or []

    # O que as pessoas procuram — separa quem achou de quem NÃO achou nada
    # (esse segundo grupo é lista de compras: procura existe, produto não).
    top_buscas = db_execute("""
        SELECT termo_norm AS termo, COUNT(*) AS n,
               COUNT(*) FILTER (WHERE resultados = 0) AS sem_resultado,
               MAX(ts) AS ultima
        FROM site_buscas
        WHERE ts >= NOW() - %s::interval
        GROUP BY termo_norm ORDER BY n DESC LIMIT 25""",
        [intervalo], fetch='all') or []

    return render_template('admin_analytics.html',
                           resumo=resumo, por_dia=por_dia,
                           top_paginas=top_paginas,
                           top_referrers=top_referrers,
                           top_buscas=top_buscas,
                           ultimas=ultimas, dias=dias)


@app.route('/admin/pedidos')
@requer_admin
def admin_pedidos():
    filtro_status = (request.args.get('status') or '').strip()
    busca = (request.args.get('q') or '').strip()
    where, params = ["1=1"], []
    if filtro_status:
        where.append("status = %s")
        params.append(filtro_status)
    if busca:
        where.append("(nome ILIKE %s OR email ILIKE %s OR CAST(id AS TEXT) = %s OR telefone ILIKE %s)")
        like = f"%{busca}%"
        params.extend([like, like, busca, like])
    pedidos = db_execute(
        f"SELECT * FROM pedidos WHERE {' AND '.join(where)} ORDER BY criado_em DESC LIMIT 200",
        params, fetch='all') or []
    # Stats (sempre considera TODOS os pedidos, ignora filtro)
    stats = db_execute("""
        SELECT
          COUNT(*) FILTER (WHERE status NOT IN ('cancelado')) AS total,
          COUNT(*) FILTER (WHERE status='aguardando_pagto') AS aguardando,
          COUNT(*) FILTER (WHERE status='pago' AND pdv_venda_id IS NOT NULL) AS pagos_processados,
          COUNT(*) FILTER (WHERE status='pago' AND pdv_venda_id IS NULL) AS pagos_pendentes,
          COUNT(*) FILTER (WHERE status IN ('enviado','entregue')) AS expedidos,
          COALESCE(SUM(total) FILTER (WHERE status NOT IN ('cancelado','aguardando_pagto')), 0) AS receita
        FROM pedidos
    """, fetch='one') or {}
    return render_template('admin_pedidos.html', pedidos=pedidos, stats=stats,
                           filtro_status=filtro_status, busca=busca)


@app.route('/api/admin/pedidos/<int:pid>/liberar-risco', methods=['POST'])
@requer_admin
def admin_liberar_risco(pid):
    """Destrava um pedido retido pelo antifraude e gera a etiqueta que o
    webhook segurou. Decisao humana explicita — nada libera sozinho."""
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'Pedido não encontrado'}), 404
    db_execute("UPDATE pedidos SET risco_liberado_em=NOW() WHERE id=%s", [pid])
    log.warning("pedido %s liberado manualmente do antifraude (score=%s)",
                pid, p.get('risco_score'))
    gerou = False
    try:
        fsid = (p.get('melhorenvio_servico_id') or '').strip()
        if (p.get('pago_em') and fsid
                and not (p.get('melhorenvio_etiqueta_id') or '').strip()
                and me_configurado() and me_token_atual()):
            gerou, _ = _gerar_etiqueta_me(
                pid, fsid, servico_nome=(p.get('frete_servico') or '')[:80])
    except Exception as e:
        log.error("etiqueta pos-liberacao pedido %s: %s", pid, e)
    return jsonify({'ok': True, 'etiqueta_gerada': gerou})


STATUS_TIMELINE = ['aguardando_pagto', 'pago', 'preparando', 'pronto_retirada', 'enviado', 'entregue']
STATUS_LABELS = {
    'aguardando_pagto': 'Aguardando pagamento',
    'pago':             'Pagamento confirmado',
    'preparando':       'Preparando seu pedido',
    'pronto_retirada':  'Pronto pra retirar na loja',
    'enviado':          'Saiu pra entrega',
    'entregue':         'Entregue ✓',
    'cancelado':        'Cancelado',
    'atrasado':         'Pagamento atrasado',
}


@app.route('/pedido/<int:pid>/acessar')
def pedido_acessar(pid):
    """Link magico do e-mail de confirmacao: entra na conta sem senha.

    O checkout nao exige cadastro, entao o pedido nasce com cliente_id NULL e
    quem comprava como visitante ficava sem conta e sem historico. Este link
    resolve os dois de uma vez, SEM por etapa nenhuma entre o cliente e o
    pagamento: quem clica provou que controla a caixa de e-mail (o token so
    foi enviado pra la), entao da pra criar/vincular a conta ja verificada e
    puxar CPF, telefone e endereco do proprio pedido.

    O token e o mesmo `pedidos.token` (24 hex) que ja protege as paginas de
    pagamento e tracking contra IDOR.
    """
    # Token e curto; sem freio da pra tentar forca bruta. Limita por IP real.
    if not rate_limit_ok('magic_link', _rl_ip(), 20, 300):
        return ('Muitas tentativas. Aguarde alguns minutos e tente de novo.',
                429, {'Content-Type': 'text/plain; charset=utf-8'})
    p = db_execute("""SELECT *, (criado_em > NOW() - INTERVAL '90 days') AS na_janela
                        FROM pedidos WHERE id=%s""", [pid], fetch='one')
    tok = str((p or {}).get('token') or '')
    if not p or not tok or not secrets.compare_digest(
            str(request.args.get('t') or ''), tok):
        abort(404)
    email = (p.get('email') or '').strip().lower()
    if not email:
        abort(404)
    # Link de pedido antigo continua abrindo o tracking, mas para de autenticar
    # -- caixa de e-mail abandonada nao vira chave vitalicia da conta.
    if not p.get('na_janela'):
        return redirect(url_for('pedido_tracking', pid=pid, t=tok))

    c = db_execute("SELECT * FROM clientes_site WHERE LOWER(email)=%s",
                   [email], fetch='one')
    if not c:
        nv = db_execute(
            "INSERT INTO clientes_site (nome, email, email_verificado, "
            "email_verificado_em) VALUES (%s,%s,TRUE,NOW()) RETURNING id",
            [(p.get('nome') or 'Cliente')[:160], email[:160]], fetch='one')
        cid = nv['id']
    else:
        cid = c['id']
        db_execute("UPDATE clientes_site SET email_verificado=TRUE, "
                   "email_verificado_em=COALESCE(email_verificado_em, NOW()) "
                   "WHERE id=%s", [cid])
    session.permanent = True
    session['cliente_id'] = cid
    adotar_pedidos_convidado(cid, email, email_verificado=True)
    log.info("magic link: pedido %s autenticou a conta %s (%s)", pid, cid, email)
    return redirect(url_for('minha_conta'))


@app.route('/pedido/<int:pid>/tracking')
def pedido_tracking(pid):
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p or not pedido_acesso_ok(p):
        abort(404)
    itens = db_execute(
        "SELECT * FROM pedido_itens WHERE pedido_id=%s ORDER BY id",
        [pid], fetch='all') or []
    return render_template('pedido_tracking.html',
                           p=p, itens=itens,
                           status_timeline=STATUS_TIMELINE,
                           status_labels=STATUS_LABELS,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/api/admin/pedido/<int:pid>/detalhe')
@requer_admin
def admin_pedido_detalhe(pid):
    """Tudo do pedido pro painel do admin: itens com foto, entrega e valores."""
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'Pedido não encontrado'}), 404
    itens = db_execute("""SELECT descricao, codigo_barras, foto_url,
                                 preco_unitario, quantidade, subtotal
                            FROM pedido_itens WHERE pedido_id=%s
                           ORDER BY id""", [pid], fetch='all') or []
    def num(v):
        return float(v) if v is not None else 0.0
    endereco = ''
    if p.get('endereco'):
        endereco = (f"{p['endereco']}, {p.get('numero') or 's/n'}"
                    f"{' — ' + p['complemento'] if p.get('complemento') else ''}"
                    f"\n{p.get('bairro') or ''} · {p.get('cidade') or ''}"
                    f"/{p.get('uf') or ''} · CEP {p.get('cep') or ''}")
    return jsonify({
        'id': p['id'],
        'status': p['status'],
        'criado_em': p['criado_em'].strftime('%d/%m/%Y às %H:%M') if p.get('criado_em') else '',
        'cliente': {'nome': p.get('nome'), 'email': p.get('email'),
                    'telefone': p.get('telefone'), 'cpf': p.get('cpf')},
        'entrega': {'endereco': endereco,
                    'servico': p.get('melhorenvio_servico_nome') or p.get('frete_servico'),
                    'prazo': p.get('frete_prazo'),
                    'rastreio': p.get('melhorenvio_rastreio'),
                    'presente': bool(p.get('embrulho_presente')),
                    'mensagem': p.get('embrulho_mensagem')},
        'pagamento': {'forma': p.get('forma_pagto'), 'parcelas': p.get('parcelas'),
                      'link': p.get('asaas_link') if str(p.get('asaas_link') or '').startswith('http') else None,
                      'cobranca': p.get('asaas_cobranca_id')},
        'valores': {'subtotal': num(p.get('subtotal')), 'frete': num(p.get('frete')),
                    'desconto': num(p.get('desconto')), 'juros': num(p.get('juros_valor')),
                    'total': num(p.get('total')), 'cupom': p.get('cupom_codigo')},
        'observacao': p.get('observacao'),
        'itens': [{'descricao': i['descricao'], 'ean': i.get('codigo_barras'),
                   'foto': i.get('foto_url'), 'preco': num(i.get('preco_unitario')),
                   'qtd': num(i.get('quantidade')), 'subtotal': num(i.get('subtotal'))}
                  for i in itens],
    })


@app.route('/api/admin/pedido/<int:pid>/status', methods=['POST'])
@requer_admin
def admin_pedido_status(pid):
    d = request.get_json() or {}
    novo = (d.get('status') or '').strip()
    rastreio = (d.get('rastreio') or '').strip() or None
    if novo not in STATUS_TIMELINE + ['cancelado']:
        return jsonify({'erro': 'Status inválido'}), 400
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'Pedido não encontrado'}), 404
    db_execute("""UPDATE pedidos SET status=%s,
                  melhorenvio_rastreio=COALESCE(%s, melhorenvio_rastreio),
                  atualizado_em=NOW() WHERE id=%s""",
               [novo, rastreio, pid])
    # Idem à rota do PDV Pro: cai no rastreio ja gravado pela etiqueta quando
    # ninguem digitou um.
    rastreio = rastreio or (p.get('melhorenvio_rastreio') or '').strip() or None
    if not rastreio and novo == 'enviado':
        rastreio = me_backfill_rastreio(p)
    # Se cancelou e tem venda no PDV Pro, cancela NF e volta estoque
    if novo == 'cancelado' and p.get('pdv_venda_id') and PDVPRO_API_KEY:
        try:
            requests.post(
                PDVPRO_URL + '/api/integracao/cancelar-venda',
                json={'venda_id': p['pdv_venda_id'],
                      'justificativa': 'Pedido site cancelado'},
                headers={'X-API-Key': PDVPRO_API_KEY}, timeout=30)
        except Exception as e:
            log.error("cancelar NF: %s", e)
    # Se virou 'pronto_retirada', carimba pronto_em pro contador de fila bater
    if novo == 'pronto_retirada':
        db_execute("UPDATE pedidos SET pronto_em=COALESCE(pronto_em, NOW()) WHERE id=%s", [pid])
    # Notifica cliente
    try:
        primeiro = (p.get('nome') or 'amigo(a)').split()[0]
        msgs = {
            'preparando': (f"📦 Oi {primeiro}! Seu *Pedido #{pid}* está sendo "
                           f"preparado com muito carinho 💛"),
            'pronto_retirada': (
                f"🏪 Oi {primeiro}! Seu *Pedido #{pid}* já está *pronto pra retirar* "
                f"na Luqui Brinquedos! 💛\n\n"
                f"📍 R. Eng. Rebouças, 2053 — Cascavel/PR\n"
                f"🕐 Seg a Sex: 8h-18h · Sáb: 9h-13h\n\n"
                f"Leva um documento com foto. Te esperamos! 🧸"),
            'enviado': (f"🚚 Oi {primeiro}! Seu *Pedido #{pid}* "
                        f"acabou de sair pra entrega!"
                        + (f"\n\n*Rastreio:* {rastreio}" if rastreio else "")
                        + f"\n\nAcompanhe: https://www.luquibrinquedos.com.br/pedido/{pid}/tracking?t={p.get('token','')}"),
            'entregue': (f"💛 *Pedido #{pid} entregue!* Esperamos que ame!\n\n"
                         f"Que tal nos avaliar? "
                         f"https://www.luquibrinquedos.com.br/pedido/{pid}/tracking?t={p.get('token','')}"),
        }
        if novo in msgs:
            enviar_whatsapp(p['telefone'], msgs[novo])
    except Exception as e:
        log.error("WA status: %s", e)
    return jsonify({'ok': True, 'status': novo})


@app.route('/admin/assinantes')
@requer_admin
def admin_assinantes():
    rows = db_execute(
        """SELECT a.*, p.nome AS plano_nome, p.preco_mensal,
                  c.nome AS cliente_nome, c.email AS cliente_email
           FROM clube_assinaturas a
           JOIN clube_planos p ON p.id=a.plano_id
           JOIN clientes_site c ON c.id=a.cliente_id
           ORDER BY a.iniciada_em DESC""", fetch='all') or []
    return render_template('admin_assinantes.html', assinaturas=rows)


@app.route('/admin/clientes')
@requer_admin
def admin_clientes():
    rows = db_execute("""
      SELECT c.id, c.nome, c.email, c.telefone, c.cpf, c.cidade, c.uf, c.criado_em,
             COUNT(DISTINCT p.id) FILTER (WHERE p.status IN """ + _SQL_PAGOS + """) AS qtd_pedidos,
             COALESCE(SUM(p.total) FILTER (WHERE p.status IN """ + _SQL_PAGOS + """),0) AS total_gasto,
             MAX(p.criado_em) AS ultimo_pedido,
             (SELECT a.id FROM clube_assinaturas a
              WHERE a.cliente_id=c.id AND a.status='ativa' LIMIT 1) AS assinatura_ativa
        FROM clientes_site c
        LEFT JOIN pedidos p ON p.cliente_id=c.id
        GROUP BY c.id
        ORDER BY total_gasto DESC, qtd_pedidos DESC
        LIMIT 300
    """, fetch='all') or []
    return render_template('admin_clientes.html', clientes=rows)


@app.route('/admin/banners', methods=['GET', 'POST'])
@requer_admin
def admin_banners():
    if request.method == 'POST':
        bid = request.form.get('id')
        d = {k: (request.form.get(k) or '').strip() for k in
             ('titulo', 'subtitulo', 'imagem_url', 'link', 'cta_texto', 'cor_fundo')}
        # subtitulo sai com |safe no hero da home — sanitiza antes de gravar
        d['subtitulo'] = sanitizar_html(d['subtitulo'])
        ordem = int(request.form.get('ordem') or 0)
        ativo = request.form.get('ativo') == 'on'
        if bid:
            db_execute("""UPDATE banners SET titulo=%s, subtitulo=%s,
                          imagem_url=%s, link=%s, cta_texto=%s, cor_fundo=%s,
                          ordem=%s, ativo=%s WHERE id=%s""",
                       [d['titulo'], d['subtitulo'], d['imagem_url'] or None,
                        d['link'] or None, d['cta_texto'] or 'Ver',
                        d['cor_fundo'] or '#4FB8FF', ordem, ativo, int(bid)])
        else:
            db_execute("""INSERT INTO banners
                (titulo, subtitulo, imagem_url, link, cta_texto, cor_fundo, ordem, ativo)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                [d['titulo'], d['subtitulo'], d['imagem_url'] or None,
                 d['link'] or None, d['cta_texto'] or 'Ver',
                 d['cor_fundo'] or '#4FB8FF', ordem, ativo])
        return redirect(url_for('admin_banners'))
    banners = db_execute("SELECT * FROM banners ORDER BY ordem, id",
                         fetch='all') or []
    return render_template('admin_banners.html', banners=banners)


@app.route('/admin/banners/<int:bid>/excluir', methods=['POST'])
@requer_admin
def admin_banner_excluir(bid):
    db_execute("DELETE FROM banners WHERE id=%s", [bid])
    return redirect(url_for('admin_banners'))


@app.route('/admin/chats')
@requer_admin
def admin_chats():
    """Lista todas as conversas da Luquizinha do site."""
    convs = db_execute("""SELECT c.*,
                                 (SELECT COUNT(*) FROM site_chat_mensagens m
                                  WHERE m.conversa_id=c.id AND m.role='user'
                                    AND m.content IS NOT NULL AND m.content <> '') AS msgs_user,
                                 (SELECT content FROM site_chat_mensagens m
                                  WHERE m.conversa_id=c.id AND m.role='user'
                                    AND m.content IS NOT NULL AND m.content <> ''
                                  ORDER BY m.id ASC LIMIT 1) AS primeira_msg,
                                 (SELECT COUNT(*) FROM site_chat_mensagens m
                                  WHERE m.conversa_id=c.id AND m.role='assistant'
                                    AND m.blocks::text LIKE %s) AS buscas_feitas
                          FROM site_chat_conversas c
                          ORDER BY ultimo_msg_em DESC NULLS LAST LIMIT 200""",
                       ['%buscar_produtos%'], fetch='all') or []
    return render_template('admin_chats.html', conversas=convs)


@app.route('/admin/chats/<int:cid>')
@requer_admin
def admin_chat_view(cid):
    conv = db_execute("SELECT * FROM site_chat_conversas WHERE id=%s",
                      [cid], fetch='one')
    if not conv:
        return redirect(url_for('admin_chats'))
    msgs_raw = db_execute("""SELECT role, content, blocks, criado_em
                             FROM site_chat_mensagens
                             WHERE conversa_id=%s ORDER BY id ASC""",
                          [cid], fetch='all') or []
    msgs = []
    for r in msgs_raw:
        texto = r.get('content') or ''
        produtos = []
        if r.get('blocks'):
            blocks = r['blocks'] if isinstance(r['blocks'], list) else []
            for b in blocks:
                if b.get('type') == 'text':
                    texto = (texto + '\n' + b.get('text', '')).strip()
                elif b.get('type') == 'tool_use':
                    texto = (texto + f"\n[tool] {b.get('name')}({json.dumps(b.get('input') or {}, ensure_ascii=False)})").strip()
                elif b.get('type') == 'tool_result':
                    try:
                        d = json.loads(b.get('content') or '{}')
                        if isinstance(d, dict) and d.get('produtos'):
                            produtos = d['produtos']
                    except Exception:
                        pass
        if texto or produtos:
            msgs.append({'role': r['role'], 'texto': texto, 'produtos': produtos,
                         'criado_em': r['criado_em']})
    return render_template('admin_chat_view.html', conv=conv, mensagens=msgs)


@app.route('/admin/banners/seed', methods=['POST'])
@requer_admin
def admin_banner_seed():
    """Insere os 5 banners default (so os que ainda nao existem pelo titulo)."""
    seeds = [
        ('PAGUE NO PIX',
         'Ganhe <b style="color:#FFC700">3% de desconto</b> em qualquer compra!',
         '/produtos', 'Ver produtos',
         'linear-gradient(135deg,#1652C7,#3B82F6)', 1),
        ('PARCELE EM 12X',
         'Sem juros (parcela mínima <b style="color:#FFC700">R$ 50</b>)',
         '/produtos', 'Comprar agora',
         'linear-gradient(135deg,#0E3D9E,#1652C7)', 2),
        ('RETIRE NA LOJA',
         '📍 Cascavel/PR — <b style="color:#FFC700">frete grátis</b>. Agende o horário no checkout!',
         '/retirar-na-loja', 'Como funciona',
         'linear-gradient(135deg,#1652C7,#4FB8FF)', 3),
        ('CLUBE LUQUI 🎁',
         'Descontos exclusivos + <b style="color:#FFC700">5% acumulativo</b> + 1 ponto a cada R$ 1',
         '/clube', 'Quero entrar',
         'linear-gradient(135deg,#A16207,#FFC700)', 4),
        ('NOVIDADES ✨',
         'Confere o que <b style="color:#FFC700">chegou</b> de mais legal!',
         '/novidades', 'Ver novidades',
         'linear-gradient(135deg,#1652C7,#3B82F6)', 5),
    ]
    inseridos = 0
    for tit, sub, link, cta, cor, ordem in seeds:
        ja = db_execute("SELECT id FROM banners WHERE titulo=%s",
                        [tit], fetch='one')
        if ja:
            continue
        db_execute("""INSERT INTO banners
          (titulo, subtitulo, link, cta_texto, cor_fundo, ordem, ativo)
          VALUES (%s,%s,%s,%s,%s,%s,true)""",
          [tit, sub, link, cta, cor, ordem])
        inseridos += 1
    return redirect(url_for('admin_banners'))


@app.route('/admin/cupons', methods=['GET', 'POST'])
@requer_admin
def admin_cupons():
    if request.method == 'POST':
        cid = request.form.get('id')
        codigo = (request.form.get('codigo') or '').strip().upper()[:40]
        tipo = (request.form.get('tipo') or 'pct').strip()
        valor = float((request.form.get('valor') or '0').replace(',', '.'))
        valor_min = float((request.form.get('valor_min') or '0').replace(',', '.'))
        usos_max = request.form.get('usos_max')
        usos_max = int(usos_max) if usos_max and usos_max.isdigit() else None
        valido_ate = request.form.get('valido_ate') or None
        ativo = request.form.get('ativo') == 'on'
        if not codigo or valor <= 0 or tipo not in ('pct', 'rs'):
            return redirect(url_for('admin_cupons') + '?erro=dados')
        if cid:
            db_execute("""UPDATE cupons SET codigo=%s, tipo=%s, valor=%s,
                          valor_min=%s, usos_max=%s, valido_ate=%s, ativo=%s
                          WHERE id=%s""",
                       [codigo, tipo, valor, valor_min, usos_max,
                        valido_ate, ativo, int(cid)])
        else:
            db_execute("""INSERT INTO cupons
                (codigo, tipo, valor, valor_min, usos_max, valido_ate, ativo)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (codigo) DO NOTHING""",
                [codigo, tipo, valor, valor_min, usos_max, valido_ate, ativo])
        return redirect(url_for('admin_cupons'))
    cupons = db_execute(
        "SELECT * FROM cupons ORDER BY ativo DESC, id DESC",
        fetch='all') or []
    return render_template('admin_cupons.html', cupons=cupons)


@app.route('/admin/cupons/<int:cid>/excluir', methods=['POST'])
@requer_admin
def admin_cupom_excluir(cid):
    db_execute("DELETE FROM cupons WHERE id=%s", [cid])
    return redirect(url_for('admin_cupons'))


@app.route('/admin/planos')
@requer_admin
def admin_planos():
    planos = db_execute(
        "SELECT * FROM clube_planos ORDER BY ordem", fetch='all') or []
    return render_template('admin_planos.html', planos=planos)


def _envio_config_por_plano(slug):
    """Quantidade de brinquedos e livros que cada plano envia por mês."""
    cfgs = {
        'smart-mensal':     {'brinquedos': 1, 'livros': 0, 'extras': 'marca página'},
        'smart-anual':      {'brinquedos': 1, 'livros': 0, 'extras': 'marca página'},
        'essencial-mensal': {'brinquedos': 1, 'livros': 1, 'extras': 'marca página + material'},
        'essencial-anual':  {'brinquedos': 1, 'livros': 1, 'extras': 'marca página + material'},
        'premium-mensal':   {'brinquedos': 2, 'livros': 2, 'extras': 'marca página + material + 1 evento'},
        'premium-anual':    {'brinquedos': 2, 'livros': 2, 'extras': 'marca página + material + 1 evento'},
    }
    return cfgs.get(slug, {'brinquedos': 1, 'livros': 0, 'extras': ''})


@app.route('/admin/clube/envio-mensal')
@requer_admin
def admin_clube_envio():
    """Tela pra preparar e despachar a Caixa Misteriosa do mês."""
    referencia = request.args.get('mes') or datetime.now(SP_TZ).strftime('%Y-%m')
    ativos = db_execute("""
        SELECT a.id AS assinatura_id, a.proximo_envio, a.ultimo_envio,
               p.slug AS plano_slug, p.nome AS plano_nome, p.preco_mensal,
               c.id AS cliente_id, c.nome AS cliente_nome,
               c.email, c.telefone, c.endereco, c.numero, c.complemento,
               c.bairro, c.cidade, c.uf, c.cep,
               (SELECT COUNT(*) FROM clube_envios e
                  WHERE e.assinatura_id=a.id AND e.referencia_mes=%s) AS ja_enviou
          FROM clube_assinaturas a
          JOIN clube_planos p ON p.id=a.plano_id
          JOIN clientes_site c ON c.id=a.cliente_id
         WHERE a.status='ativa'
         ORDER BY a.proximo_envio NULLS FIRST, c.nome
    """, [referencia], fetch='all') or []
    # Enriquece com config de quantidade
    for a in ativos:
        a['envio_cfg'] = _envio_config_por_plano(a['plano_slug'])
    return render_template('admin_clube_envio.html',
                           assinantes=ativos, referencia=referencia)


@app.route('/api/clube/envio/registrar', methods=['POST'])
@requer_admin
def clube_envio_registrar():
    """Registra o envio de UM assinante no mês. Marca clube_envios + atualiza
    proximo_envio + dá baixa de estoque no PDV Pro pra cada produto."""
    d = request.get_json() or {}
    aid = int(d.get('assinatura_id') or 0)
    referencia = (d.get('referencia_mes') or
                  datetime.now(SP_TZ).strftime('%Y-%m'))
    itens = d.get('itens') or []  # [{produto_id, descricao, preco}]
    if not aid or not itens:
        return jsonify({'erro': 'Faltam dados'}), 400
    ass = db_execute("SELECT * FROM clube_assinaturas WHERE id=%s",
                     [aid], fetch='one')
    if not ass or ass['status'] != 'ativa':
        return jsonify({'erro': 'Assinatura não ativa'}), 400
    # Insere envios + baixa estoque PDV Pro
    descricao_total = ', '.join(i.get('descricao', '?') for i in itens)
    db_execute("""INSERT INTO clube_envios
        (assinatura_id, referencia_mes, descricao, observacao)
        VALUES (%s,%s,%s,%s)""",
        [aid, referencia, descricao_total[:500],
         d.get('observacao') or 'Caixa Misteriosa do mês'])
    # Atualiza assinatura
    db_execute("""UPDATE clube_assinaturas SET ultimo_envio=CURRENT_DATE,
                  proximo_envio=CURRENT_DATE + INTERVAL '30 days'
                  WHERE id=%s""", [aid])
    # Dá baixa no PDV Pro como "venda interna"
    if PDVPRO_API_KEY:
        try:
            cli = db_execute("""SELECT c.* FROM clientes_site c
                JOIN clube_assinaturas a ON a.cliente_id=c.id
                WHERE a.id=%s""", [aid], fetch='one') or {}
            payload = {
                'pedido_id': f'clube-{aid}-{referencia}',
                'cliente': {'nome': cli.get('nome'), 'email': cli.get('email'),
                            'cpf': cli.get('cpf'), 'telefone': cli.get('telefone')},
                'itens': [{'produto_id': i['produto_id'],
                           'descricao': i.get('descricao'),
                           'preco_unitario': 0,  # brinde do clube
                           'quantidade': 1,
                           'subtotal': 0} for i in itens],
                'total': 0, 'desconto': 0, 'frete': 0,
                'forma_pagto': 'bonus_clube',
            }
            requests.post(PDVPRO_URL + '/api/integracao/pedido',
                          json=payload,
                          headers={'X-API-Key': PDVPRO_API_KEY}, timeout=15)
        except Exception as e:
            log.error("baixa estoque clube: %s", e)
    return jsonify({'ok': True})


@app.route('/api/integracao/buscar-produto')
@requer_admin
def admin_buscar_produto():
    """Proxy do admin pra buscar produtos no PDV Pro (autocomplete na tela
    de envio do clube)."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'produtos': []})
    rs = pdv_get('/api/integracao/produtos',
                 {'busca': q, 'limite': 10, 'apenas_vitrine': '0'}, ttl=10)
    return jsonify({'produtos': (rs or {}).get('produtos', [])[:10]})


# ─── Asaas ────────────────────────────────────────────────────────────────────
def cpf_valido(cpf):
    """Valida os dígitos verificadores do CPF (não só o tamanho)."""
    c = ''.join(ch for ch in (cpf or '') if ch.isdigit())
    if len(c) != 11 or c == c[0] * 11:
        return False
    for pos in (9, 10):
        soma = sum(int(c[i]) * (pos + 1 - i) for i in range(pos))
        dig = (soma * 10) % 11
        if dig == 10:
            dig = 0
        if dig != int(c[pos]):
            return False
    return True


def _asaas_headers():
    return {'access_token': ASAAS_API_KEY,
            'Content-Type': 'application/json',
            'User-Agent': 'LuquiShop/1.0'}


def asaas_criar_customer(nome, email, cpf, telefone=None):
    """Cria ou atualiza customer no Asaas. Devolve o id (sempre tenta reusar)."""
    if not ASAAS_API_KEY:
        return None
    cpf_d = ''.join(c for c in (cpf or '') if c.isdigit())
    # Tenta buscar pelo CPF primeiro
    try:
        r = requests.get(f'{ASAAS_BASE}/customers',
                         params={'cpfCnpj': cpf_d} if cpf_d else {'email': email},
                         headers=_asaas_headers(), timeout=10)
        if r.status_code == 200:
            data = r.json().get('data') or []
            if data:
                return data[0]['id']
    except Exception as e:
        log.error("asaas customer search: %s", e)
    # Cria novo
    # notificationDisabled: checkout de e-commerce. A régua padrão do Asaas
    # persegue quem não pagou (email/SMS na criação, 10 dias antes, no
    # vencimento e nos atrasos, com robô de voz nos atrasos) — quem abandonou
    # o carrinho levava ligação de cobrança e desistia.
    body = {'name': nome, 'email': email, 'cpfCnpj': cpf_d, 'mobilePhone': telefone,
            'notificationDisabled': True}
    try:
        r = requests.post(f'{ASAAS_BASE}/customers',
                          json=body, headers=_asaas_headers(), timeout=12)
        if r.status_code in (200, 201):
            return r.json().get('id')
        log.error("asaas customer create %s: %s", r.status_code, r.text[:300])
    except Exception as e:
        log.error("asaas customer create exc: %s", e)
    return None


def asaas_criar_cobranca(customer_id, valor, billing_type, descricao,
                         vencimento=None, parcelas=1, due_days=3,
                         externa_ref=None):
    """Cria payment. billing_type: PIX, BOLETO, CREDIT_CARD, UNDEFINED."""
    if not ASAAS_API_KEY or not customer_id:
        return None
    if not vencimento:
        vencimento = (datetime.now(SP_TZ).date()
                      + timedelta(days=due_days)).isoformat()
    body = {
        'customer': customer_id,
        'billingType': billing_type,
        'value': round(float(valor), 2),
        'dueDate': vencimento,
        'description': descricao[:500],
        'externalReference': externa_ref or '',
    }
    if billing_type == 'CREDIT_CARD' and parcelas > 1:
        body['installmentCount'] = parcelas
        body['totalValue'] = round(float(valor), 2)
        body['value'] = round(float(valor) / parcelas, 2)
    try:
        r = requests.post(f'{ASAAS_BASE}/payments',
                          json=body, headers=_asaas_headers(), timeout=15)
        if r.status_code in (200, 201):
            return r.json()
        log.error("asaas payment create %s: %s", r.status_code, r.text[:300])
    except Exception as e:
        log.error("asaas payment create exc: %s", e)
    return None


def asaas_criar_assinatura(customer_id, valor, descricao,
                           billing_type='PIX', externa_ref=None):
    """Cria assinatura mensal recorrente no Asaas."""
    if not ASAAS_API_KEY or not customer_id:
        return None
    proximo_venc = (datetime.now(SP_TZ).date() + timedelta(days=3)).isoformat()
    body = {
        'customer': customer_id,
        'billingType': billing_type,
        'value': round(float(valor), 2),
        'nextDueDate': proximo_venc,
        'cycle': 'MONTHLY',
        'description': descricao[:500],
        'externalReference': externa_ref or '',
    }
    try:
        r = requests.post(f'{ASAAS_BASE}/subscriptions',
                          json=body, headers=_asaas_headers(), timeout=15)
        if r.status_code in (200, 201):
            return r.json()
        log.error("asaas subscription create %s: %s", r.status_code, r.text[:300])
    except Exception as e:
        log.error("asaas subscription create exc: %s", e)
    return None


def asaas_cancelar_assinatura(subscription_id):
    if not ASAAS_API_KEY or not subscription_id:
        return False
    try:
        r = requests.delete(f'{ASAAS_BASE}/subscriptions/{subscription_id}',
                            headers=_asaas_headers(), timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        log.error("asaas subscription cancel: %s", e)
        return False


def asaas_criar_assinatura_cartao(customer_id, valor, descricao, externa_ref,
                                  credit_card, holder_info, remote_ip):
    """Assinatura CREDIT_CARD com cartão inline (checkout transparente).
    Primeira parcela é cobrada na hora; Asaas tokeniza o cartão pras próximas.
    Retorna (status_code, dict-resp)."""
    if not ASAAS_API_KEY or not customer_id:
        return 0, {'errors': [{'description': 'Gateway não configurado'}]}
    proximo_venc = (datetime.now(SP_TZ).date() + timedelta(days=3)).isoformat()
    body = {
        'customer': customer_id,
        'billingType': 'CREDIT_CARD',
        'value': round(float(valor), 2),
        'nextDueDate': proximo_venc,
        'cycle': 'MONTHLY',
        'description': descricao[:500],
        'externalReference': externa_ref or '',
        'creditCard': credit_card,
        'creditCardHolderInfo': holder_info,
        'remoteIp': remote_ip,
    }
    try:
        r = requests.post(f'{ASAAS_BASE}/subscriptions',
                          json=body, headers=_asaas_headers(), timeout=25)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {'errors': [{'description': r.text[:300]}]}
    except Exception as e:
        log.error("asaas assinatura cartao exc: %s", e)
        return 0, {'errors': [{'description': str(e)}]}


def asaas_criar_cobranca_cartao(customer_id, valor, descricao, parcelas,
                                externa_ref, credit_card, holder_info, remote_ip):
    """Cobrança CREDIT_CARD com dados do cartão inline (checkout transparente).
    Retorna (status_code, dict-resp). resp.errors[].description traz o motivo
    da recusa quando code in (400, 401, 402)."""
    if not ASAAS_API_KEY or not customer_id:
        return 0, {'errors': [{'description': 'Gateway não configurado'}]}
    body = {
        'customer': customer_id,
        'billingType': 'CREDIT_CARD',
        'value': round(float(valor), 2),
        'dueDate': (datetime.now(SP_TZ).date() + timedelta(days=3)).isoformat(),
        'description': descricao[:500],
        'externalReference': externa_ref or '',
        'creditCard': credit_card,
        'creditCardHolderInfo': holder_info,
        'remoteIp': remote_ip,
    }
    if parcelas and int(parcelas) > 1:
        body['installmentCount'] = int(parcelas)
        body['totalValue'] = round(float(valor), 2)
        body['value'] = round(float(valor) / int(parcelas), 2)
    try:
        r = requests.post(f'{ASAAS_BASE}/payments',
                          json=body, headers=_asaas_headers(), timeout=25)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {'errors': [{'description': r.text[:300]}]}
    except Exception as e:
        log.error("asaas cobranca cartao exc: %s", e)
        return 0, {'errors': [{'description': str(e)}]}


def asaas_buscar_cobranca(payment_id):
    """Objeto completo da cobrança. Serve pra ler `creditCard` (bandeira + 4
    últimos) de quem pagou pela fatura hospedada, onde o site nunca vê o
    cartão."""
    try:
        r = requests.get(f'{ASAAS_BASE}/payments/{payment_id}',
                         headers=_asaas_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error("asaas buscar cobranca %s: %s", payment_id, e)
    return {}


def asaas_buscar_pix_qr(payment_id):
    """Pega payload + QR code base64 do PIX."""
    try:
        r = requests.get(f'{ASAAS_BASE}/payments/{payment_id}/pixQrCode',
                         headers=_asaas_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error("asaas pix qr: %s", e)
    return {}


def asaas_status_conta():
    """Consulta dados da conta Asaas (sandbox/prod, status, etc).
    Usado pelo admin pra confirmar se 3DS/antifraude estão ligados — a
    ativação do 3DS é só pelo painel Asaas (Configurações → Conta), não
    há endpoint API público pra ligar."""
    if not ASAAS_API_KEY:
        return {'ok': False, 'erro': 'ASAAS_API_KEY não configurada'}
    try:
        r = requests.get(f'{ASAAS_BASE}/myAccount',
                         headers=_asaas_headers(), timeout=10)
        if r.status_code == 200:
            d = r.json() or {}
            return {
                'ok': True,
                'nome': d.get('name'),
                'email': d.get('email'),
                'cnpj': d.get('cpfCnpj'),
                'modo': 'sandbox' if 'sandbox' in (ASAAS_BASE or '') else 'producao',
                'painel_3ds': 'https://www.asaas.com/account/paymentSettings',
                'instrucao': 'Painel Asaas → Configurações da conta → '
                             'Configurações de cartão → ativar "Autenticação 3D Secure"',
            }
        return {'ok': False, 'erro': f'Asaas HTTP {r.status_code}'}
    except Exception as e:
        return {'ok': False, 'erro': str(e)}


def asaas_buscar_boleto_info(payment_id):
    """Pega identificationField (linha digitável) + barCode + bankSlipUrl
    do boleto. Usado pra mostrar copia-cola na própria página."""
    try:
        r = requests.get(
            f'{ASAAS_BASE}/payments/{payment_id}/identificationField',
            headers=_asaas_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error("asaas boleto info: %s", e)
    return {}


# ─── WhatsApp (Z-API) ─────────────────────────────────────────────────────────
def enviar_whatsapp(numero, mensagem):
    """Manda mensagem via Z-API. `numero` em E164 sem '+', ex '5545991119800'."""
    if not (ZAPI_INSTANCE and ZAPI_TOKEN):
        log.info("Z-API não configurado — WhatsApp pulado")
        return False
    numero = ''.join(c for c in (numero or '') if c.isdigit())
    if not numero:
        return False
    if not numero.startswith('55'):
        numero = '55' + numero
    url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"
    try:
        r = requests.post(url, json={'phone': numero, 'message': mensagem},
                          headers={'Client-Token': ZAPI_CLIENT_TOKEN,
                                   'Content-Type': 'application/json'},
                          timeout=15)
        if r.status_code in (200, 201):
            return True
        log.error("Z-API %s pra %s: %s", r.status_code, numero, r.text[:300])
    except Exception as e:
        log.error("Z-API exc: %s", e)
    return False


# ─── Email (Resend) ───────────────────────────────────────────────────────────
def enviar_email(para, assunto, html):
    if not RESEND_API_KEY:
        log.info("Resend não configurado — email pulado")
        return False
    try:
        r = requests.post('https://api.resend.com/emails',
                          headers={'Authorization': f'Bearer {RESEND_API_KEY}',
                                   'Content-Type': 'application/json',
                                   'User-Agent': 'LuquiShop/1.0'},
                          json={'from': EMAIL_FROM, 'to': [para],
                                'reply_to': EMAIL_REPLY_TO,
                                'subject': assunto, 'html': html},
                          timeout=15)
        if r.status_code in (200, 202):
            return True
        log.error("Resend %s: %s", r.status_code, r.text[:300])
    except Exception as e:
        log.error("Resend exc: %s", e)
    return False


# ─── Antifraude ───────────────────────────────────────────────────────────────
# Motivacao (24/07/2026): 5 pedidos pra Brasilia sairam todos do mesmo punhado
# de IPs adjacentes de datacenter (45.134.141.130/131/133 — CDN77/DataCamp),
# com DUAS identidades diferentes (CPFs, nomes, e-mails e telefones distintos)
# saindo do MESMO IP. Um deles foi pago no cartao e a etiqueta saiu sozinha no
# webhook — mercadoria na rua antes de qualquer olho humano ver o pedido.
#
# O desenho aqui e deliberado: NADA disso recusa pagamento. Bloquear cartao por
# score derruba venda legitima demais (mae pagando com o cartao dela, presente
# comprado pelo marido). O que trava e o passo IRREVERSIVEL — a etiqueta
# automatica. Pedido de risco alto fica pago e parado esperando o Lucas.
RISCO_LIMITE = 50          # >= isso segura a etiqueta automatica
MAX_TENTATIVAS_CARTAO = 5  # cartoes distintos por pedido antes de travar
RISCO_VALOR_ALTO = 800.0
RISCO_VALOR_MUITO_ALTO = 1500.0

# Nomes de rede que denunciam hospedagem/VPN em vez de banda larga residencial.
_ORG_DATACENTER = (
    'datacamp', 'cdn77', 'ovh', 'hetzner', 'digitalocean', 'linode', 'vultr',
    'contabo', 'leaseweb', 'choopa', 'quadranet', 'psychz', 'm247', 'zenlayer',
    'amazon', 'aws', 'google', 'microsoft', 'azure', 'oracle', 'alibaba',
    'cloudflare', 'fastly', 'akamai', 'hostinger', 'godaddy', 'namecheap',
    'nordvpn', 'surfshark', 'expressvpn', 'mullvad', 'privatelayer', 'packethub',
    'hosting', 'datacenter', 'data center', 'cloud', 'server', 'colo',
    # O IP do pedido #52 (149.22.86.243) e da CDNEXT, Lisboa — hospedagem, mas
    # nenhuma palavra acima batia. Lista de nome de empresa envelhece sozinha;
    # por isso o sinal de RIR fora da LatAm existe como rede de seguranca.
    'cdnext', 'cdn', 'telecom italia sparkle', 'g-core', 'stark industries',
    'flokinet', 'bitlaunch', 'ipxo', 'hivelocity', 'servers.com',
)


# ─── Sanitização de HTML do CMS ───────────────────────────────────────────────
# As páginas do CMS e o subtítulo do banner são renderizados com `|safe`, ou
# seja, HTML cru direto na loja. Só admin escreve ali — mas "só admin" deixou de
# ser garantia forte quando se soma a falta de CSRF (corrigida logo abaixo) e um
# painel que até ontem aceitava tentativa de senha infinita. Se um <script>
# entrar nessas tabelas, ele roda na página de TODO cliente.
_TAGS_OK = ['p', 'br', 'strong', 'b', 'em', 'i', 'u', 'ul', 'ol', 'li', 'a',
            'h1', 'h2', 'h3', 'h4', 'blockquote', 'hr', 'span', 'div',
            'table', 'thead', 'tbody', 'tr', 'th', 'td', 'small', 'img']
_ATTRS_OK = {'a': ['href', 'title', 'target', 'rel'],
             'img': ['src', 'alt', 'title', 'width', 'height'],
             '*': ['style']}
_PROTOCOLOS_OK = ['http', 'https', 'mailto', 'tel']


def sanitizar_html(bruto):
    """Deixa passar formatação, remove script/onclick/javascript:.

    Se o bleach faltar (deploy quebrado, ambiente sem a lib), NÃO devolve o
    HTML cru — escapa tudo. Falhar exibindo `<b>` literal é feio; falhar
    servindo <script> de terceiro na loja inteira é incidente.
    """
    if not bruto:
        return bruto
    try:
        import bleach
        # A partir do bleach 5 o `style` só sobrevive com um CSSSanitizer
        # (pacote bleach[css]). Sem ele, o bleach descarta o atributo inteiro e
        # as páginas que o Lucas já escreveu perderiam a formatação na primeira
        # vez que fossem salvas de novo.
        css = None
        try:
            from bleach.css_sanitizer import CSSSanitizer
            css = CSSSanitizer(allowed_css_properties=[
                'color', 'background-color', 'font-size', 'font-weight',
                'font-style', 'text-align', 'text-decoration', 'line-height',
                'margin', 'margin-top', 'margin-bottom', 'padding',
                'border', 'border-radius', 'width', 'max-width', 'height',
                'display', 'float', 'vertical-align'])
        except ImportError:
            log.warning("bleach[css] ausente — style= será removido do CMS")
        return bleach.clean(bruto, tags=_TAGS_OK, attributes=_ATTRS_OK,
                            protocols=_PROTOCOLOS_OK, strip=True,
                            css_sanitizer=css)
    except ImportError:
        log.error("bleach ausente — escapando HTML do CMS por precaução")
        from markupsafe import escape
        return str(escape(bruto))


# ─── CSRF ─────────────────────────────────────────────────────────────────────
# Sem isso, qualquer site que o Lucas visitasse logado no painel podia disparar
# POST autenticado no lugar dele — criar cupom, reescrever página, liberar
# pedido retido pelo antifraude. Mesma coisa pro cliente logado: trocar senha
# ou cancelar assinatura.
#
# A lista é de ADESÃO, não de exclusão. Fluxo de venda (carrinho, checkout,
# pagar-cartao) fica FORA de propósito: CSRF ali não dá nada pro atacante (ele
# faria a vítima criar um pedido pra si mesma) e um engano na validação
# derrubaria faturamento. Webhook, /cron e /api/integracao também ficam fora —
# são chamadas servidor-a-servidor, autenticadas por token/chave e sem cookie,
# então CSRF não se aplica.
_CSRF_PREFIXOS = (
    '/admin/', '/api/admin/',
    '/api/clube/',                 # pausar, cancelar, trocar plano
    '/api/minha-conta/',           # trocar senha — o clássico de account takeover
    '/api/wishlist/', '/api/listas/', '/minhas-listas/',
)
# /admin/login fica de fora: o formulário é servido antes de existir sessão, e
# CSRF de login é ameaça bem menor que quebrar a porta de entrada do painel.
_CSRF_ISENTOS = ('/admin/login',)


def csrf_token():
    """Token por sessão. Criado na primeira renderização que precisar dele."""
    tok = session.get('_csrf')
    if not tok:
        tok = secrets.token_urlsafe(32)
        session['_csrf'] = tok
    return tok


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def _valida_csrf():
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    caminho = request.path
    if caminho in _CSRF_ISENTOS:
        return None
    if not caminho.startswith(_CSRF_PREFIXOS):
        return None
    esperado = session.get('_csrf') or ''
    recebido = (request.headers.get('X-CSRF-Token')
                or request.form.get('_csrf')
                or (request.get_json(silent=True) or {}).get('_csrf')
                or '')
    if esperado and secrets.compare_digest(str(recebido), str(esperado)):
        return None
    log.warning("CSRF recusado em %s (origin=%s)", caminho,
                request.headers.get('Origin') or request.headers.get('Referer'))
    if caminho.startswith('/api/'):
        return jsonify({'erro': 'Sessão expirada. Recarregue a página.'}), 403
    return ('Sessão expirada. Recarregue a página e tente de novo.', 403,
            {'Content-Type': 'text/plain; charset=utf-8'})


@app.after_request
def _headers_seguranca(resp):
    """A produção não devolvia header de segurança nenhum (conferido no curl).

    Sem HSTS, o primeiro acesso em http:// pode ser interceptado; sem
    nosniff/frame-options a loja podia ser embutida em iframe de terceiro
    (clickjacking em cima do checkout). CSP fica de fora de propósito: os
    templates usam script/style inline pra caramba e uma CSP restritiva
    quebraria o site — entra depois, com nonce, se valer a pena.
    """
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy',
                            'geolocation=(), microphone=(), camera=()')
    if os.environ.get('RAILWAY_ENVIRONMENT'):
        resp.headers.setdefault('Strict-Transport-Security',
                                'max-age=31536000; includeSubDomains')
    return resp


@app.template_filter('brt')
def _filtro_brt(v, fmt='%d/%m/%Y %H:%M'):
    """Formata data/hora SEMPRE no fuso de Brasília.

    O Postgres da Railway roda em UTC e as colunas de pedido são TIMESTAMPTZ,
    então o psycopg2 devolve datetime ciente em UTC. Chamar `.strftime()` direto
    no template imprime a hora UTC: um pedido das 15h44 aparecia como 18h44, e o
    #35, feito 22h42 do dia 22, aparecia como "23/07 02:42" — dia errado.

    `date` puro (aniversário, validade de cupom, próximo envio) NÃO leva
    conversão: não tem hora, e deslocar fuso mudaria o dia à toa.
    """
    if not v:
        return '—'
    try:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            v = v.astimezone(SP_TZ)
        return v.strftime(fmt)
    except Exception:
        return '—'


def _so_digitos(v):
    return ''.join(c for c in (v or '') if c.isdigit())


def _brl(v):
    """1060.77 -> 'R$ 1.060,77'. Trocar ',' por '.' direto no format do Python
    estraga o separador decimal junto ('R$ 1.060.77')."""
    return 'R$ ' + f'{float(v or 0):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def _nome_norm(v):
    """Nome sem acento, sem pontuacao, caixa alta, espaco unico."""
    v = unicodedata.normalize('NFKD', (v or '')).encode('ascii', 'ignore').decode()
    v = re.sub(r'[^A-Za-z ]', ' ', v).upper()
    return ' '.join(v.split())


def semelhanca_nomes(a, b):
    """Quanto os dois nomes parecem ser da mesma pessoa: None | 0.0 a 1.0.

    A versão anterior exigia primeiro nome E último sobrenome iguais e errava
    feio. Medido nos pedidos reais de 27/07, ela reprovou 6 de 10 nomes que
    eram da MESMA pessoa: "JAELZIN FORLIN" x "Jaelzinho forlin" (a fatura
    corta), "DIESON GESTEIRA" x "Dielson Gesteira" (o cliente errou uma
    letra), "JOÃO LUIZ" x "Joao Luis" (Luiz/Luis), "THIAGO CORDERO" x "THIAGO
    CORDERO PIVOTTO" (nome do meio ausente).

    Marcar cliente de verdade é o pior erro que um antifraude comete: some com
    a confiança de quem lê o alerta, e aí o alerta que importa também é
    ignorado. Similaridade separa os dois grupos com folga no dado real —
    mesma pessoa ficou entre 0,61 e 0,97; pessoa diferente, entre 0,08 e 0,26.
    """
    na, nb = _nome_norm(a), _nome_norm(b)
    if not na or not nb:
        return None                       # sem dado pra comparar
    if na == nb:
        return 1.0
    pa, pb = na.split(), nb.split()

    def _casa(x, y):
        # inicial abreviada ou nome cortado: JAELZIN <-> JAELZINHO
        return (x == y or (len(x) == 1 and y.startswith(x))
                or (len(y) == 1 and x.startswith(y))
                or x.startswith(y) or y.startswith(x))

    # Primeiro nome e sobrenome batendo já resolve, mesmo com miolo diferente.
    if _casa(pa[0], pb[0]) and (len(pa) == 1 or len(pb) == 1
                                or _casa(pa[-1], pb[-1])):
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def ip_reputacao(ip):
    """{'rir','org','datacenter'} do IP, com cache no banco.

    Consulta RDAP (rdap.org redireciona pro RIR certo). Falha SEMPRE aberta:
    se a consulta cair, o pedido segue como se o IP fosse limpo — antifraude
    que derruba checkout custa mais caro que a fraude que ele pega.
    """
    ip = (ip or '').strip()
    if not ip or ip.startswith(('10.', '192.168.', '127.')):
        return None
    try:
        cache = db_execute("SELECT * FROM ip_reputacao WHERE ip=%s "
                           "AND checado_em > NOW() - interval '30 days'",
                           [ip], fetch='one')
        if cache:
            return {'rir': cache['rir'], 'org': cache['org'],
                    'datacenter': cache['datacenter']}
    except Exception:
        return None
    try:
        r = requests.get(f'https://rdap.org/ip/{ip}',
                         headers={'User-Agent': 'LuquiShop-antifraude/1.0'},
                         timeout=4)
        if r.status_code != 200:
            return None
        d = r.json() or {}
        org = (d.get('name') or '')[:200]
        for ent in (d.get('entities') or []):
            vc = ent.get('vcardArray')
            if not vc:
                continue
            for campo in vc[1]:
                if campo[0] == 'fn' and campo[3]:
                    org = f'{org} / {campo[3]}'[:200]
                    break
        # De onde sai o RIR: `port43` é o caminho normal, mas nem toda resposta
        # traz (o 149.22.86.243 veio sem, e o IP ficou sem classificação
        # nenhuma). Os links `self` sempre apontam pro RDAP do RIR — servem de
        # segunda fonte.
        pistas = (d.get('port43') or '')
        for lk in (d.get('links') or []):
            pistas += ' ' + str(lk.get('href') or '')
        pistas = pistas.lower()
        rir = ('lacnic' if 'lacnic' in pistas else
               'ripe' if 'ripe' in pistas else
               'arin' if 'arin' in pistas else
               'apnic' if 'apnic' in pistas else
               'afrinic' if 'afrinic' in pistas else '')[:20]
        alvo = org.lower()
        dc = any(k in alvo for k in _ORG_DATACENTER)
        db_execute("""INSERT INTO ip_reputacao (ip, rir, org, datacenter, checado_em)
                      VALUES (%s,%s,%s,%s,NOW())
                      ON CONFLICT (ip) DO UPDATE SET rir=EXCLUDED.rir,
                        org=EXCLUDED.org, datacenter=EXCLUDED.datacenter,
                        checado_em=NOW()""", [ip, rir, org, dc])
        return {'rir': rir, 'org': org, 'datacenter': dc}
    except Exception as e:
        log.info("rdap %s falhou (segue): %s", ip, e)
        return None


def avaliar_risco_pedido(pid):
    """Recalcula score+motivos do pedido e grava. Retorna (score, motivos)."""
    try:
        p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
        if not p:
            return 0, []
        score, motivos = 0, []
        total = float(p.get('total') or 0)
        cpf = _so_digitos(p.get('cpf'))
        ip = (p.get('ip_cliente') or '').strip()

        # 1) Titular do cartao x comprador — o sinal que o Lucas pediu.
        if p.get('titular_cpf'):
            if _so_digitos(p['titular_cpf']) != cpf:
                score += 40
                motivos.append('CPF do titular do cartão ≠ CPF do comprador')
            # Pontuação graduada. "Diferente" e "escrito diferente" não são a
            # mesma coisa: fatura corta nome, cliente erra letra, Luiz vira
            # Luis. Só cai a régua inteira quando é outra pessoa mesmo.
            sem = semelhanca_nomes(p.get('titular_nome'), p.get('nome'))
            if sem is not None and sem < 0.45:
                score += 40
                motivos.append(f"Titular do cartão \"{p['titular_nome']}\" "
                               f"é outra pessoa (comprador: \"{p['nome']}\")")
            elif sem is not None and sem < 0.75:
                score += 15
                motivos.append(f"Titular do cartão \"{p['titular_nome']}\" "
                               f"só parece com o comprador \"{p['nome']}\"")

        # 2) Mesmo IP, outra identidade. Foi assim que os 5 pedidos de
        #    Brasilia se denunciaram: dois CPFs saindo do mesmo endereco.
        if ip:
            try:
                outros = db_execute(
                    "SELECT DISTINCT cpf, nome FROM pedidos "
                    "WHERE ip_cliente=%s AND id<>%s AND cpf IS NOT NULL",
                    [ip, pid], fetch='all') or []
                cpfs = {_so_digitos(o['cpf']) for o in outros} - {cpf, ''}
                if cpfs:
                    score += 35
                    motivos.append(f'Mesmo IP já usou outro(s) CPF(s): '
                                   f'{", ".join(sorted(cpfs))[:80]}')
            except Exception:
                pass
            # Velocidade. Teste de cartão roda ao contrário do golpe de valor
            # alto: várias compras BARATAS e idênticas, nomes de teclado
            # aleatório, minutos de intervalo, pra descobrir quais números de
            # cartão ainda passam. Os pedidos #40/#41 (R$ 10,76 cada, 11 min de
            # diferença) pontuaram só 10 porque toda a régua olhava pra valor
            # ALTO. Frequência pega o que o valor não pega.
            try:
                v = db_execute(
                    "SELECT COUNT(*) AS n, COUNT(DISTINCT cpf) AS cpfs "
                    "FROM pedidos WHERE ip_cliente=%s AND id<>%s "
                    "AND criado_em > NOW() - interval '1 hour'",
                    [ip, pid], fetch='one') or {}
                if int(v.get('n') or 0) >= 2:
                    score += 30
                    motivos.append(f'{int(v["n"]) + 1} pedidos do mesmo IP '
                                   f'em menos de 1 hora')
                elif int(v.get('n') or 0) == 1 and int(v.get('cpfs') or 0) == 1:
                    score += 15
                    motivos.append('2 pedidos do mesmo IP em menos de 1 hora')
            except Exception:
                pass
            rep = ip_reputacao(ip)
            if rep:
                if rep.get('datacenter'):
                    score += 30
                    motivos.append(f'IP de datacenter/VPN ({rep.get("org") or "?"})')
                elif rep.get('rir') and rep['rir'] != 'lacnic' \
                        and (p.get('uf') or '') != '':
                    score += 20
                    motivos.append(f'IP registrado fora da América Latina '
                                   f'({rep["rir"].upper()}) com entrega no Brasil')

        # 3) Perfil da compra. Sozinho nao condena ninguem — soma pouco.
        if total >= RISCO_VALOR_MUITO_ALTO:
            score += 25
            motivos.append(f'Valor alto ({_brl(total)})')
        elif total >= RISCO_VALOR_ALTO:
            score += 15
            motivos.append(f'Valor acima da média ({_brl(total)})')
        if p.get('forma_pagto') == 'cartao' and int(p.get('parcelas') or 1) >= 6:
            score += 10
            motivos.append(f'{p["parcelas"]}x no cartão')
        if not p.get('cliente_id'):
            score += 10
            motivos.append('Checkout sem conta (visitante)')

        # 2.2) MESMO CARTÃO, outro CPF. O sinal mais forte que existe: nome,
        # e-mail, telefone, IP e até endereço o golpista troca de graça; o
        # cartão que ele conseguiu, não. Em 27/07 o VISA final 2746 pagou o
        # #56 (Natal/RN) e o #59 (Salvador/BA) — CPFs e nomes diferentes,
        # 1.400 km de distância, 4 minutos de intervalo. Os dois passaram
        # limpos pela régua anterior (score 0 e 15) porque ela não via cartão.
        if p.get('cartao_final'):
            try:
                mesmos = db_execute(
                    "SELECT DISTINCT cpf, nome, id FROM pedidos "
                    "WHERE cartao_final=%s AND cartao_bandeira IS NOT DISTINCT FROM %s "
                    "AND id<>%s AND cpf IS NOT NULL "
                    "AND criado_em > NOW() - interval '60 days'",
                    [p['cartao_final'], p.get('cartao_bandeira'), pid],
                    fetch='all') or []
                outros = {_so_digitos(m['cpf']) for m in mesmos} - {cpf, ''}
                if outros:
                    score += 50
                    quem = ', '.join(sorted({m['nome'] for m in mesmos
                                             if _so_digitos(m['cpf']) in outros})[:2])
                    motivos.append(f'Cartão final {p["cartao_final"]} também foi '
                                   f'usado por outro CPF ({quem})')
            except Exception:
                pass

        # 2.5) MESMO ENDEREÇO, outro CPF. O sinal mais forte de todos e o que
        # faltava: em 25/07 o pedido #52 ("THIAGO CORDERO PIVOTTO", CPF, e-mail,
        # telefone e IP novos) foi entregue no MESMO endereço dos #38/#39
        # ("LEONARDO FELIPE FERREIRA GONCALVES") — e pediu o MESMO produto que
        # aqueles dois tentaram levar. Identidade é barata de trocar; o lugar
        # pra onde a mercadoria tem que chegar, não.
        cep = _so_digitos(p.get('cep'))
        if cep and len(cep) == 8:
            try:
                viz = db_execute(
                    "SELECT DISTINCT cpf, nome FROM pedidos "
                    "WHERE regexp_replace(COALESCE(cep,''), '\\D', '', 'g')=%s "
                    "AND COALESCE(numero,'')=COALESCE(%s,'') AND id<>%s "
                    "AND cpf IS NOT NULL",
                    [cep, p.get('numero'), pid], fetch='all') or []
                outros_cpf = {_so_digitos(v['cpf']) for v in viz} - {cpf, ''}
                if outros_cpf:
                    score += 40
                    nomes = ', '.join(sorted({v['nome'] for v in viz
                                              if _so_digitos(v['cpf']) in outros_cpf})[:2])
                    motivos.append(f'Mesmo endereço de entrega já usado por '
                                   f'outro CPF ({nomes})')
            except Exception:
                pass

        # 3.5) Valor idêntico repetido por identidades diferentes. Assinatura
        # de teste de cartão: quem valida números roubados repete SEMPRE o
        # mesmo carrinho barato e só troca o nome. Em 24/07 saíram quatro
        # pedidos de R$ 10,76 em 55 min — "yhuj uhjkm", "Rafael Almeida",
        # "Ana Santos", "Gabriel Santos" — e o primeiro veio de outro IP, então
        # nem velocidade por IP nem valor alto pegariam. O valor pega.
        try:
            rep = db_execute(
                "SELECT COUNT(DISTINCT cpf) AS n FROM pedidos "
                "WHERE total=%s AND id<>%s AND cpf IS NOT NULL AND cpf<>%s "
                "AND criado_em > NOW() - interval '3 hours'",
                [p.get('total'), pid, p.get('cpf')], fetch='one') or {}
            if int(rep.get('n') or 0) >= 2:
                score += 35
                motivos.append(f'Mesmo valor exato ({_brl(total)}) usado por '
                               f'{int(rep["n"]) + 1} CPFs diferentes em 3h')
            elif int(rep.get('n') or 0) == 1:
                score += 20
                motivos.append(f'Mesmo valor exato ({_brl(total)}) que outro '
                               f'pedido de CPF diferente nas últimas 3h')
        except Exception:
            pass

        # 4) Mesmo CPF trocando telefone/e-mail entre pedidos.
        try:
            iguais = db_execute(
                "SELECT DISTINCT telefone, email FROM pedidos "
                "WHERE cpf=%s AND id<>%s", [p.get('cpf'), pid], fetch='all') or []
            if any(_so_digitos(o['telefone']) != _so_digitos(p.get('telefone'))
                   for o in iguais):
                score += 15
                motivos.append('Mesmo CPF já comprou com outro telefone')
        except Exception:
            pass

        db_execute("""UPDATE pedidos SET risco_score=%s, risco_motivos=%s,
                      risco_em=NOW() WHERE id=%s""",
                   [score, ' | '.join(motivos) or None, pid])
        return score, motivos
    except Exception as e:
        log.error("avaliar_risco pedido %s: %s", pid, e)
        return 0, []


def reavaliar_vizinhos(pid, ip):
    """Repontua os pedidos recentes do MESMO IP.

    Um pedido só pode ser julgado com o que existia quando ele nasceu: o #41
    valia 10 pontos até o #42 aparecer 1 minuto depois com outro CPF no mesmo
    IP. Sem reavaliar pra trás, o primeiro pedido de uma sequência de teste de
    cartão fica sempre limpo — e é justamente o que já passou.
    """
    if not ip:
        return
    try:
        irmaos = db_execute(
            "SELECT id FROM pedidos WHERE ip_cliente=%s AND id<>%s "
            "AND criado_em > NOW() - interval '24 hours' "
            "AND risco_liberado_em IS NULL ORDER BY id DESC LIMIT 10",
            [ip, pid], fetch='all') or []
        for irm in irmaos:
            antes = db_execute("SELECT risco_score FROM pedidos WHERE id=%s",
                               [irm['id']], fetch='one') or {}
            sc, mt = avaliar_risco_pedido(irm['id'])
            # Só alerta quem CRUZOU o limite agora — senão cada pedido novo
            # remandaria o mesmo aviso dos vizinhos já sinalizados.
            if sc >= RISCO_LIMITE and int(antes.get('risco_score') or 0) < RISCO_LIMITE:
                alertar_risco(irm['id'], sc, mt,
                              contexto=' — reavaliado pelo pedido seguinte')
    except Exception as e:
        log.error("reavaliar vizinhos de %s: %s", pid, e)


def alertar_risco(pid, score, motivos, contexto=''):
    """Avisa o Lucas no WhatsApp. Nunca levanta excecao pro fluxo de venda."""
    if score < RISCO_LIMITE:
        return
    try:
        p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one') or {}
        enviar_whatsapp(
            ADMIN_WHATSAPP,
            f"🚨 *Pedido #{pid} com risco alto* ({score} pts){contexto}\n\n"
            f"Comprador: {p.get('nome')} — CPF {p.get('cpf')}\n"
            f"Titular do cartão: {p.get('titular_nome') or '(não informado)'}\n"
            f"Total: *R$ {p.get('total')}* em {p.get('parcelas')}x "
            f"({p.get('forma_pagto')})\n"
            f"Entrega: {p.get('cidade')}/{p.get('uf')}\n"
            f"IP: {p.get('ip_cliente') or '—'}\n\n"
            f"*Por quê:*\n" + '\n'.join(f'• {m}' for m in motivos) + "\n\n"
            f"⛔ A etiqueta automática NÃO vai sair. Confere e libera em "
            f"https://www.luquibrinquedos.com.br/admin/pedidos")
    except Exception as e:
        log.error("alerta risco pedido %s: %s", pid, e)


# ─── Checkout: finalizar pedido ───────────────────────────────────────────────
@app.route('/api/checkout/finalizar', methods=['POST'])
def checkout_finalizar():
    d = request.get_json() or {}
    itens = carrinho_ler()
    if not itens:
        return jsonify({'erro': 'Carrinho vazio'}), 400
    # Validação básica — quando vai retirar na loja, endereço é opcional
    is_retira = (d.get('frete_servico') or '').lower().startswith('retirar')
    obrig = ['nome', 'email', 'telefone', 'cpf', 'forma_pagto']
    if not is_retira:
        obrig += ['cep', 'endereco', 'numero', 'bairro', 'cidade', 'uf']
    for c in obrig:
        if not (d.get(c) or '').strip():
            return jsonify({'erro': f'Campo {c} obrigatório'}), 400
    if d['forma_pagto'] not in ('pix', 'cartao', 'boleto'):
        return jsonify({'erro': 'Forma de pagamento inválida'}), 400
    # Chave de emergência: desliga SÓ o cartão e mantém a loja vendendo no PIX.
    # PIX não tem estorno, então zera a exposição a chargeback sem colocar o
    # site em manutenção — que derrubaria o faturamento inteiro junto.
    # Liga/desliga em site_config (`cartao_ativo` = 0/1), sem deploy.
    if d['forma_pagto'] == 'cartao' and cfg('cartao_ativo', '1') != '1':
        return jsonify({'erro': 'Pagamento no cartão temporariamente indisponível. '
                                'Finalize no PIX e ganhe desconto — a confirmação '
                                'é na hora.'}), 400
    # Cartão só para a região da loja. Fora do raio, PIX.
    if d['forma_pagto'] == 'cartao':
        ok_raio, km_raio, motivo_raio = cartao_liberado_para(d.get('cep'), is_retira)
        if not ok_raio:
            log.info("cartao barrado por distancia: cep=%s %s",
                     _so_digitos(d.get('cep')), motivo_raio)
            return jsonify({
                'erro': 'Para entregas fora de Cascavel e região aceitamos '
                        'PIX (com desconto) e boleto. É só trocar a forma de '
                        'pagamento aqui em cima — a confirmação do PIX é na hora.',
                'fora_do_raio': True}), 400
    # CPF tem que ser VÁLIDO (dígitos verificadores), não só ter 11 dígitos.
    # Sem isso um CPF digitado errado passa aqui e só é recusado no Asaas com
    # "CPF/CNPJ inválido" — a cliente trava sem entender e a venda se perde.
    cpf_digs = ''.join(c for c in (d.get('cpf') or '') if c.isdigit())
    if not cpf_valido(cpf_digs):
        return jsonify({'erro': 'CPF inválido — confira os números digitados '
                                'e tente de novo.'}), 400
    # CEP 8 digitos (se for entrega)
    if not is_retira:
        cep_digs = ''.join(c for c in (d.get('cep') or '') if c.isdigit())
        if len(cep_digs) != 8:
            return jsonify({'erro': 'CEP inválido — precisa ter 8 dígitos'}), 400
        uf_d = (d.get('uf') or '').strip().upper()
        if len(uf_d) != 2:
            return jsonify({'erro': 'UF inválida'}), 400
    # Valida que cada produto tem dados fiscais no PDV Pro (NCM/CFOP/CSOSN).
    # Sem isso a NF-e nao emite e o pedido fica sem nota — bloqueia ANTES de
    # cobrar do cliente em vez de descobrir depois do pagamento.
    if PDVPRO_API_KEY and PDVPRO_URL:
        try:
            ids = [int(it.get('produto_id') or 0) for it in itens
                   if it.get('produto_id')]
            if ids:
                r = requests.post(
                    PDVPRO_URL + '/api/integracao/validar-fiscal',
                    json={'ids': ids},
                    headers={'X-API-Key': PDVPRO_API_KEY},
                    timeout=8)
                if r.status_code == 200:
                    resp = r.json() or {}
                    if not resp.get('ok') and resp.get('problemas'):
                        falt = []
                        for pid, info in (resp['problemas'] or {}).items():
                            falt.append(f"{info.get('descricao','')} "
                                        f"(falta: {', '.join(info.get('faltando') or [])})")
                        log.warning(f"checkout bloqueado por fiscal: {falt}")
                        return jsonify({
                            'erro': ('Não conseguimos emitir nota fiscal pra '
                                     'esses produtos no momento. Por favor, '
                                     'fale com a loja pelo WhatsApp pra '
                                     'finalizar a compra: '
                                     + '; '.join(falt[:3]))
                        }), 400
        except Exception as e:
            log.warning(f"falha ao validar fiscal no PDV (segue): {e}")
    # Calcula totais
    subtotal = sum(float(it['preco']) * float(it['qtd']) for it in itens)
    # Frete: NUNCA confiar no valor vindo do cliente (era manipulável — dava pra
    # fechar pedido com frete negativo/adulterado). Re-cota no servidor e usa o
    # valor da opção escolhida. Retirar na loja = 0.
    if is_retira:
        frete = 0.0
    else:
        frete_servico_esc = (d.get('frete_servico') or '').strip()
        opcoes_frete = me_cotar(d.get('cep'), itens)
        match_frete = next(
            (o for o in opcoes_frete
             if o.get('servico') == frete_servico_esc
             or (d.get('frete_id') and str(o.get('id')) == str(d.get('frete_id')))),
            None)
        if not match_frete:
            return jsonify({'erro': 'Não conseguimos confirmar o valor do frete. '
                            'Recarregue a página e escolha a opção de entrega '
                            'novamente.'}), 400
        frete = float(match_frete['valor'])
    desconto_pix_pct = float(cfg('desconto_pix_pct', '3'))
    desconto_boleto_pct = float(cfg('desconto_boleto_pct', '3'))
    desconto = 0.0
    if d['forma_pagto'] == 'pix':
        desconto = round(subtotal * desconto_pix_pct / 100, 2)
    elif d['forma_pagto'] == 'boleto':
        desconto = round(subtotal * desconto_boleto_pct / 100, 2)
    # Cupom
    cupom_codigo = (d.get('cupom_codigo') or '').strip().upper()
    if cupom_codigo == 'PRIMEIRO10' and not CUPOM_PRIMEIRA_COMPRA_ATIVO:
        cupom_codigo = ''
    cupom_desconto = 0.0
    if cupom_codigo:
        c = db_execute("""SELECT * FROM cupons WHERE UPPER(codigo)=%s AND ativo
                          AND (valido_ate IS NULL OR valido_ate >= CURRENT_DATE)
                          AND (usos_max IS NULL OR usos < usos_max)""",
                       [cupom_codigo], fetch='one')
        if c and subtotal >= float(c['valor_min'] or 0):
            if c['tipo'] == 'pct':
                cupom_desconto = round(subtotal * float(c['valor']) / 100, 2)
            else:
                cupom_desconto = min(float(c['valor']), subtotal)
            db_execute("UPDATE cupons SET usos=usos+1 WHERE id=%s", [c['id']])
    # Pontos do Clube (max 25% do total apos descontos PIX/cupom)
    pontos_resgatados = 0.0
    desconto_pontos = 0.0
    pontos_pedidos = 0.0
    try:
        pontos_pedidos = float(d.get('pontos_usar') or 0)
    except (TypeError, ValueError):
        pontos_pedidos = 0.0
    if pontos_pedidos > 0 and CLUBE_LUQUI_ATIVO:
        info_pontos = pdv_consultar_pontos(d.get('cpf'))
        if info_pontos and info_pontos.get('cliente_existe'):
            saldo = float(info_pontos.get('saldo') or 0)
            vpp = float(info_pontos.get('valor_por_ponto') or 0)
            pontos_pedidos = min(pontos_pedidos, saldo)
            valor_em_reais = round(pontos_pedidos * vpp, 2)
            # Limite de 25% do total apos descontos
            parcial = max(0, subtotal + frete - desconto - cupom_desconto)
            limite_max = round(parcial * 0.25, 2)
            if valor_em_reais > limite_max:
                valor_em_reais = limite_max
                pontos_pedidos = round(limite_max / vpp, 2) if vpp > 0 else 0
            if valor_em_reais > 0:
                pontos_resgatados = pontos_pedidos
                desconto_pontos = valor_em_reais
    base = max(0, round(subtotal + frete - desconto - cupom_desconto - desconto_pontos, 2))
    parcelas = max(1, min(int(cfg('parcelamento_max', '12')),
                          int(d.get('parcelas') or 1)))
    # Parcelamento: sem juros so ate parcelas_sem_juros_max (default 1x).
    # Acima disso, aplica juros compostos (Tabela Price).
    max_sem_juros = int(cfg('parcelas_sem_juros_max', '1'))
    juros_am = float(cfg('juros_parcelamento_am', '2.49')) / 100.0
    juros_valor = 0.0
    total = base
    if d['forma_pagto'] == 'cartao' and parcelas > max_sem_juros and base > 0 and juros_am > 0:
        fator = (juros_am * (1 + juros_am) ** parcelas) / ((1 + juros_am) ** parcelas - 1)
        parcela_valor = round(base * fator, 2)
        total = round(parcela_valor * parcelas, 2)
        juros_valor = round(total - base, 2)
    # Mínimo por cobrança — depende de QUEM cobra, não do site.
    # O R$ 5,00 é regra do Asaas, que hoje só processa PIX e boleto. O cartão
    # migrou pra Pagar.me, que aceita bem menos (testado no sandbox: R$ 1,00
    # aprova). Manter os R$ 5 no cartão recusaria venda que o gateway aceita.
    minimo = valor_minimo_para(d['forma_pagto'])
    if total < minimo:
        falta = round(minimo - total, 2)
        msg = (f'Valor mínimo de R$ {minimo:.2f} pra fechar a compra. '
               f'Faltam R$ {falta:.2f} — adicione mais um item ou use menos pontos '
               f'do Clube.').replace('.', ',')
        return jsonify({'erro': msg}), 400
    # Cria pedido no banco (status aguardando_pagto)
    cli = cliente_logado()
    embrulho = bool(d.get('embrulho_presente'))
    embrulho_msg = ((d.get('embrulho_mensagem') or '').strip()[:300]) if embrulho else None
    embrulho_tipo_raw = (d.get('embrulho_tipo') or '').strip().lower()
    embrulho_tipo = embrulho_tipo_raw if (embrulho and embrulho_tipo_raw in ('menino', 'menina')) else None
    entrega_agendada = ((d.get('entrega_agendada') or '').strip()[:40]) or None
    # frete_servico_id: ID numerico do Melhor Envio (PAC, SEDEX, etc) que vai
    # ser usado pra gerar etiqueta automatica apos pagamento. LOCAL/RETIRA
    # nao tem etiqueta.
    fsid = (d.get('frete_servico_id') or '').strip()
    if fsid in ('LOCAL', 'RETIRA'): fsid = ''
    # Token imprevisível por pedido: as páginas /pedido/<id>/pagamento e
    # /tracking são públicas (checkout sem login) e antes eram acessíveis só
    # pelo id sequencial (IDOR — dava pra ver CPF/endereço de outro trocando o
    # id). Agora exigem ?t=<token> (ou dono logado/admin).
    ped_token = secrets.token_urlsafe(18)
    ped = db_execute("""
        INSERT INTO pedidos
          (cliente_id, email, nome, telefone, cpf, cep, endereco, numero,
           complemento, bairro, cidade, uf, subtotal, frete, desconto, total,
           forma_pagto, parcelas, frete_servico, frete_prazo, observacao,
           cupom_codigo, cupom_desconto, embrulho_presente, embrulho_mensagem,
           embrulho_tipo, juros_valor, entrega_agendada,
           pontos_resgatados, desconto_pontos, melhorenvio_servico_id, token,
           ip_cliente)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id""",
        [cli['id'] if cli else None,
         d['email'].strip().lower(), d['nome'].strip(), d['telefone'].strip(),
         d['cpf'].strip(),
         (d.get('cep') or '').strip() or None,
         (d.get('endereco') or '').strip() or None,
         (d.get('numero') or '').strip() or None,
         d.get('complemento') or None,
         (d.get('bairro') or '').strip() or None,
         (d.get('cidade') or '').strip() or None,
         (d.get('uf') or '').strip().upper() or None,
         subtotal, frete, desconto + cupom_desconto + desconto_pontos, total,
         d['forma_pagto'], parcelas,
         d.get('frete_servico') or 'A definir',
         d.get('frete_prazo') or '', d.get('observacao') or None,
         cupom_codigo or None, cupom_desconto,
         embrulho, embrulho_msg, embrulho_tipo, juros_valor, entrega_agendada,
         pontos_resgatados, desconto_pontos,
         fsid or None, ped_token, _rl_ip()],
        fetch='one')
    pid = ped['id']
    # Atualiza dados do cliente_site logado pra auto-preencher no proximo
    # checkout. Usa COALESCE pra NÃO sobrescrever endereço com NULL caso
    # cliente tenha escolhido "Retirar na loja" (não digitou endereço).
    if cli:
        try:
            db_execute("""
                UPDATE clientes_site SET
                    nome = %s, telefone = %s, cpf = %s,
                    cep = COALESCE(NULLIF(%s,''), cep),
                    endereco = COALESCE(NULLIF(%s,''), endereco),
                    numero = COALESCE(NULLIF(%s,''), numero),
                    complemento = COALESCE(NULLIF(%s,''), complemento),
                    bairro = COALESCE(NULLIF(%s,''), bairro),
                    cidade = COALESCE(NULLIF(%s,''), cidade),
                    uf = COALESCE(NULLIF(%s,''), uf)
                WHERE id = %s
            """, [
                d['nome'].strip(), d['telefone'].strip(), d['cpf'].strip(),
                (d.get('cep') or '').strip(),
                (d.get('endereco') or '').strip(),
                (d.get('numero') or '').strip(),
                (d.get('complemento') or '').strip(),
                (d.get('bairro') or '').strip(),
                (d.get('cidade') or '').strip(),
                (d.get('uf') or '').strip().upper(),
                cli['id']
            ])
        except Exception as e:
            log.warning(f"falha ao atualizar dados do cliente {cli['id']}: {e}")
    # Insere itens + reserva items da lista de aniversario (se houver)
    for it in itens:
        db_execute("""INSERT INTO pedido_itens
            (pedido_id, produto_pdv_id, codigo_barras, descricao,
             preco_unitario, quantidade, subtotal, foto_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            [pid, it['produto_id'], it.get('codigo_barras'),
             it['descricao'], it['preco'], it['qtd'],
             float(it['preco']) * float(it['qtd']),
             it.get('foto_url')])
        # Se esse item veio de uma lista de aniversario, marca como reservado
        lista_item_id = it.get('lista_item_id')
        if lista_item_id:
            try:
                db_execute("""UPDATE lista_aniversario_itens
                              SET comprado_por_nome=%s, pedido_id=%s,
                                  comprado_em=NOW()
                              WHERE id=%s AND comprado_em IS NULL""",
                           [d['nome'].strip()[:120], pid, int(lista_item_id)])
            except Exception as e:
                log.error(f"reservar lista item {lista_item_id}: {e}")
    # Antifraude: pontua o pedido assim que ele existe (IP, identidade repetida,
    # valor, parcelas). Roda ANTES da cobrança so pra o Lucas ser avisado de
    # tentativa mesmo quando o pagamento nem chega a acontecer — foi o caso dos
    # pedidos #38/#39. Nao interrompe o checkout em hipotese nenhuma.
    try:
        _sc, _mt = avaliar_risco_pedido(pid)
        alertar_risco(pid, _sc, _mt, contexto=' — pedido criado, ainda não pago')
        reavaliar_vizinhos(pid, _rl_ip())
    except Exception as e:
        log.error("antifraude checkout pedido %s: %s", pid, e)
    # Cria customer + cobrança no Asaas
    # Debita pontos no PDV (se foi usado). Se falhar, desfaz o desconto
    # pra nao dar credito de graca pro cliente.
    if pontos_resgatados > 0:
        ok_pts, msg_pts = pdv_resgatar_pontos(d['cpf'], pontos_resgatados, pid)
        if not ok_pts:
            log.error(f"Pedido {pid}: falha resgatar pontos ({msg_pts}) — desfazendo desconto")
            db_execute("""UPDATE pedidos SET pontos_resgatados=0, desconto_pontos=0,
                          desconto=desconto-%s, total=total+%s
                          WHERE id=%s""",
                       [desconto_pontos, desconto_pontos, pid])
            total += desconto_pontos
    customer_id = asaas_criar_customer(d['nome'], d['email'],
                                       d['cpf'], d['telefone'])
    if not customer_id:
        db_execute("UPDATE pedidos SET status='erro_asaas' WHERE id=%s", [pid])
        return jsonify({'erro': 'Falha ao criar cliente Asaas. '
                                 'Tente novamente ou pedido pelo WhatsApp.'}), 502
    # CARTÃO: a cobrança só nasce quando o cliente preencher os dados na
    # próxima página (checkout transparente em /pedido/<id>/pagamento). Aqui
    # só guarda o customer_id no campo asaas_link pra reusar lá.
    if d['forma_pagto'] == 'cartao':
        db_execute("UPDATE pedidos SET asaas_link=%s WHERE id=%s",
                   [f'customer:{customer_id}', pid])
        session['carrinho'] = []
        session.modified = True
        return jsonify({'ok': True, 'pedido_id': pid,
                        'pagamento_url': f'/pedido/{pid}/pagamento?t={ped_token}'})
    # PIX ou BOLETO: cria cobrança agora pra mostrar dados de pagamento na
    # própria página (QR visual / linha digitável). Cartão tem fluxo próprio.
    billing = 'PIX' if d['forma_pagto'] == 'pix' else 'BOLETO'
    cobranca = asaas_criar_cobranca(
        customer_id, total, billing,
        descricao=f'Luqui Brinquedos — Pedido #{pid}',
        parcelas=1, externa_ref=f'pedido-{pid}')
    if not cobranca:
        db_execute("UPDATE pedidos SET status='erro_asaas' WHERE id=%s", [pid])
        return jsonify({'erro': 'Falha ao gerar cobrança. '
                                 'Tente novamente ou pedido pelo WhatsApp.'}), 502
    cob_id = cobranca.get('id')
    link = cobranca.get('invoiceUrl')
    pix_payload, pix_image, boleto_url, boleto_barcode = '', '', None, None
    if d['forma_pagto'] == 'pix':
        pix = asaas_buscar_pix_qr(cob_id) or {}
        pix_payload = pix.get('payload', '')
        pix_image = pix.get('encodedImage', '')
    else:  # boleto
        boleto_url = cobranca.get('bankSlipUrl')
        # Linha digitável (identificationField) vem do endpoint específico
        info = asaas_buscar_boleto_info(cob_id) or {}
        boleto_barcode = info.get('identificationField') or ''
    db_execute("""UPDATE pedidos SET asaas_cobranca_id=%s, asaas_link=%s,
                  asaas_pix_qrcode=%s, asaas_pix_qr_image=%s,
                  asaas_boleto_url=%s, asaas_boleto_barcode=%s
                  WHERE id=%s""",
               [cob_id, link, pix_payload, pix_image,
                boleto_url, boleto_barcode, pid])
    # Limpa carrinho e devolve URL de pagamento
    session['carrinho'] = []
    session.modified = True
    return jsonify({'ok': True, 'pedido_id': pid,
                    'pagamento_url': f'/pedido/{pid}/pagamento?t={ped_token}'})


@app.route('/api/admin/asaas/conta')
@requer_admin
def admin_asaas_conta():
    """Mostra status da conta Asaas + link/instrução pra ativar 3DS."""
    return jsonify(asaas_status_conta())


# ─── Cartão só perto da loja ─────────────────────────────────────────────────
# Decisão do Lucas em 28/07/2026, depois da semana de fraude: cartão só para
# Cascavel e região; o resto do Brasil compra no PIX. A razão está nos dados —
# TODA a fraude veio de longe (Brasília, Natal, Salvador, Rio, Fortaleza),
# enquanto cliente da região é gente que ele alcança, que retira na loja e que
# tem nome a zelar na cidade. PIX não tem estorno, então venda pra fora do raio
# continua acontecendo sem risco de chargeback.
CEP_LOJA_PADRAO = '85812130'   # R. Eng. Rebouças, 2053 — Cascavel/PR

# Os 452 municipios a ate 300 km da loja, PRECOMPUTADOS, no formato
# 'NOME|UF' -- nome normalizado por _nome_norm (sem acento, caixa alta).
#
# A UF faz PARTE da chave porque a 300 km o raio atravessa a divisa: alem dos
# 291 municipios do PR entram 94 de SC, 45 do RS, 19 do MS e 3 de SP. Com o
# nome sozinho, uma "SANTA HELENA/GO" passaria pela regra achando que era a
# Santa Helena do Parana. Foi tambem por isso que a checagem antiga de "UF tem
# que ser a da loja" saiu: ela barrava Chapeco/SC antes de olhar a lista.
#
# Geocodificar o CEP na hora nao funciona: a AwesomeAPI devolve HTTP 429 pro
# IP do Railway (compartilhado, sempre quente) e a BrasilAPI nao traz
# coordenada. Por isso a lista e fixa; em troca a decisao nao depende de
# nenhuma API em tempo real.
#
# Gerada do dataset IBGE de municipios com coordenadas (5.570 linhas, varredura
# do Brasil inteiro), haversine ate -24.9506788,-53.4487927. Mais longe que
# entrou: Itaguaje/PR (300,0 km). Primeiro que ficou de fora: Vicentina/MS
# (300,1 km). Curitiba (424 km), Ponta Grossa (331), Dourados (334) e Campo
# Grande (515) seguem fora; Foz do Iguacu (132), Maringa (229), Londrina (295),
# Chapeco/SC (253) e Navirai/MS (223) entraram.
#
# A geracao anterior varria so 4 mesorregioes do PR e por isso perdia 7
# municipios que estavam DENTRO dos 120 km (Alto Piquiri, Brasilandia do Sul,
# Cafezal do Sul, Francisco Alves, Ipora, Mariluz, Perobal). Varrer o pais
# inteiro custa o mesmo e nao tem esse ponto cego.
#
# Pra mudar o raio, gere a lista de novo; nao adianta so mexer no numero --
# `cartao_raio_km` serve apenas pro texto da recusa.
CIDADES_RAIO_CARTAO = frozenset({
    'AMAMBAI|MS', 'BATAYPORA|MS', 'CAARAPO|MS', 'CORONEL SAPUCAIA|MS',
    'ELDORADO|MS', 'GLORIA DE DOURADOS|MS', 'IGUATEMI|MS', 'ITAQUIRAI|MS',
    'IVINHEMA|MS', 'JAPORA|MS', 'JATEI|MS', 'JUTI|MS', 'MUNDO NOVO|MS',
    'NAVIRAI|MS', 'NOVO HORIZONTE DO SUL|MS', 'PARANHOS|MS',
    'SETE QUEDAS|MS', 'TACURU|MS', 'TAQUARUSSU|MS', 'ALTAMIRA DO PARANA|PR',
    'ALTO PARAISO|PR', 'ALTO PARANA|PR', 'ALTO PIQUIRI|PR', 'ALTONIA|PR',
    'AMAPORA|PR', 'AMPERE|PR', 'ANAHY|PR', 'ANGULO|PR', 'APUCARANA|PR',
    'ARAPONGAS|PR', 'ARAPUA|PR', 'ARARUNA|PR', 'ARIRANHA DO IVAI|PR',
    'ASSIS CHATEAUBRIAND|PR', 'ASTORGA|PR', 'ATALAIA|PR',
    'BARBOSA FERRAZ|PR', 'BARRACAO|PR', 'BELA VISTA DA CAROBA|PR',
    'BITURUNA|PR', 'BOA ESPERANCA|PR', 'BOA ESPERANCA DO IGUACU|PR',
    'BOA VENTURA DE SAO ROQUE|PR', 'BOA VISTA DA APARECIDA|PR',
    'BOM JESUS DO SUL|PR', 'BOM SUCESSO|PR', 'BOM SUCESSO DO SUL|PR',
    'BORRAZOPOLIS|PR', 'BRAGANEY|PR', 'BRASILANDIA DO SUL|PR', 'CAFEARA|PR',
    'CAFELANDIA|PR', 'CAFEZAL DO SUL|PR', 'CALIFORNIA|PR', 'CAMBE|PR',
    'CAMBIRA|PR', 'CAMPINA DA LAGOA|PR', 'CAMPINA DO SIMAO|PR',
    'CAMPO BONITO|PR', 'CAMPO MOURAO|PR', 'CANDIDO DE ABREU|PR',
    'CANDOI|PR', 'CANTAGALO|PR', 'CAPANEMA|PR',
    'CAPITAO LEONIDAS MARQUES|PR', 'CASCAVEL|PR', 'CATANDUVAS|PR',
    'CERRO AZUL|PR', 'CEU AZUL|PR', 'CHOPINZINHO|PR', 'CIANORTE|PR',
    'CIDADE GAUCHA|PR', 'CLEVELANDIA|PR', 'COLORADO|PR', 'CORBELIA|PR',
    'CORONEL DOMINGOS SOARES|PR', 'CORONEL VIVIDA|PR',
    'CORUMBATAI DO SUL|PR', 'CRUZ MACHADO|PR', 'CRUZEIRO DO IGUACU|PR',
    'CRUZEIRO DO OESTE|PR', 'CRUZEIRO DO SUL|PR', 'CRUZMALTINA|PR',
    'DIAMANTE D OESTE|PR', 'DIAMANTE DO NORTE|PR', 'DIAMANTE DO SUL|PR',
    'DOIS VIZINHOS|PR', 'DOURADINA|PR', 'DOUTOR CAMARGO|PR',
    'ENEAS MARQUES|PR', 'ENGENHEIRO BELTRAO|PR', 'ENTRE RIOS DO OESTE|PR',
    'ESPERANCA NOVA|PR', 'ESPIGAO ALTO DO IGUACU|PR', 'FAROL|PR',
    'FAXINAL|PR', 'FENIX|PR', 'FERNANDES PINHEIRO|PR',
    'FLOR DA SERRA DO SUL|PR', 'FLORAI|PR', 'FLORESTA|PR', 'FLORIDA|PR',
    'FORMOSA DO OESTE|PR', 'FOZ DO IGUACU|PR', 'FOZ DO JORDAO|PR',
    'FRANCISCO ALVES|PR', 'FRANCISCO BELTRAO|PR', 'GENERAL CARNEIRO|PR',
    'GODOY MOREIRA|PR', 'GOIOERE|PR', 'GOIOXIM|PR', 'GRANDES RIOS|PR',
    'GUAIRA|PR', 'GUAIRACA|PR', 'GUAMIRANGA|PR', 'GUAPOREMA|PR',
    'GUARACI|PR', 'GUARANIACU|PR', 'GUARAPUAVA|PR', 'HONORIO SERPA|PR',
    'IBEMA|PR', 'ICARAIMA|PR', 'IGUARACU|PR', 'IGUATU|PR', 'IMBAU|PR',
    'IMBITUVA|PR', 'INACIO MARTINS|PR', 'INAJA|PR', 'INDIANOPOLIS|PR',
    'IPIRANGA|PR', 'IPORA|PR', 'IRACEMA DO OESTE|PR', 'IRATI|PR',
    'IRETAMA|PR', 'ITAGUAJE|PR', 'ITAIPULANDIA|PR', 'ITAMBE|PR',
    'ITAPEJARA D OESTE|PR', 'ITAUNA DO SUL|PR', 'IVAI|PR', 'IVAIPORA|PR',
    'IVATE|PR', 'IVATUBA|PR', 'JAGUAPITA|PR', 'JANDAIA DO SUL|PR',
    'JANIOPOLIS|PR', 'JAPURA|PR', 'JARDIM ALEGRE|PR', 'JESUITAS|PR',
    'JURANDA|PR', 'JUSSARA|PR', 'KALORE|PR', 'LARANJAL|PR',
    'LARANJEIRAS DO SUL|PR', 'LIDIANOPOLIS|PR', 'LINDOESTE|PR', 'LOANDA|PR',
    'LOBATO|PR', 'LONDRINA|PR', 'LUIZIANA|PR', 'LUNARDELLI|PR', 'MALLET|PR',
    'MAMBORE|PR', 'MANDAGUACU|PR', 'MANDAGUARI|PR', 'MANFRINOPOLIS|PR',
    'MANGUEIRINHA|PR', 'MANOEL RIBAS|PR', 'MARECHAL CANDIDO RONDON|PR',
    'MARIA HELENA|PR', 'MARIALVA|PR', 'MARILANDIA DO SUL|PR', 'MARILENA|PR',
    'MARILUZ|PR', 'MARINGA|PR', 'MARIOPOLIS|PR', 'MARIPA|PR',
    'MARMELEIRO|PR', 'MARQUINHO|PR', 'MARUMBI|PR', 'MATELANDIA|PR',
    'MATO RICO|PR', 'MAUA DA SERRA|PR', 'MEDIANEIRA|PR', 'MERCEDES|PR',
    'MIRADOR|PR', 'MIRASELVA|PR', 'MISSAL|PR', 'MOREIRA SALES|PR',
    'MUNHOZ DE MELO|PR', 'NOSSA SENHORA DAS GRACAS|PR',
    'NOVA ALIANCA DO IVAI|PR', 'NOVA AURORA|PR', 'NOVA CANTU|PR',
    'NOVA ESPERANCA|PR', 'NOVA ESPERANCA DO SUDOESTE|PR',
    'NOVA LARANJEIRAS|PR', 'NOVA LONDRINA|PR', 'NOVA OLIMPIA|PR',
    'NOVA PRATA DO IGUACU|PR', 'NOVA SANTA ROSA|PR', 'NOVA TEBAS|PR',
    'NOVO ITACOLOMI|PR', 'ORTIGUEIRA|PR', 'OURIZONA|PR',
    'OURO VERDE DO OESTE|PR', 'PAICANDU|PR', 'PALMAS|PR', 'PALMITAL|PR',
    'PALOTINA|PR', 'PARAISO DO NORTE|PR', 'PARANACITY|PR', 'PARANAPOEMA|PR',
    'PARANAVAI|PR', 'PATO BRAGADO|PR', 'PATO BRANCO|PR', 'PAULA FREITAS|PR',
    'PAULO FRONTIN|PR', 'PEABIRU|PR', 'PEROBAL|PR', 'PEROLA|PR',
    'PEROLA D OESTE|PR', 'PINHAL DE SAO BENTO|PR', 'PINHAO|PR',
    'PITANGA|PR', 'PITANGUEIRAS|PR', 'PLANALTINA DO PARANA|PR',
    'PLANALTO|PR', 'PORTO BARREIRO|PR', 'PORTO RICO|PR', 'PORTO VITORIA|PR',
    'PRADO FERREIRA|PR', 'PRANCHITA|PR', 'PRESIDENTE CASTELO BRANCO|PR',
    'PRUDENTOPOLIS|PR', 'QUARTO CENTENARIO|PR', 'QUATRO PONTES|PR',
    'QUEDAS DO IGUACU|PR', 'QUERENCIA DO NORTE|PR', 'QUINTA DO SOL|PR',
    'RAMILANDIA|PR', 'RANCHO ALEGRE D OESTE|PR', 'REALEZA|PR',
    'REBOUCAS|PR', 'RENASCENCA|PR', 'RESERVA|PR', 'RESERVA DO IGUACU|PR',
    'RIO AZUL|PR', 'RIO BOM|PR', 'RIO BONITO DO IGUACU|PR',
    'RIO BRANCO DO IVAI|PR', 'ROLANDIA|PR', 'RONCADOR|PR', 'RONDON|PR',
    'ROSARIO DO IVAI|PR', 'SABAUDIA|PR', 'SALGADO FILHO|PR',
    'SALTO DO LONTRA|PR', 'SANTA CRUZ DE MONTE CASTELO|PR', 'SANTA FE|PR',
    'SANTA HELENA|PR', 'SANTA ISABEL DO IVAI|PR',
    'SANTA IZABEL DO OESTE|PR', 'SANTA LUCIA|PR', 'SANTA MARIA DO OESTE|PR',
    'SANTA MONICA|PR', 'SANTA TEREZA DO OESTE|PR',
    'SANTA TEREZINHA DE ITAIPU|PR', 'SANTO ANTONIO DO CAIUA|PR',
    'SANTO ANTONIO DO SUDOESTE|PR', 'SAO CARLOS DO IVAI|PR', 'SAO JOAO|PR',
    'SAO JOAO DO CAIUA|PR', 'SAO JOAO DO IVAI|PR', 'SAO JORGE D OESTE|PR',
    'SAO JORGE DO IVAI|PR', 'SAO JORGE DO PATROCINIO|PR',
    'SAO JOSE DAS PALMEIRAS|PR', 'SAO MANOEL DO PARANA|PR',
    'SAO MIGUEL DO IGUACU|PR', 'SAO PEDRO DO IGUACU|PR',
    'SAO PEDRO DO IVAI|PR', 'SAO PEDRO DO PARANA|PR', 'SAO TOME|PR',
    'SARANDI|PR', 'SAUDADE DO IGUACU|PR', 'SERRANOPOLIS DO IGUACU|PR',
    'SULINA|PR', 'TAMARANA|PR', 'TAMBOARA|PR', 'TAPEJARA|PR', 'TAPIRA|PR',
    'TELEMACO BORBA|PR', 'TERRA BOA|PR', 'TERRA RICA|PR', 'TERRA ROXA|PR',
    'TOLEDO|PR', 'TRES BARRAS DO PARANA|PR', 'TUNEIRAS DO OESTE|PR',
    'TUPASSI|PR', 'TURVO|PR', 'UBIRATA|PR', 'UMUARAMA|PR',
    'UNIAO DA VITORIA|PR', 'UNIFLOR|PR', 'VERA CRUZ DO OESTE|PR', 'VERE|PR',
    'VIRMOND|PR', 'VITORINO|PR', 'XAMBRE|PR', 'ALPESTRE|RS',
    'AMETISTA DO SUL|RS', 'ARATIBA|RS', 'BARRA DO GUARITA|RS',
    'BARRA DO RIO AZUL|RS', 'BENJAMIN CONSTANT DO SUL|RS',
    'BOM PROGRESSO|RS', 'BRAGA|RS', 'CAICARA|RS', 'CERRO GRANDE|RS',
    'CRISSIUMAL|RS', 'CRISTAL DO SUL|RS', 'DERRUBADAS|RS',
    'DOUTOR MAURICIO CARDOSO|RS', 'ENTRE RIOS DO SUL|RS', 'ERVAL GRANDE|RS',
    'ERVAL SECO|RS', 'ESPERANCA DO SUL|RS', 'FAXINALZINHO|RS',
    'FREDERICO WESTPHALEN|RS', 'GRAMADO DOS LOUREIROS|RS', 'HUMAITA|RS',
    'IRAI|RS', 'ITATIBA DO SUL|RS', 'JABOTICABA|RS', 'LIBERATO SALZANO|RS',
    'MARIANO MORO|RS', 'MIRAGUAI|RS', 'NONOAI|RS', 'NOVO TIRADENTES|RS',
    'PALMITINHO|RS', 'PINHAL|RS', 'PINHEIRINHO DO VALE|RS', 'PLANALTO|RS',
    'RIO DOS INDIOS|RS', 'RODEIO BONITO|RS', 'SEBERI|RS',
    'TAQUARUCU DO SUL|RS', 'TENENTE PORTELA|RS', 'TIRADENTES DO SUL|RS',
    'TRES PASSOS|RS', 'TRINDADE DO SUL|RS', 'VICENTE DUTRA|RS',
    'VISTA ALEGRE|RS', 'VISTA GAUCHA|RS', 'ABELARDO LUZ|SC', 'AGUA DOCE|SC',
    'AGUAS DE CHAPECO|SC', 'AGUAS FRIAS|SC', 'ANCHIETA|SC', 'ARABUTA|SC',
    'ARVOREDO|SC', 'BANDEIRANTE|SC', 'BARRA BONITA|SC', 'BELMONTE|SC',
    'BOM JESUS|SC', 'BOM JESUS DO OESTE|SC', 'CAIBI|SC', 'CALMON|SC',
    'CAMPO ERE|SC', 'CATANDUVAS|SC', 'CAXAMBU DO SUL|SC', 'CHAPECO|SC',
    'CONCORDIA|SC', 'CORDILHEIRA ALTA|SC', 'CORONEL FREITAS|SC',
    'CORONEL MARTINS|SC', 'CUNHA PORA|SC', 'CUNHATAI|SC', 'DESCANSO|SC',
    'DIONISIO CERQUEIRA|SC', 'ENTRE RIOS|SC', 'FAXINAL DOS GUEDES|SC',
    'FLOR DO SERTAO|SC', 'FORMOSA DO SUL|SC', 'GALVAO|SC', 'GUARACIABA|SC',
    'GUARUJA DO SUL|SC', 'GUATAMBU|SC', 'IPORA DO OESTE|SC', 'IPUACU|SC',
    'IPUMIRIM|SC', 'IRACEMINHA|SC', 'IRANI|SC', 'IRATI|SC', 'ITA|SC',
    'ITAPIRANGA|SC', 'JARDINOPOLIS|SC', 'JUPIA|SC', 'LAJEADO GRANDE|SC',
    'LINDOIA DO SUL|SC', 'MACIEIRA|SC', 'MARAVILHA|SC', 'MAREMA|SC',
    'MATOS COSTA|SC', 'MODELO|SC', 'MONDAI|SC', 'NOVA ERECHIM|SC',
    'NOVA ITABERABA|SC', 'NOVO HORIZONTE|SC', 'OURO VERDE|SC', 'PAIAL|SC',
    'PALMA SOLA|SC', 'PALMITOS|SC', 'PARAISO|SC', 'PASSOS MAIA|SC',
    'PINHALZINHO|SC', 'PLANALTO ALEGRE|SC', 'PONTE SERRADA|SC',
    'PORTO UNIAO|SC', 'PRINCESA|SC', 'QUILOMBO|SC', 'RIQUEZA|SC',
    'ROMELANDIA|SC', 'SALTINHO|SC', 'SALTO VELOSO|SC', 'SANTA HELENA|SC',
    'SANTA TEREZINHA DO PROGRESSO|SC', 'SANTIAGO DO SUL|SC',
    'SAO BERNARDINO|SC', 'SAO CARLOS|SC', 'SAO DOMINGOS|SC',
    'SAO JOAO DO OESTE|SC', 'SAO JOSE DO CEDRO|SC',
    'SAO LOURENCO DO OESTE|SC', 'SAO MIGUEL DA BOA VISTA|SC',
    'SAO MIGUEL DO OESTE|SC', 'SAUDADES|SC', 'SEARA|SC', 'SERRA ALTA|SC',
    'SUL BRASIL|SC', 'TIGRINHOS|SC', 'TUNAPOLIS|SC', 'UNIAO DO OESTE|SC',
    'VARGEAO|SC', 'VARGEM BONITA|SC', 'XANXERE|SC', 'XAVANTINA|SC',
    'XAXIM|SC', 'EUCLIDES DA CUNHA PAULISTA|SP', 'ROSANA|SP',
    'TEODORO SAMPAIO|SP'
})


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def cep_info(cep):
    """{'lat','lng','cidade','uf'} do CEP. Campos podem vir vazios.

    Duas correções sobre a primeira versão, que barrou até o CEP da própria
    loja em 28/07:

    1) Ela gravava no cache MESMO sem coordenada, e depois lia esse registro
       como "já consultei, não existe" — uma resposta ruim virava veredito
       permanente. Agora só entra no cache o que tem coordenada.
    2) Uma fonte só. Agora tenta AwesomeAPI e BrasilAPI; a UF costuma vir
       mesmo quando a coordenada não vem, e a UF já resolve o caso comum.
    """
    c = _so_digitos(cep)
    if len(c) != 8:
        return {}
    try:
        row = db_execute("SELECT lat, lng, cidade, uf FROM cep_geo WHERE cep=%s",
                         [c], fetch='one')
        if row and row.get('lat') is not None:
            return {'lat': float(row['lat']), 'lng': float(row['lng']),
                    'cidade': row.get('cidade'), 'uf': row.get('uf')}
    except Exception:
        pass
    out = {}
    tentativas = [
        (f'https://cep.awesomeapi.com.br/json/{c}',
         lambda d: {'lat': d.get('lat'), 'lng': d.get('lng'),
                    'cidade': d.get('city'), 'uf': d.get('state')}),
        (f'https://brasilapi.com.br/api/cep/v2/{c}',
         lambda d: {'lat': ((d.get('location') or {}).get('coordinates') or {}).get('latitude'),
                    'lng': ((d.get('location') or {}).get('coordinates') or {}).get('longitude'),
                    'cidade': d.get('city'), 'uf': d.get('state')}),
    ]
    for url, extrai in tentativas:
        try:
            r = requests.get(url, headers={'User-Agent': 'LuquiShop/1.0'},
                             timeout=8)
            if r.status_code != 200:
                log.info("cep_info %s: %s devolveu %s", c, url.split('/')[2],
                         r.status_code)
                continue
            d = extrai(r.json() or {})
        except Exception as e:
            log.info("cep_info %s em %s: %s", c, url.split('/')[2], e)
            continue
        out = {k: v for k, v in d.items() if v}
        if out.get('lat') and out.get('lng'):
            break
    if not out:
        return {}
    if out.get('lat') and out.get('lng'):
        try:
            db_execute("""INSERT INTO cep_geo (cep, lat, lng, cidade, uf, checado_em)
                          VALUES (%s,%s,%s,%s,%s,NOW())
                          ON CONFLICT (cep) DO UPDATE SET lat=EXCLUDED.lat,
                            lng=EXCLUDED.lng, cidade=EXCLUDED.cidade,
                            uf=EXCLUDED.uf, checado_em=NOW()""",
                       [c, float(out['lat']), float(out['lng']),
                        (out.get('cidade') or '')[:120], (out.get('uf') or '')[:2]])
        except Exception:
            pass
        out['lat'] = float(out['lat'])
        out['lng'] = float(out['lng'])
    return out


def cep_coordenadas(cep):
    d = cep_info(cep)
    return (d['lat'], d['lng']) if d.get('lat') and d.get('lng') else None


def distancia_da_loja_km(cep):
    """Distância em km do CEP até a loja. None se não der pra calcular."""
    destino = cep_coordenadas(cep)
    if not destino:
        return None
    origem = cep_coordenadas(cfg('cartao_cep_loja', CEP_LOJA_PADRAO))
    if not origem:
        return None
    return _haversine_km(origem[0], origem[1], destino[0], destino[1])


def cartao_liberado_para(cep, is_retira=False):
    """(liberado, distancia_km, motivo).

    Retirada na loja sempre libera: a pessoa aparece no balcão, com o rosto e
    o documento. É o oposto do perfil que nos atacou.

    CEP que não resolve, RECUSA. Falhar fechado aqui custa uma venda no
    cartão — o cliente ainda paga no PIX; falhar aberto custa a mercadoria.
    """
    if cfg('cartao_raio_ativo', '1') != '1':
        return True, None, ''
    if is_retira:
        return True, 0.0, 'retirada na loja'
    try:
        raio = float(cfg('cartao_raio_km', '300'))
    except ValueError:
        raio = 300.0
    # A decisão sai da LISTA precomputada, não de geocodificação em tempo real:
    # a API de coordenadas devolve 429 pro IP do Railway. A cidade vem da
    # consulta de CEP, que tem três fontes e é confiável.
    info = cep_info(cep)
    uf = (info.get('uf') or '').upper()
    cidade = _nome_norm(info.get('cidade'))
    if not cidade:
        return False, None, 'não foi possível confirmar a cidade do CEP'
    # Sem UF nao da pra decidir: existe Santa Helena no PR, em SC e em GO.
    # Falha fechado, igual ao CEP que nao resolve.
    if not uf:
        return False, None, 'não foi possível confirmar o estado do CEP'
    if f'{cidade}|{uf}' in CIDADES_RAIO_CARTAO:
        return True, None, f'{info.get("cidade")}/{uf} está na região'
    return False, None, (f'{info.get("cidade")}/{uf} fica a mais de '
                         f'{raio:.0f} km da loja')


# ─── Pagar.me — cartão com 3DS ───────────────────────────────────────────────
# Por que existe: o Asaas não oferece 3DS ("não disponibilizamos o protocolo 3D
# Secure como funcionalidade configurável", suporte em 28/07/2026) e o antifraude
# deles aprovou, em 27/07, o MESMO cartão para dois CPFs em estados diferentes
# com 4 minutos de intervalo. Sem autenticação do emissor não existe
# transferência de responsabilidade — todo chargeback sobra pro lojista.
#
# PIX e boleto continuam no Asaas: na conta Pagar.me eles ainda não estão
# habilitados ("Sem ambiente configurado para este tipo de transação").
#
# O token do 3DS NÃO fica na API da Pagar.me — fica num host da Stone. Perder
# isso custa uma hora batendo 404 em /core/v5/tds-token.
PAGARME_BASE = 'https://api.pagar.me/core/v5'
PAGARME_TDS = {
    'teste':    ('https://3ds-sdx.stone.com.br/v2/tds-token',
                 'https://3ds-nx-js.stone.com.br/test/v2/3ds2.min.js'),
    'producao': ('https://3ds.stone.com.br/v2/tds-token',
                 'https://3ds-nx-js.stone.com.br/live/v2/3ds2.min.js'),
}


def valor_minimo_para(forma_pagto):
    """Menor valor que o gateway daquela forma de pagamento aceita.

    Não é regra da loja: é de quem processa. O Asaas exige R$ 5,00 e continua
    respondendo por PIX e boleto. O cartão foi pra Pagar.me, que aprova a
    partir de R$ 1,00 (verificado no sandbox). Deixar os R$ 5 valendo pro
    cartão recusaria venda que o gateway aceitaria.
    """
    if (forma_pagto == 'cartao'
            and cfg('cartao_provedor', 'asaas') == 'pagarme'):
        try:
            return float(cfg('valor_minimo_cartao', '1'))
        except ValueError:
            return 1.0
    try:
        return float(cfg('valor_minimo_asaas', '5'))
    except ValueError:
        return 5.0


def pagarme_cfg():
    """(secret, public, url_do_token_3ds, url_da_lib_js) conforme o ambiente."""
    amb = (os.environ.get('PAGARME_AMBIENTE') or 'teste').strip().lower()
    if amb not in PAGARME_TDS:
        amb = 'teste'
    suf = 'TEST' if amb == 'teste' else 'PROD'
    tds_url, js_url = PAGARME_TDS[amb]
    return (os.environ.get(f'PAGARME_SECRET_KEY_{suf}') or '',
            os.environ.get(f'PAGARME_PUBLIC_KEY_{suf}') or '',
            tds_url, js_url)


def _pagarme_headers():
    sk, _, _, _ = pagarme_cfg()
    basic = base64.b64encode(f'{sk}:'.encode()).decode()
    return {'Authorization': f'Basic {basic}',
            'Content-Type': 'application/json',
            'User-Agent': 'LuquiShop/1.0'}


def pagarme_configurado():
    return bool(pagarme_cfg()[0])


@app.route('/api/pagarme/tds-token')
def pagarme_tds_token():
    """Entrega ao navegador um token de 3DS. Vale ~20s, então é pedido na hora.

    A chave secreta NUNCA vai pro browser — por isso este proxy existe.
    """
    if not pagarme_configurado():
        return jsonify({'erro': 'pagarme não configurado'}), 503
    if not rate_limit_ok('tds_token', _rl_ip(), 30, 900):
        return jsonify({'erro': 'muitas tentativas'}), 429
    _, _, tds_url, _ = pagarme_cfg()
    try:
        r = requests.get(tds_url, headers=_pagarme_headers(), timeout=12)
        if r.status_code != 200:
            log.error("tds-token %s: %s", r.status_code, r.text[:200])
            return jsonify({'erro': 'falha ao iniciar autenticação'}), 502
        return jsonify({'tds_token': (r.json() or {}).get('tds_token')})
    except Exception as e:
        log.error("tds-token: %s", e)
        return jsonify({'erro': 'falha ao iniciar autenticação'}), 502


def _pagarme_endereco(p):
    num = (p.get('numero') or 'S/N')
    rua = (p.get('endereco') or 'Nao informado')
    bairro = (p.get('bairro') or '')
    return {
        'country': 'BR',
        'state': (p.get('uf') or 'PR')[:2].upper(),
        'city': (p.get('cidade') or 'Cascavel')[:64],
        'zip_code': _so_digitos(p.get('cep')) or '85812130',
        'line_1': f'{num}, {rua}, {bairro}'.strip(' ,')[:256],
        # A biblioteca de 3DS EXIGE line_2 e recusa a autenticação inteira sem
        # ele: "order.shipping.address.line_2 is required". A maioria dos
        # pedidos não tem complemento, então manda o bairro — e, se nem isso,
        # um traço. Campo vazio conta como ausente.
        'line_2': (p.get('complemento') or bairro or '-')[:64],
    }


def _pagarme_cliente(p):
    tel = _so_digitos(p.get('telefone'))
    ddd, numero = (tel[:2], tel[2:]) if len(tel) >= 10 else ('45', '991119800')
    return {
        'name': (p.get('nome') or 'Cliente')[:64],
        'email': (p.get('email') or '')[:64],
        'document': _so_digitos(p.get('cpf')),
        'document_type': 'CPF',
        'type': 'individual',
        'phones': {'mobile_phone': {'country_code': '55',
                                    'area_code': ddd, 'number': numero}},
        'address': _pagarme_endereco(p),
    }


def _pagarme_itens(pid, p):
    itens = db_execute("SELECT * FROM pedido_itens WHERE pedido_id=%s",
                       [pid], fetch='all') or []
    out = []
    for i in itens:
        out.append({'amount': int(round(float(i['preco_unitario']) * 100)),
                    'description': (i['descricao'] or 'Produto')[:64],
                    'quantity': int(float(i['quantidade']) or 1),
                    'code': str(i.get('produto_pdv_id') or i['id'])})
    # A Pagar.me confere itens x total. Frete, juros e desconto entram como
    # linhas próprias pra soma fechar — senão a cobrança nasce com valor
    # diferente do que o cliente viu na tela.
    soma = sum(x['amount'] * x['quantity'] for x in out)
    alvo = int(round(float(p['total']) * 100))
    if alvo != soma:
        dif = alvo - soma
        out.append({'amount': dif, 'quantity': 1, 'code': 'ajuste',
                    'description': 'Frete/juros' if dif > 0 else 'Desconto'})
    return out


def pagarme_dados_3ds(pid):
    """Payload que o navegador manda pra biblioteca de 3DS. Não inclui CVV nem
    número do cartão — quem preenche isso é o próprio formulário, no browser."""
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return None
    return {
        'customer': _pagarme_cliente(p),
        'items': [{'description': i['description'], 'code': i['code']}
                  for i in _pagarme_itens(pid, p)],
        'shipping': {'recipient_name': (p.get('nome') or 'Cliente')[:64],
                     'address': _pagarme_endereco(p)},
        'requestor_url': SITE_URL,
        'valor_centavos': int(round(float(p['total']) * 100)),
        'parcelas': int(p.get('parcelas') or 1),
        'billing_address': _pagarme_endereco(p),
    }


def pagarme_criar_cobranca(pid, cartao, tds_trans_id=None):
    """Cria o pedido+cobrança na Pagar.me. Devolve (http_status, dict)."""
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return 404, {'erro': 'pedido não encontrado'}
    cc = {
        'installments': int(p.get('parcelas') or 1),
        'statement_descriptor': 'LUQUIBRINQ',
        'card': {
            'number': cartao['numero'],
            'holder_name': cartao['titular_nome'][:64],
            'exp_month': int(cartao['mes']),
            'exp_year': int(cartao['ano']),
            'cvv': cartao['ccv'],
            'billing_address': _pagarme_endereco(p),
        },
    }
    # É ESTE campo que amarra a autenticação do emissor à cobrança. Sem ele a
    # transação vira uma compra comum e a responsabilidade continua sua.
    if tds_trans_id:
        cc['transaction_id'] = tds_trans_id
    body = {
        'items': _pagarme_itens(pid, p),
        'customer': _pagarme_cliente(p),
        'payments': [{'payment_method': 'credit_card', 'credit_card': cc}],
        'code': f'pedido-{pid}',
        'closed': True,
    }
    try:
        r = requests.post(f'{PAGARME_BASE}/orders', json=body,
                          headers=_pagarme_headers(), timeout=45)
        try:
            d = r.json()
        except Exception:
            d = {'raw': r.text[:400]}
        return r.status_code, d
    except Exception as e:
        log.error("pagarme criar cobranca pedido %s: %s", pid, e)
        return 502, {'erro': str(e)[:200]}


def _pagar_cartao_pagarme(pid, p, cartao, tds):
    """Cobra pela Pagar.me exigindo (ou não) autenticação do emissor.

    Tudo ajustável por site_config, sem deploy:
      cartao_provedor     asaas | pagarme
      tds_ativo           1 = pede autenticação; 0 = cobra direto
      tds_status_aceitos  quais respostas do emissor valem (padrão só 'Y')
      tds_obrigatorio     1 = sem autenticação boa, recusa a compra

    Sobre os status: 'Y' é o único que GARANTE transferência de
    responsabilidade. 'A' é tentativa de autenticação (o emissor não
    participou) e costuma transferir também, mas depende da bandeira — por
    isso fica de fora do padrão e o Lucas liga se quiser mais aprovação.
    """
    tds = tds or {}
    trans_id = (tds.get('tds_server_trans_id') or '').strip() or None
    status_3ds = (tds.get('trans_status') or '').strip().upper()
    aceitos = [s.strip().upper() for s in
               cfg('tds_status_aceitos', 'Y').split(',') if s.strip()]
    exige = cfg('tds_ativo', '1') == '1'
    obrigatorio = cfg('tds_obrigatorio', '1') == '1'

    autenticado = bool(trans_id) and status_3ds in aceitos
    if exige and obrigatorio and not autenticado:
        # A mensagem muda conforme o motivo, porque a AÇÃO do cliente muda:
        # em 'U' insistir no mesmo cartão não adianta (o emissor não participa
        # do 3DS), enquanto num desafio cancelado basta refazer.
        if tds.get('challenge_canceled'):
            msg = ('Você cancelou a confirmação do banco. Tente de novo e '
                   'conclua a verificação para finalizar a compra.')
        elif status_3ds in ('U', 'R'):
            msg = ('O banco deste cartão não oferece a confirmação de '
                   'segurança que exigimos. Finalize no PIX (com desconto e '
                   'confirmação na hora) ou no boleto — é só clicar abaixo.')
        elif status_3ds == 'N':
            msg = ('Seu banco não reconheceu esta compra como sua. Confira os '
                   'dados do cartão ou finalize no PIX.')
        elif status_3ds:
            msg = ('Seu banco não confirmou esta compra (código '
                   f'{status_3ds}). Tente outro cartão ou finalize no PIX.')
        else:
            msg = ('Não foi possível confirmar a compra com o seu banco. '
                   'Tente de novo ou finalize no PIX.')
        log.warning("pedido %s recusado no 3DS: status=%s trans_id=%s",
                    pid, status_3ds or '-', bool(trans_id))
        return jsonify({'erro': msg, 'sem_3ds': True}), 402

    st, d = pagarme_criar_cobranca(pid, cartao,
                                   trans_id if autenticado else None)
    charges = (d or {}).get('charges') or []
    ch = charges[0] if charges else {}
    tx = ch.get('last_transaction') or {}
    status_ch = (ch.get('status') or '').lower()

    if st not in (200, 201) or status_ch in ('failed', 'refused', ''):
        # A recusa do EMISSOR vem em `acquirer_message` — não em
        # gateway_response.errors. Quando o banco nega, a chamada à API deu
        # certo (gateway_response fica {"code": "200"}) e só os errors ficam
        # vazios; procurar o motivo ali devolvia sempre o genérico "Transação
        # não autorizada". Aconteceu em 06/08/2026 no primeiro teste real: o
        # emissor respondeu "Cartão vencido ou data de vencimento incorreta"
        # (código 1001) e nem o dono da loja conseguiu saber disso — mesma
        # classe do problema que o TEF do PDV Pro teve em julho.
        motivo = (tx.get('acquirer_message') or '').strip()
        if not motivo:
            try:
                errs = (tx.get('gateway_response') or {}).get('errors') or []
                motivo = (errs[0].get('message') if errs else '') or ''
                motivo = motivo.split('|')[-1].strip()
            except Exception:
                pass
        motivo = motivo or (d or {}).get('message') or 'Transação não autorizada'
        # O texto do adquirente é escrito pro LOJISTA ("Oriente o usuário a
        # contatar o banco"). Pra tela do cliente fica só a primeira parte,
        # que é a que diz o que ele tem que fazer.
        motivo = re.split(r'\.\s*Oriente\b', motivo)[0].strip(' .')
        log.warning("pedido %s pagarme recusado: http=%s status=%s cod=%s msg=%s",
                    pid, st, status_ch,
                    tx.get('acquirer_return_code') or '-', motivo[:160])
        return jsonify({'erro': f'Pagamento não autorizado. {motivo[:140]}'}), 402

    # Pago. Guarda o rastro do cartão e da autenticação antes de seguir.
    card = tx.get('card') or {}
    try:
        db_execute("""UPDATE pedidos SET asaas_cobranca_id=%s, cartao_final=%s,
                      cartao_bandeira=%s WHERE id=%s""",
                   [str(ch.get('id') or d.get('id') or '')[:60],
                    str(card.get('last_four_digits') or '')[:4],
                    (card.get('brand') or '')[:20], pid])
    except Exception as e:
        log.warning("gravar cartao pagarme pedido %s: %s", pid, e)
    log.info("pedido %s PAGO na pagarme (3ds=%s status=%s charge=%s)",
             pid, status_3ds or 'off', status_ch, ch.get('id'))

    virou = db_execute("UPDATE pedidos SET status='pago', pago_em=NOW() "
                       "WHERE id=%s AND status='aguardando_pagto' RETURNING id",
                       [pid], fetch='one')
    if virou:
        processar_pedido_pago(pid)
    return jsonify({'ok': True, 'status': 'pago'})


@app.route('/api/pedido/<int:pid>/pagar-cartao', methods=['POST'])
def pedido_pagar_cartao(pid):
    """Checkout transparente — cliente preenche cartão na própria página da
    loja; servidor encaminha pro Asaas via POST /payments com creditCard
    inline. Cartão NÃO é persistido em lugar nenhum."""
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'Pedido não encontrado'}), 404
    if p['forma_pagto'] != 'cartao':
        return jsonify({'erro': 'Este pedido não é cartão'}), 400
    if p['status'] != 'aguardando_pagto':
        return jsonify({'erro': f'Pedido com status "{p["status"]}" — '
                                 'não dá pra reprocessar'}), 400

    # ── Freio de teste de cartão ──────────────────────────────────────────
    # Recusa NÃO muda o status do pedido, então o guard acima não impedia
    # nada: dava pra reaproveitar o mesmo pedido e mandar cartão atrás de
    # cartão pra sempre. Em 24/07 o pedido #48 recebeu 67 tentativas em
    # 5min30. Isso não é prejuízo direto, mas é o excesso de autorização
    # negada que faz adquirente marcar (e derrubar) conta de lojista.
    # Cliente legítimo erra o cartão 2 ou 3 vezes, não 67.
    if cfg('cartao_ativo', '1') != '1':
        log.warning("cartao-barrado pedido=%s motivo=cartao_desativado", pid)
        return jsonify({'erro': 'Pagamento no cartão temporariamente '
                                'indisponível. Fale com a loja pelo WhatsApp.',
                        'whatsapp': cfg('whatsapp_loja', WHATSAPP_LOJA)}), 503
    # Mesma trava do checkout, repetida aqui de propósito: pedido antigo pode
    # ter nascido antes da regra, e esta rota é chamável direto.
    _retira = (p.get('frete_servico') or '').lower().startswith('retirar')
    _ok_raio, _, _mot = cartao_liberado_para(p.get('cep'), _retira)
    if not _ok_raio:
        log.warning("cartao-barrado pedido=%s motivo=fora_do_raio (%s)", pid, _mot)
        return jsonify({'erro': 'Para a sua região aceitamos PIX e boleto. '
                                'Fale com a loja que a gente te ajuda a '
                                'finalizar.',
                        'whatsapp': cfg('whatsapp_loja', WHATSAPP_LOJA)}), 403
    tentativas = int(p.get('tentativas_cartao') or 0)
    if tentativas >= MAX_TENTATIVAS_CARTAO:
        log.warning("cartao-barrado pedido=%s motivo=max_tentativas (%s)",
                    pid, tentativas)
        return jsonify({
            'erro': 'Muitas tentativas de pagamento neste pedido. Fale com a '
                    'loja pelo WhatsApp que a gente finaliza pra você.',
            'whatsapp': cfg('whatsapp_loja', WHATSAPP_LOJA)}), 429
    if not rate_limit_ok('cartao_ip', _rl_ip(), 12, 900):
        log.warning("cartao-barrado pedido=%s motivo=rate_limit_ip", pid)
        return jsonify({
            'erro': 'Muitas tentativas de pagamento. Aguarde alguns minutos.'}), 429
    db_execute("UPDATE pedidos SET tentativas_cartao=COALESCE(tentativas_cartao,0)+1 "
               "WHERE id=%s", [pid])
    if tentativas + 1 == MAX_TENTATIVAS_CARTAO:
        try:
            enviar_whatsapp(ADMIN_WHATSAPP,
                f"🃏 *Pedido #{pid} travado por tentativas de cartão*\n\n"
                f"{MAX_TENTATIVAS_CARTAO} cartões diferentes tentados no mesmo "
                f"pedido — cara de teste de cartão roubado.\n"
                f"Comprador: {p.get('nome')}\nIP: {p.get('ip_cliente') or '—'}\n\n"
                f"Não precisa fazer nada: o pedido não aceita mais cartão.")
        except Exception:
            pass

    d = request.get_json(silent=True) or {}
    num = ''.join(c for c in (d.get('numero') or '') if c.isdigit())
    mes = (d.get('validade_mes') or '').strip().zfill(2)
    ano = (d.get('validade_ano') or '').strip()
    if len(ano) == 2:
        ano = '20' + ano
    ccv = ''.join(c for c in (d.get('ccv') or '') if c.isdigit())
    holder_nome = (d.get('titular_nome') or '').strip().upper()[:80]
    holder_cpf = ''.join(c for c in (d.get('titular_cpf') or '') if c.isdigit())
    # Por que este log existe: em 17/08/2026 o cartão ficou 17 dias sem
    # aprovar nada e 4 de 5 tentativas nem chegaram ao Pagar.me. Como cada
    # barreira devolvia 4xx sem dizer qual era, não houve como saber onde
    # morreram. Agora toda saída antes da cobrança grita "cartao-barrado
    # motivo=...", e a próxima tentativa se explica sozinha.
    if not (12 <= len(num) <= 19):
        log.warning("cartao-barrado pedido=%s motivo=numero_invalido (%s dig)",
                    pid, len(num))
        return jsonify({'erro': 'Número do cartão inválido'}), 400
    if not (3 <= len(ccv) <= 4):
        log.warning("cartao-barrado pedido=%s motivo=cvv_invalido", pid)
        return jsonify({'erro': 'CVV inválido'}), 400
    try:
        if not (1 <= int(mes) <= 12) or int(ano) < datetime.now().year:
            raise ValueError
    except ValueError:
        log.warning("cartao-barrado pedido=%s motivo=validade_invalida (%s/%s)",
                    pid, mes, ano)
        return jsonify({'erro': 'Validade do cartão inválida'}), 400
    if len(holder_nome) < 3:
        log.warning("cartao-barrado pedido=%s motivo=titular_sem_nome", pid)
        return jsonify({'erro': 'Nome do titular obrigatório'}), 400
    if len(holder_cpf) not in (11, 14):
        log.warning("cartao-barrado pedido=%s motivo=titular_sem_cpf", pid)
        return jsonify({'erro': 'CPF/CNPJ do titular obrigatório'}), 400

    # Guarda quem pagou ANTES de escolher o provedor — o antifraude usa isso
    # nos dois caminhos.
    try:
        db_execute("""UPDATE pedidos SET titular_nome=%s, titular_cpf=%s,
                      ip_cliente=COALESCE(ip_cliente,%s) WHERE id=%s""",
                   [holder_nome, holder_cpf, _rl_ip(), pid])
        sc, mt = avaliar_risco_pedido(pid)
        alertar_risco(pid, sc, mt, contexto=' — cartão sendo processado agora')
        reavaliar_vizinhos(pid, p.get('ip_cliente') or _rl_ip())
    except Exception as e:
        log.error("antifraude cartao pedido %s: %s", pid, e)

    # ── Qual provedor cobra este cartão ───────────────────────────────────
    # PIX e boleto seguem no Asaas; só o cartão migra, porque só o cartão tem
    # chargeback e só a Pagar.me oferece 3DS.
    if cfg('cartao_provedor', 'asaas') == 'pagarme' and pagarme_configurado():
        return _pagar_cartao_pagarme(pid, p, {
            'numero': num, 'mes': mes, 'ano': ano, 'ccv': ccv,
            'titular_nome': holder_nome, 'titular_cpf': holder_cpf,
        }, (d.get('tds') or {}))

    # Reusa customer criado no /finalizar (guardado como "customer:<id>" no link)
    customer_id = None
    link_atual = (p.get('asaas_link') or '')
    if link_atual.startswith('customer:'):
        customer_id = link_atual.split(':', 1)[1]
    if not customer_id:
        customer_id = asaas_criar_customer(p['nome'], p['email'],
                                           p['cpf'], p['telefone'])
    if not customer_id:
        return jsonify({'erro': 'Falha ao registrar comprador no gateway'}), 502

    cc = {
        'holderName': holder_nome,
        'number': num,
        'expiryMonth': mes,
        'expiryYear': ano,
        'ccv': ccv,
    }
    holder = {
        'name': holder_nome,
        'email': p['email'],
        'cpfCnpj': holder_cpf,
        'postalCode': ''.join(c for c in (p.get('cep') or '') if c.isdigit()) or '00000000',
        'addressNumber': (p.get('numero') or 'S/N')[:10],
        'phone': ''.join(c for c in (p.get('telefone') or '') if c.isdigit())[:11] or '0000000000',
    }
    remote_ip = (request.headers.get('X-Forwarded-For')
                 or request.remote_addr or '0.0.0.0').split(',')[0].strip()

    # (o antifraude e a gravação do titular já rodaram acima, antes de
    # escolher o provedor)
    code, resp = asaas_criar_cobranca_cartao(
        customer_id, p['total'],
        f'Luqui Brinquedos — Pedido #{pid}',
        p.get('parcelas') or 1, f'pedido-{pid}',
        cc, holder, remote_ip)

    if code not in (200, 201):
        msg = 'Não foi possível processar o pagamento'
        try:
            errs = (resp or {}).get('errors') or []
            if errs and errs[0].get('description'):
                msg = errs[0]['description']
        except Exception:
            pass
        log.warning(f"pedido {pid} cartao recusado: code={code} msg={msg}")
        # Fallback: a recusa do checkout transparente quase sempre é o emissor
        # negando transação não autenticada. A fatura hospedada do Asaas roda
        # antifraude/3DS própria e aprova onde o transparente apanha, então
        # oferecemos esse caminho em vez de deixar o cliente na mão.
        fallback = (p.get('asaas_link') or '')
        if not fallback.startswith('http'):
            cob = asaas_criar_cobranca(
                customer_id, p['total'], 'CREDIT_CARD',
                f'Luqui Brinquedos — Pedido #{pid}',
                parcelas=p.get('parcelas') or 1,
                externa_ref=f'pedido-{pid}')
            fallback = (cob or {}).get('invoiceUrl') or ''
            if fallback:
                db_execute("""UPDATE pedidos SET asaas_cobranca_id=%s,
                              asaas_link=%s WHERE id=%s""",
                           [(cob or {}).get('id'), fallback, pid])
        if fallback:
            return jsonify({'erro': msg, 'fallback_url': fallback,
                            'fallback_msg': 'Finalize pela página segura do '
                                            'Asaas — costuma aprovar quando o '
                                            'banco recusa aqui.'}), 402
        return jsonify({'erro': msg}), 402

    cob_id = resp.get('id')
    link = resp.get('invoiceUrl') or ''
    # Guarda os 4 últimos + bandeira que o Asaas devolve. É o que permite ver
    # o mesmo cartão pagando com identidades diferentes.
    try:
        cc_resp = resp.get('creditCard') or {}
        if cc_resp.get('creditCardNumber'):
            db_execute("UPDATE pedidos SET cartao_final=%s, cartao_bandeira=%s "
                       "WHERE id=%s",
                       [str(cc_resp.get('creditCardNumber'))[:4],
                        (cc_resp.get('creditCardBrand') or '')[:20], pid])
    except Exception as e:
        log.warning("guardar final do cartao pedido %s: %s", pid, e)
    status_asaas = resp.get('status', '')  # CONFIRMED, RECEIVED, PENDING, AWAITING_RISK_ANALYSIS
    db_execute("""UPDATE pedidos SET asaas_cobranca_id=%s, asaas_link=%s
                  WHERE id=%s""",
               [cob_id, link, pid])
    if status_asaas in ('CONFIRMED', 'RECEIVED'):
        # RETURNING pra saber se ESTA chamada foi a que virou o pedido. Se o
        # webhook chegou primeiro, o UPDATE não pega nada e não processamos de
        # novo — sem isso, os dois caminhos correriam juntos e o cliente
        # receberia e-mail dobrado (e a venda entraria duas vezes no PDV).
        virou = db_execute("UPDATE pedidos SET status='pago', pago_em=NOW() "
                           "WHERE id=%s AND status='aguardando_pagto' "
                           "RETURNING id", [pid], fetch='one')
        if virou:
            processar_pedido_pago(pid)
        return jsonify({'ok': True, 'status': 'pago'})
    # 3D Secure: quando a conta Asaas tem 3DS ativo, a resposta traz uma URL
    # de autenticação. Abrimos numa popup; webhook confirma depois.
    redirect_3ds = (resp.get('creditCard') or {}).get('authenticationUrl') \
        or resp.get('authorizationUrl') or resp.get('paymentLink')
    if redirect_3ds:
        return jsonify({'ok': True, 'status': '3ds',
                        'redirect_3ds': redirect_3ds,
                        'mensagem': 'Confirme com seu banco para autorizar a compra'})
    # AWAITING_RISK_ANALYSIS / PENDING: análise antifraude; webhook confirma.
    return jsonify({'ok': True, 'status': 'analise',
                    'mensagem': 'Pagamento em análise — você será avisado em segundos'})


@app.route('/api/pedido/<int:pid>/trocar-pra-pix', methods=['POST'])
def pedido_trocar_pra_pix(pid):
    """Converte um pedido de cartão em PIX, sem refazer nada.

    Quando o cartão é recusado (banco negando, 3DS falhando), o cliente ficava
    preso: a página de pagamento só oferecia cartão, e a única saída era
    abandonar e montar o carrinho de novo. Isso é perder a venda com o
    comprador já decidido.

    O desconto do PIX é aplicado, então o total muda — por isso a cobrança
    velha é cancelada e nasce uma nova.
    """
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p or not pedido_acesso_ok(p):
        abort(404)
    if p['status'] != 'aguardando_pagto':
        return jsonify({'erro': 'Este pedido já foi processado'}), 400
    if p['forma_pagto'] == 'pix':
        return jsonify({'ok': True, 'ja_era_pix': True})

    # Refaz a conta: tira juros de parcelamento e aplica o desconto do PIX.
    bruto = float(p['subtotal']) + float(p['frete'] or 0) - float(p['desconto'] or 0)
    pct = float(cfg('desconto_pix_pct', '3'))
    total = round(max(0.0, bruto * (1 - pct / 100)), 2)
    minimo = valor_minimo_para('pix')
    if total < minimo:
        return jsonify({'erro': f'No PIX o total fica abaixo do mínimo de '
                                f'R$ {minimo:.2f}.'.replace('.', ',')}), 400

    customer_id = asaas_criar_customer(p['nome'], p['email'], p['cpf'],
                                       p['telefone'])
    if not customer_id:
        return jsonify({'erro': 'Não consegui gerar o PIX agora. '
                                'Tente de novo em instantes.'}), 502
    cob = asaas_criar_cobranca(customer_id, total, 'PIX',
                               f'Luqui Brinquedos — Pedido #{pid}',
                               externa_ref=f'pedido-{pid}')
    if not cob or not cob.get('id'):
        return jsonify({'erro': 'Não consegui gerar o PIX agora. '
                                'Tente de novo em instantes.'}), 502
    pix = asaas_buscar_pix_qr(cob['id']) or {}
    db_execute("""UPDATE pedidos SET forma_pagto='pix', parcelas=1, juros_valor=0,
                  total=%s, desconto=%s, asaas_cobranca_id=%s, asaas_link=%s,
                  asaas_pix_qrcode=%s, asaas_pix_qr_image=%s, atualizado_em=NOW()
                  WHERE id=%s AND status='aguardando_pagto'""",
               [total, round(float(p['desconto'] or 0) + (bruto - total), 2),
                cob['id'], cob.get('invoiceUrl') or '',
                pix.get('payload') or '', pix.get('encodedImage') or '', pid])
    log.info("pedido %s trocado de cartao pra PIX (R$ %.2f)", pid, total)
    return jsonify({'ok': True, 'total': total,
                    'url': f'/pedido/{pid}/pagamento?t={p.get("token") or ""}'})


@app.route('/pedido/<int:pid>/pagamento')
def pedido_pagamento(pid):
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p or not pedido_acesso_ok(p):
        abort(404)
    # Lazy-fetch da imagem do QR Code PIX se faltou na criação do pedido
    # (pedidos antigos podem ter só o payload sem a imagem). Busca da API
    # Asaas e salva pra próxima vez.
    if (p.get('forma_pagto') == 'pix'
        and p.get('asaas_cobranca_id')
        and not p.get('asaas_pix_qr_image')):
        try:
            pix = asaas_buscar_pix_qr(p['asaas_cobranca_id']) or {}
            img = pix.get('encodedImage', '')
            payload = pix.get('payload', '') or p.get('asaas_pix_qrcode', '')
            if img:
                db_execute("""UPDATE pedidos SET asaas_pix_qr_image=%s,
                              asaas_pix_qrcode=COALESCE(NULLIF(%s,''), asaas_pix_qrcode)
                              WHERE id=%s""",
                           [img, payload, pid])
                p = dict(p)
                p['asaas_pix_qr_image'] = img
                if payload:
                    p['asaas_pix_qrcode'] = payload
        except Exception as e:
            log.warning(f"lazy pix qr pedido {pid}: {e}")
    itens = db_execute(
        "SELECT * FROM pedido_itens WHERE pedido_id=%s ORDER BY id",
        [pid], fetch='all') or []
    # Dados pro 3DS. Só monta quando o cartão está indo pela Pagar.me — se o
    # provedor for o Asaas, a página segue exatamente como era.
    usa_pagarme = (p.get('forma_pagto') == 'cartao'
                   and cfg('cartao_provedor', 'asaas') == 'pagarme'
                   and pagarme_configurado())
    tds_js = tds_dados = None
    if usa_pagarme:
        tds_js = pagarme_cfg()[3]
        tds_dados = pagarme_dados_3ds(pid)
    return render_template('pedido_pagamento.html',
                           p=p, itens=itens,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           usa_pagarme=usa_pagarme,
                           tds_js=tds_js, tds_dados=tds_dados,
                           tds_ativo=(cfg('tds_ativo', '1') == '1'),
                           carrinho=carrinho_ler())


@app.route('/api/pedido/<int:pid>/status')
def api_pedido_status(pid):
    """Polling pra página de pagamento detectar quando pago/cancelado."""
    p = db_execute("SELECT status, pago_em FROM pedidos WHERE id=%s",
                   [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'não encontrado'}), 404
    return jsonify({'status': p['status'],
                    'pago_em': p['pago_em'].isoformat() if p.get('pago_em') else None})


@app.route('/api/pedido/<int:pid>/nfe')
def pedido_nfe(pid):
    """Devolve URLs da NF-e (DANFE PDF + XML) consultando o PDV Pro."""
    c = cliente_logado()
    p = db_execute("""SELECT id, cliente_id, email, nfe_ref FROM pedidos
                       WHERE id=%s""", [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'pedido nao encontrado'}), 404
    # Confere posse: cliente logado bate, ou email bate
    if c:
        if p.get('cliente_id') and p['cliente_id'] != c['id']:
            return jsonify({'erro': 'acesso negado'}), 403
    else:
        return jsonify({'erro': 'faca login'}), 401
    if not p.get('nfe_ref'):
        return jsonify({'erro': 'nf ainda nao emitida'}), 404
    if not PDVPRO_API_KEY:
        return jsonify({'erro': 'integracao nao configurada'}), 503
    try:
        r = requests.get(PDVPRO_URL + f'/api/integracao/nfe/{p["nfe_ref"]}',
                         headers={'X-API-Key': PDVPRO_API_KEY}, timeout=10)
        if r.status_code == 200:
            return jsonify(r.json())
        return jsonify({'erro': 'NF indisponivel'}), 502
    except Exception as e:
        log.error("consultar NF: %s", e)
        return jsonify({'erro': str(e)}), 500


@app.route('/api/pedido/<int:pid>/status')
def pedido_status(pid):
    p = db_execute("SELECT id, status, pago_em FROM pedidos WHERE id=%s",
                   [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'Pedido não encontrado'}), 404
    return jsonify({'status': p['status'],
                    'pago_em': p['pago_em'].isoformat() if p['pago_em'] else None})


# ─── Envia pedido pro PDV Pro (cria venda + baixa estoque + emite NF) ─────────
def _enviar_pedido_pro_pdv(pid):
    """Monta payload do pedido e POSTa no /api/integracao/pedido do PDV Pro.
    Retorna (ok, resposta_dict_ou_None). Atualiza pedidos.pdv_venda_id +
    nfe_ref + tentativas_pdv. Reutilizado pelo webhook Asaas E pelo cron de
    reconciliação (caso o webhook falhe no meio — Railway pode reiniciar
    o serviço durante o request)."""
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return False, None
    itens = db_execute("SELECT * FROM pedido_itens WHERE pedido_id=%s",
                       [pid], fetch='all') or []
    pdv_payload = {
        'pedido_id': pid,
        'cliente': {'nome': p['nome'], 'email': p['email'],
                    'cpf': p['cpf'], 'telefone': p['telefone']},
        'endereco': {
            'cep': p['cep'], 'endereco': p['endereco'],
            'numero': p['numero'], 'complemento': p.get('complemento'),
            'bairro': p['bairro'], 'cidade': p['cidade'], 'uf': p['uf']
        },
        'itens': [{'produto_id': i['produto_pdv_id'],
                   'descricao': i['descricao'],
                   'preco_unitario': float(i['preco_unitario']),
                   'quantidade': float(i['quantidade']),
                   'subtotal': float(i['subtotal'])} for i in itens],
        'total': float(p['total']),
        'desconto': float(p['desconto']),
        'frete': float(p['frete']),
        # Juros do parcelamento no cartao. PRECISA ir separado: `total` ja vem
        # com eles embutidos, mas a NF-e soma itens + frete e ignora juros. Sem
        # esse campo o PDV grava acrescimo=0 e declara um pagamento MAIOR que o
        # total da nota -> SEFAZ 866 "ausencia de troco". Quebrou o pedido #35
        # (R$ 1.060,77 em 3x, R$ 50,71 de juros) em 23/07/2026 -- o primeiro
        # pedido do site parcelado no cartao.
        'juros': float(p.get('juros_valor') or 0),
        'forma_pagto': p['forma_pagto'],
        'frete_servico': p.get('frete_servico') or '',
        # A venda entra no PDV (baixa estoque, conta no relatório), mas a NF-e
        # NÃO sai sozinha. O pedido #52 — pago com cartão de terceiro — ganhou
        # NF-e 55 autorizada na SEFAZ antes de qualquer conferência, e sobrou
        # cancelar nota de uma venda que não deveria existir. Agora o Lucas
        # confere e emite pelo painel do PDV (/api/nfe/emitir/<venda_id>),
        # no mesmo momento em que decide gerar a etiqueta.
        'emitir_nfe': False,
    }
    try:
        # Incrementa tentativas ANTES de tentar — assim mesmo se travar a
        # gente sabe quantas vezes foi tentado.
        db_execute("UPDATE pedidos SET pdv_tentativas=COALESCE(pdv_tentativas,0)+1, "
                   "pdv_ultima_tentativa=NOW() WHERE id=%s", [pid])
        r = requests.post(PDVPRO_URL + '/api/integracao/pedido',
                          json=pdv_payload,
                          headers={'X-API-Key': PDVPRO_API_KEY}, timeout=20)
        if r.status_code != 200:
            log.error("PDV /pedido %s status %s: %s", pid, r.status_code, r.text[:300])
            return False, None
        resp_pdv = r.json() or {}
        pdv_vid = resp_pdv.get('venda_id')
        nfe_ref = resp_pdv.get('nfe_ref')
        if pdv_vid:
            db_execute("UPDATE pedidos SET pdv_venda_id=%s WHERE id=%s",
                       [pdv_vid, pid])
            log.info("pedido %s → PDV venda %s (NF ref=%s)",
                     pid, pdv_vid, nfe_ref or 'n/a')
        if nfe_ref:
            db_execute("""UPDATE pedidos SET nfe_ref=%s, nfe_numero=%s,
                          nfe_modelo=%s WHERE id=%s""",
                       [nfe_ref, str(resp_pdv.get('nfe_numero') or ''),
                        str(resp_pdv.get('nfe_modelo') or ''), pid])
        if resp_pdv.get('nfe_erro'):
            log.warning("NF auto pedido %s: %s", pid, resp_pdv['nfe_erro'])
        return True, resp_pdv
    except Exception as e:
        log.error("falha ao enviar pedido %s pro PDV Pro: %s", pid, e)
        return False, None


# ─── Cron: reconcilia pedidos pago + sem pdv_venda_id ─────────────────────────
@app.route('/cron/reconciliar-pedidos-site')
def cron_reconciliar_pedidos_site():
    """Rede de segurança: se o webhook Asaas processa o pagamento mas o
    POST pro PDV Pro falha (Railway reinicia no meio, PDV fora, timeout...),
    o pedido fica 'pago' sem 'pdv_venda_id'. Esse cron pega esses casos a
    cada 5 min, re-tenta o envio. Após 3 falhas seguidas, manda WhatsApp
    pro admin pra investigar manualmente."""
    if not _cron_token_ok():
        return 'forbidden', 403
    pendentes = db_execute("""
        SELECT id, total, pdv_tentativas, pago_em
        FROM pedidos
        WHERE status='pago'
          AND pdv_venda_id IS NULL
          AND pago_em > NOW() - INTERVAL '7 days'
          AND COALESCE(pdv_tentativas, 0) < 10
        ORDER BY pago_em ASC
        LIMIT 20
    """, fetch='all') or []
    reenviados, falhas, alertas = 0, 0, []
    for ped in pendentes:
        ok, _ = _enviar_pedido_pro_pdv(ped['id'])
        if ok:
            reenviados += 1
        else:
            falhas += 1
            # Após 3 tentativas falhas, avisa admin uma vez
            tentativas = int(ped.get('pdv_tentativas') or 0) + 1  # +1 = a que acabou
            if tentativas == 3:
                alertas.append((ped['id'], float(ped['total'])))
    for pid, tot in alertas:
        try:
            enviar_whatsapp(ADMIN_WHATSAPP,
                f"⚠️ *Pedido #{pid} (R$ {tot:.2f}) não foi pro PDV Pro*\n\n"
                f"Cliente pagou mas o sistema não conseguiu enviar a venda "
                f"depois de 3 tentativas. O cron vai continuar tentando, mas "
                f"vale conferir manualmente em /admin/pedidos.".replace('.', ','))
        except Exception as e:
            log.warning(f"alerta WhatsApp pedido {pid}: {e}")
    return jsonify({
        'ok': True,
        'pendentes_encontrados': len(pendentes),
        'reenviados': reenviados,
        'falhas': falhas,
        'alertas_admin': len(alertas),
    })


# ─── Meta Conversions API ─────────────────────────────────────────────────────
# O fbq('track','Purchase') vive em pedido_pagamento.html e só roda se o cliente
# CARREGAR a pagina do pedido ja com status='pago'. No fluxo real ele paga fora
# do site (app do banco), o Asaas confirma aqui no servidor e o cliente nunca
# mais volta na aba — entao o evento nunca disparava. Resultado medido em
# 24/07/2026: 16 InitiateCheckout e ZERO Purchase em 7 dias.
# Aqui o proprio servidor manda o evento, sem depender do navegador.
META_PIXEL_ID = os.environ.get('META_PIXEL_ID', '1011945628185906')
META_CAPI_TOKEN = os.environ.get('META_CAPI_TOKEN', '')


def _capi_hash(valor):
    """SHA-256 do dado normalizado, como a Meta exige (minusculo, sem espaco)."""
    v = (valor or '').strip().lower()
    return hashlib.sha256(v.encode()).hexdigest() if v else None


def _capi_telefone(tel):
    """So digitos, com DDI 55 — senao a Meta nao casa o contato."""
    d = re.sub(r'\D', '', tel or '')
    if not d:
        return None
    if not d.startswith('55'):
        d = '55' + d
    return hashlib.sha256(d.encode()).hexdigest()


def enviar_purchase_capi(p):
    """Manda o Purchase pra Meta pelo servidor. Nunca levanta excecao:
    rastreamento quebrado nao pode derrubar processamento de pagamento."""
    if not META_CAPI_TOKEN:
        log.info("CAPI: META_CAPI_TOKEN vazio, pulando pedido %s", p.get('id'))
        return False
    try:
        # itens do pedido pro custom_data (uma query so; falha aqui nao impede
        # o evento — venda sem detalhe de item ainda vale mais que venda nenhuma)
        try:
            itens = db_execute("SELECT produto_pdv_id, quantidade, preco_unitario "
                               "FROM pedido_itens WHERE pedido_id=%s AND "
                               "produto_pdv_id IS NOT NULL", [p['id']], fetch='all') or []
        except Exception as e:
            log.error("CAPI itens pedido=%s: %s", p.get('id'), e)
            itens = []
        nome = (p.get('nome') or '').strip().split()
        user = {
            'em': [_capi_hash(p.get('email'))] if p.get('email') else None,
            'ph': [_capi_telefone(p.get('telefone'))] if p.get('telefone') else None,
            'fn': [_capi_hash(nome[0])] if nome else None,
            'ln': [_capi_hash(nome[-1])] if len(nome) > 1 else None,
            'country': [_capi_hash('br')],
            # identificador proprio: garante casamento mesmo em pedido sem
            # email/telefone, e liga varias compras do mesmo cliente.
            'external_id': [_capi_hash(str(p.get('cliente_id')))] if p.get('cliente_id') else None,
        }
        user = {k: v for k, v in user.items() if v and v[0]}
        evento = {
            'event_name': 'Purchase',
            'event_time': int(time.time()),
            'action_source': 'website',
            # rota real e /pedido/<id>/pagamento — a Meta cruza essa URL com o
            # que o pixel do navegador viu; apontar pra URL inexistente derruba
            # a qualidade do casamento do evento.
            'event_source_url': f"{SITE_URL.rstrip('/')}/pedido/{p['id']}/pagamento",
            # mesmo event_id do fbq do navegador: se os dois dispararem, a Meta
            # deduplica em vez de contar a venda duas vezes.
            'event_id': f"pedido-{p['id']}",
            'user_data': user,
            'custom_data': {
                'currency': 'BRL',
                'value': float(p.get('total') or 0),
                'order_id': str(p['id']),
                # os PRODUTOS vendidos: sem isso a Meta sabe que houve venda mas
                # nao QUAL item, e o anuncio de catalogo nao aprende nada.
                'content_type': 'product',
                'content_ids': [str(i['produto_pdv_id']) for i in itens],
                'contents': [{'id': str(i['produto_pdv_id']),
                              'quantity': int(float(i.get('quantidade') or 1)),
                              'item_price': float(i.get('preco_unitario') or 0)}
                             for i in itens],
            },
        }
        r = requests.post(
            f'https://graph.facebook.com/v22.0/{META_PIXEL_ID}/events',
            json={'data': [evento], 'access_token': META_CAPI_TOKEN}, timeout=10)
        ok = r.status_code == 200 and r.json().get('events_received', 0) > 0
        log.info("CAPI Purchase pedido=%s valor=%s -> %s %s",
                 p['id'], p.get('total'), r.status_code, r.text[:200])
        _capi_registrar(p['id'], f"HTTP {r.status_code} {r.text[:300]}")
        return ok
    except Exception as e:
        log.error("CAPI Purchase pedido=%s falhou: %s", p.get('id'), e)
        _capi_registrar(p.get('id'), f"EXCECAO {type(e).__name__}: {e}"[:300])
        return False


def _capi_registrar(pid, resposta):
    """Grava o resultado no proprio pedido. Em try/except: gravar diagnostico
    nunca pode derrubar o processamento do pagamento."""
    try:
        if pid:
            db_execute("UPDATE pedidos SET capi_em=NOW(), capi_resposta=%s "
                       "WHERE id=%s", [resposta, pid])
    except Exception as e:
        log.error("CAPI registrar pedido=%s: %s", pid, e)


def processar_pedido_pago(pid):
    """Tudo que precisa acontecer UMA vez quando um pedido vira pago.

    Existia só dentro do webhook do Asaas, e o checkout transparente nunca
    passava por aqui: ao receber CONFIRMED ele mesmo gravava
    `status='pago', pago_em=NOW()` e devolvia. Quando o webhook chegava logo
    depois, o guard `if p['pago_em']` via a marca já posta e saía em
    `ja_processado` — então, pra TODO pedido pago no cartão pela página da
    loja, não rodava nada disto: Purchase pra Meta, venda no PDV, etiqueta,
    e-mail e WhatsApp de confirmação, aviso pro admin, e a reavaliação de
    risco que segura a etiqueta.

    Medido em 27/07: só 2 de 19 pedidos pagos tinham disparado o Purchase —
    os dois pagos no PIX, onde o webhook chega primeiro. Com a loja rodando
    campanha, isso é o Meta otimizando às cegas.

    Quem chama é responsável por já ter gravado `pago_em` (é essa marca que
    garante execução única).
    """
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return
    # Final do cartao pela API. O checkout transparente ja grava a partir da
    # propria resposta, mas quem paga pela FATURA HOSPEDADA do Asaas nunca
    # passava por la — e a fatura hospedada e o caminho que mais aprova. Sem
    # buscar aqui, a regra de "mesmo cartao, outro CPF" ficava cega justamente
    # onde o dinheiro entra.
    if not p.get('cartao_final') and p.get('asaas_cobranca_id'):
        try:
            cob = asaas_buscar_cobranca(p['asaas_cobranca_id']) or {}
            cc = cob.get('creditCard') or {}
            if cc.get('creditCardNumber'):
                db_execute("UPDATE pedidos SET cartao_final=%s, cartao_bandeira=%s "
                           "WHERE id=%s",
                           [str(cc['creditCardNumber'])[:4],
                            (cc.get('creditCardBrand') or '')[:20], pid])
                p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid],
                               fetch='one') or p
        except Exception as e:
            log.warning("buscar cartao da cobranca %s: %s", pid, e)
    try:
        enviar_purchase_capi(p)
    except Exception as e:
        log.error("CAPI pedido %s: %s", pid, e)
    try:
        _enviar_pedido_pro_pdv(pid)
    except Exception as e:
        log.error("PDV pedido %s: %s", pid, e)

    # Antifraude: reavalia AGORA (o titular do cartao so existe depois do
    # pagamento) e, se o risco for alto, a etiqueta automatica NAO sai. Esse e
    # o unico passo irreversivel do fluxo.
    risco_sc = 0
    try:
        risco_sc, risco_mt = avaliar_risco_pedido(pid)
        if risco_sc >= RISCO_LIMITE and not p.get('risco_liberado_em'):
            alertar_risco(pid, risco_sc, risco_mt,
                          contexto=' — *PAGO*, retido antes de postar')
    except Exception as e:
        log.error("antifraude pedido pago %s: %s", pid, e)

    try:
        fsid = (p.get('melhorenvio_servico_id') or '').strip()
        etiq_ja = (p.get('melhorenvio_etiqueta_id') or '').strip()
        if risco_sc >= RISCO_LIMITE and not p.get('risco_liberado_em'):
            log.warning("pedido %s retido por antifraude (score=%s) — "
                        "etiqueta automatica nao gerada", pid, risco_sc)
        elif fsid and not etiq_ja and me_configurado() and me_token_atual():
            ok_et, res_et = _gerar_etiqueta_me(
                pid, fsid, servico_nome=(p.get('frete_servico') or '')[:80])
            if ok_et:
                log.info("etiqueta ME gerada auto pra pedido %s "
                         "(envio=%s rastreio=%s)", pid,
                         res_et.get('etiqueta_id'), res_et.get('rastreio'))
            else:
                try:
                    enviar_whatsapp(ADMIN_WHATSAPP,
                        f"⚠️ *Etiqueta ME falhou — pedido #{pid}*\n\n"
                        f"Motivo: {(res_et.get('erro') or 'erro')[:200]}\n\n"
                        f"Gera manual: /admin/pedidos")
                except Exception:
                    pass
                log.warning("etiqueta ME falhou pedido %s: %s", pid, res_et)
    except Exception as e:
        log.error("etiqueta auto pedido %s: %s", pid, e)

    try:
        enviar_email(p['email'],
                     f'Pedido #{pid} confirmado — Luqui Brinquedos',
                     f"""<p>Olá {p['nome'].split()[0]}! 💛</p>
<p>Seu pagamento foi <b>confirmado</b> e estamos preparando seu pedido com muito carinho.</p>
<p><b>Pedido:</b> #{pid}<br>
<b>Total pago:</b> R$ {p['total']}<br>
<b>Entrega em:</b> {p['endereco']}, {p['numero']} — {p['cidade']}/{p['uf']}</p>
<p>Te avisamos quando sair pra entrega! 🚚</p>
<p><a href="https://www.luquibrinquedos.com.br/pedido/{pid}/acessar?t={p.get('token','')}"
     style="background:#FFC700;color:#1652C7;padding:12px 24px;border-radius:8px;
            font-weight:900;text-decoration:none;display:inline-block">
  👤 Acessar meus pedidos
</a></p>
<p style="font-size:13px;color:#64748B">Esse link entra direto na sua conta, sem senha —
guarda ele só pra você. Lá você acompanha a entrega, baixa a nota fiscal e junta
pontos do Clube Luqui.</p>
<p>Dúvidas? <a href='https://wa.me/{cfg('whatsapp_loja', WHATSAPP_LOJA)}'>WhatsApp (45) 99111-9800</a></p>
<p>Abraço,<br>Luqui Brinquedos 🧸</p>""")
    except Exception as e:
        log.error("email confirma: %s", e)

    try:
        is_retira_p = (p.get('frete_servico') or '').lower().startswith('retirar')
        if is_retira_p:
            linha_entrega = "Retirada: na loja (R. Eng. Rebouças, 2053)"
            linha_fechamento = "Te aviso aqui quando estiver pronto pra retirar!"
        else:
            linha_entrega = f"Entrega: {p.get('cidade') or '—'}/{p.get('uf') or '—'}"
            linha_fechamento = "Te aviso quando sair pra entrega!"
        enviar_whatsapp(p['telefone'],
            f"💛 Oi {p['nome'].split()[0]}! Sou a Luqui Brinquedos.\n\n"
            f"Seu pagamento do *Pedido #{pid}* foi confirmado! 🎉\n"
            f"Total: *R$ {p['total']}*\n"
            f"{linha_entrega}\n\n"
            f"Já estamos preparando tudo com muito carinho 🧸\n"
            f"{linha_fechamento}")
    except Exception as e:
        log.error("WA cliente: %s", e)

    try:
        enviar_whatsapp(ADMIN_WHATSAPP,
            f"🛒 *Novo pedido pago #{pid}*\n\n"
            f"Cliente: {p['nome']}\n"
            f"Telefone: {p['telefone']}\n"
            f"Total: *R$ {p['total']}*\n"
            f"Forma: {p['forma_pagto']}\n"
            f"Endereço: {p['endereco']}, {p['numero']} - {p['cidade']}/{p['uf']}\n\n"
            f"Ver: https://www.luquibrinquedos.com.br/admin/pedidos")
    except Exception as e:
        log.error("WA admin: %s", e)


# ─── Webhook Asaas: confirma pagamento ────────────────────────────────────────
@app.route('/webhook/asaas', methods=['POST'])
def webhook_asaas():
    # Autenticação — FAIL-CLOSED. Antes era `if ASAAS_WEBHOOK_TOKEN:`, ou seja,
    # se a env sumisse (deploy novo, typo, serviço recriado) o endpoint passava
    # a aceitar QUALQUER POST. E este webhook marca pedido como pago, dispara
    # venda no PDV e gera etiqueta: um POST forjado com
    # {"event":"PAYMENT_CONFIRMED","payment":{"externalReference":"pedido-N"}}
    # despacharia mercadoria de graça. Sem token configurado, ninguém entra.
    if not ASAAS_WEBHOOK_TOKEN:
        log.error("webhook/asaas: ASAAS_WEBHOOK_TOKEN não configurado — recusando")
        return jsonify({'erro': 'webhook não configurado'}), 503
    recv = (request.headers.get('asaas-access-token')
            or request.headers.get('Asaas-Access-Token') or '').strip()
    if not secrets.compare_digest(recv, ASAAS_WEBHOOK_TOKEN):
        log.warning("webhook/asaas: token inválido")
        return jsonify({'erro': 'token inválido'}), 401
    d = request.get_json(silent=True) or {}
    event = d.get('event')
    payment = d.get('payment') or {}
    ref = payment.get('externalReference') or ''
    log.info("webhook/asaas: %s ref=%s", event, ref)
    # Assinatura do clube?
    if ref.startswith('clube-'):
        try:
            aid = int(ref.split('-')[1])
        except (IndexError, ValueError):
            return jsonify({'erro': 'ref inválida'}), 400
        ass = db_execute("""SELECT a.*, p.nome AS plano_nome, p.preco_mensal,
                                   c.nome AS cliente_nome, c.email AS cliente_email
                            FROM clube_assinaturas a
                            JOIN clube_planos p ON p.id=a.plano_id
                            JOIN clientes_site c ON c.id=a.cliente_id
                            WHERE a.id=%s""", [aid], fetch='one')
        if not ass:
            return jsonify({'erro': 'assinatura não encontrada'}), 404
        if event in ('PAYMENT_CONFIRMED', 'PAYMENT_RECEIVED'):
            db_execute("""UPDATE clube_assinaturas SET status='ativa',
                          ultimo_envio=NULL,
                          pago_em=COALESCE(pago_em, NOW()),
                          proximo_envio=CURRENT_DATE + INTERVAL '7 days'
                          WHERE id=%s""", [aid])
            # Email
            try:
                enviar_email(ass['cliente_email'],
                    f'🎁 Bem-vindo ao Clube Luqui — {ass["plano_nome"]}',
                    f"""<p>Olá {ass['cliente_nome'].split()[0]}! 💛</p>
<p>Seu pagamento foi confirmado e sua assinatura do
<b>{ass['plano_nome']}</b> está <b style='color:#16A34A'>ATIVA</b>! 🎉</p>
<p>Em até <b>7 dias úteis</b> sua primeira Caixa Misteriosa vai pra
expedição. Te avisamos com o código de rastreio assim que sair!</p>
<p>Valor mensal: <b>R$ {ass['preco_mensal']}</b><br>
Próxima cobrança: dia {(datetime.now(SP_TZ).date()+timedelta(days=30)).strftime('%d/%m/%Y')}</p>
<p>Bora brincar muito? 🧸<br>
Dúvidas? <a href='https://wa.me/{cfg('whatsapp_loja', WHATSAPP_LOJA)}'>WhatsApp (45) 99111-9800</a></p>
<p>Abraço,<br>Luqui Brinquedos</p>""")
            except Exception as e:
                log.error("email clube ativa: %s", e)
            # WhatsApp cliente
            cli_tel = db_execute("SELECT telefone FROM clientes_site WHERE id=%s",
                                 [ass['cliente_id']], fetch='one') or {}
            try:
                enviar_whatsapp(cli_tel.get('telefone'),
                    f"🎁 {ass['cliente_nome'].split()[0]}, sua assinatura do "
                    f"*Clube Luqui* está ativa!\n\n"
                    f"Plano: *{ass['plano_nome']}* (R$ {ass['preco_mensal']}/mês)\n\n"
                    f"Sua 1ª Caixa Misteriosa sai em até 7 dias úteis 📦\n"
                    f"Bora brincar! 🧸")
            except Exception as e:
                log.error("WA clube cliente: %s", e)
            # WhatsApp admin
            try:
                enviar_whatsapp(ADMIN_WHATSAPP,
                    f"🎁 *Novo assinante Clube Luqui!*\n\n"
                    f"Cliente: {ass['cliente_nome']}\n"
                    f"Plano: {ass['plano_nome']} (R$ {ass['preco_mensal']}/mês)\n\n"
                    f"Preparar caixa: https://www.luquibrinquedos.com.br/admin/clube/envio-mensal")
            except Exception as e:
                log.error("WA clube admin: %s", e)
        elif event in ('PAYMENT_OVERDUE',):
            db_execute("UPDATE clube_assinaturas SET status='atrasado' "
                       "WHERE id=%s", [aid])
        elif event in ('SUBSCRIPTION_DELETED', 'PAYMENT_DELETED'):
            db_execute("""UPDATE clube_assinaturas SET status='cancelada',
                          cancelada_em=NOW() WHERE id=%s""", [aid])
        return jsonify({'ok': True})

    if not ref.startswith('pedido-'):
        return jsonify({'ok': True, 'ignorado': 'sem ref de pedido/clube'})
    try:
        pid = int(ref.split('-')[1])
    except (IndexError, ValueError):
        return jsonify({'erro': 'ref inválida'}), 400
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'pedido não encontrado'}), 404
    if event in ('PAYMENT_CONFIRMED', 'PAYMENT_RECEIVED'):
        # Uma compra no CARTÃO gera DOIS eventos: PAYMENT_CONFIRMED na hora e
        # PAYMENT_RECEIVED quando a operadora repassa o dinheiro — ~30 dias
        # depois. Olhar só status=='pago' não segurava o segundo: até lá o
        # pedido já tinha andado pra 'enviado', o guard passava batido e o
        # pagamento era processado DE NOVO — outra venda no PDV, estoque
        # baixado outra vez e NF-e nova autorizada. (No PIX isso nunca
        # apareceu: lá os dois eventos chegam no mesmo segundo, com o pedido
        # ainda em 'pago'.)
        # pago_em é a marca de "essa transição já aconteceu" e não some quando
        # o pedido avança de status. Pedido atrasado que paga depois tem
        # pago_em nulo, então continua entrando normalmente.
        if p['pago_em'] or p['pdv_venda_id']:
            return jsonify({'ok': True, 'ja_processado': True})
        db_execute("""UPDATE pedidos SET status='pago', pago_em=NOW(),
                      atualizado_em=NOW() WHERE id=%s""", [pid])
        processar_pedido_pago(pid)
    elif event in ('PAYMENT_OVERDUE',):
        db_execute("UPDATE pedidos SET status='atrasado' WHERE id=%s", [pid])
    elif event in ('PAYMENT_DELETED', 'PAYMENT_REFUNDED'):
        db_execute("UPDATE pedidos SET status='cancelado' WHERE id=%s", [pid])
    return jsonify({'ok': True})


# ─── Bootstrap ────────────────────────────────────────────────────────────────
with app.app_context():
    try:
        init_db()
        log.info("LuquiShop banco pronto.")
    except Exception as e:
        log.error("init_db: %s", e)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5090)), debug=True)
