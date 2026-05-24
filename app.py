"""LuquiShop — Loja online + Clube Caixa Misteriosa da Luqui Brinquedos.

Stack Flask+PG. Produtos/estoque/promoções são puxados do PDV Pro em tempo real
via API (X-API-Key). Quando um pedido é pago, dispara webhook que cria a venda
no PDV Pro automaticamente.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import requests
from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('luquishop')
SP_TZ = ZoneInfo('America/Sao_Paulo')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or 'troque-em-prod-luqui-shop'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

DATABASE_URL = os.environ.get('DATABASE_URL') or ''
PDVPRO_URL = os.environ.get('PDVPRO_URL', 'https://pdvpro.luqsys.com.br')
PDVPRO_API_KEY = os.environ.get('PDVPRO_API_KEY', '')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'lucasfagundes91@hotmail.com')
ADMIN_SENHA_PADRAO = os.environ.get('ADMIN_SENHA', 'Lucasf123@')
WHATSAPP_LOJA = os.environ.get('WHATSAPP_LOJA', '5545991077788')
ASAAS_API_KEY = os.environ.get('ASAAS_API_KEY', '')
ASAAS_WEBHOOK_TOKEN = os.environ.get('ASAAS_WEBHOOK_TOKEN', '')
ASAAS_BASE = 'https://api.asaas.com/v3'
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
EMAIL_FROM = os.environ.get('EMAIL_FROM',
                            'Luqui Brinquedos <contato@luquibrinquedos.com.br>')
SITE_URL = os.environ.get('SITE_URL', 'https://www.luquibrinquedos.com.br')


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
        # Configurações gerais (key/value)
        """CREATE TABLE IF NOT EXISTS site_config (
            chave VARCHAR(60) PRIMARY KEY,
            valor TEXT
        )""",
        # Banners do hero
        """CREATE TABLE IF NOT EXISTS banners (
            id SERIAL PRIMARY KEY,
            titulo VARCHAR(160),
            imagem_url TEXT,
            link TEXT,
            ordem INT DEFAULT 0,
            ativo BOOLEAN DEFAULT TRUE
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

    # Configs default
    try:
        defaults = {
            'frete_gratis_cidades': 'Cascavel,Toledo',
            'frete_gratis_uf': 'PR',
            'desconto_pix_pct': '5',
            'parcelamento_max': '12',
            'whatsapp_loja': WHATSAPP_LOJA,
        }
        for k, v in defaults.items():
            db_execute("""INSERT INTO site_config (chave, valor) VALUES (%s,%s)
                          ON CONFLICT (chave) DO NOTHING""", [k, v])
    except Exception as e:
        log.error("seed config: %s", e)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def cfg(chave, default=''):
    r = db_execute("SELECT valor FROM site_config WHERE chave=%s",
                   [chave], fetch='one')
    return r['valor'] if r else default


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


@app.context_processor
def _ctx_globals():
    return {'ano': datetime.now(SP_TZ).year}


def cliente_logado():
    cid = session.get('cliente_id')
    if not cid:
        return None
    return db_execute("SELECT * FROM clientes_site WHERE id=%s",
                      [cid], fetch='one')


def admin_logado():
    return session.get('admin_id') is not None


def requer_admin(f):
    @wraps(f)
    def w(*a, **kw):
        if not admin_logado():
            return redirect(url_for('admin_login', next=request.path))
        return f(*a, **kw)
    return w


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
            timeout=8,
        )
        if r.status_code != 200:
            log.error("PDV Pro %s → %s", path, r.status_code)
            return None
        data = r.json()
        _PDV_CACHE[key] = {'t': now, 'data': data}
        return data
    except Exception as e:
        log.error("pdv_get %s: %s", path, e)
        if cached:
            return cached['data']  # serve stale em caso de erro
        return None


def listar_produtos(busca=None, categoria=None, limite=24, offset=0):
    p = {'limite': limite, 'offset': offset}
    if busca:
        p['busca'] = busca
    if categoria:
        p['categoria'] = categoria
    r = pdv_get('/api/integracao/produtos', p) or {}
    return r.get('produtos', []), r.get('total', 0)


def buscar_produto(produto_id):
    r = pdv_get(f'/api/integracao/produtos/{produto_id}') or {}
    return r.get('produto')


def listar_categorias():
    r = pdv_get('/api/integracao/categorias') or {}
    return r.get('categorias', [])


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
@app.route('/healthz')
def healthz():
    try:
        db_execute("SELECT 1", fetch='one')
        return 'ok', 200
    except Exception:
        return 'down', 500


@app.route('/')
def home():
    produtos, _ = listar_produtos(limite=12)
    categorias = listar_categorias()
    banners = db_execute(
        "SELECT * FROM banners WHERE ativo ORDER BY ordem", fetch='all') or []
    return render_template('home.html',
                           produtos=produtos,
                           categorias=categorias,
                           banners=banners,
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/categoria/<slug>')
def categoria(slug):
    pagina = max(1, int(request.args.get('p', 1)))
    por_pagina = 24
    produtos, total = listar_produtos(
        categoria=slug, limite=por_pagina, offset=(pagina - 1) * por_pagina)
    categorias = listar_categorias()
    cat_nome = next((c['nome'] for c in categorias if c['slug'] == slug), slug)
    return render_template('categoria.html',
                           produtos=produtos,
                           total=total,
                           pagina=pagina,
                           por_pagina=por_pagina,
                           categorias=categorias,
                           categoria_nome=cat_nome,
                           categoria_slug=slug,
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/buscar')
def buscar():
    q = (request.args.get('q') or '').strip()
    produtos, total = listar_produtos(busca=q, limite=48) if q else ([], 0)
    return render_template('busca.html',
                           produtos=produtos, total=total, termo=q,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/produto/<int:pid>')
def produto(pid):
    p = buscar_produto(pid)
    if not p:
        abort(404)
    return render_template('produto.html',
                           p=p,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


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
    preco = float(prod.get('preco_promo') or prod.get('preco_venda') or 0)
    itens = carrinho_ler()
    achei = next((i for i in itens if i['produto_id'] == pid), None)
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
                    'itens': itens})


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
    return render_template('checkout.html',
                           itens=itens, subtotal=sub,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=itens,
                           desconto_pix_pct=float(cfg('desconto_pix_pct', '5')),
                           parcelamento_max=int(cfg('parcelamento_max', '12')))


@app.route('/api/checkout/cep')
def checkout_cep():
    cep = (request.args.get('cep') or '').replace('-', '').replace('.', '')
    if len(cep) != 8 or not cep.isdigit():
        return jsonify({'erro': 'CEP inválido'}), 400
    try:
        r = requests.get(f'https://viacep.com.br/ws/{cep}/json/', timeout=6)
        d = r.json()
        if d.get('erro'):
            return jsonify({'erro': 'CEP não encontrado'}), 404
        return jsonify({'ok': True,
                        'endereco': d.get('logradouro'),
                        'bairro': d.get('bairro'),
                        'cidade': d.get('localidade'),
                        'uf': d.get('uf')})
    except Exception as e:
        return jsonify({'erro': str(e)}), 500


@app.route('/api/checkout/frete')
def checkout_frete():
    """Cálculo simples por enquanto: grátis em Cascavel/Toledo PR, R$ 24,90
    fixo no resto do PR e R$ 39,90 no resto do Brasil. Melhor Envio entra na
    fase 2 (token OAuth)."""
    cidade = (request.args.get('cidade') or '').strip().lower()
    uf = (request.args.get('uf') or '').upper()
    cidades_gratis = [c.strip().lower() for c in
                      cfg('frete_gratis_cidades', 'Cascavel,Toledo').split(',')]
    if uf == cfg('frete_gratis_uf', 'PR') and cidade in cidades_gratis:
        opcoes = [{'servico': 'Retirada/Entrega Luqui',
                   'valor': 0, 'prazo': '1-2 dias úteis'}]
    elif uf == 'PR':
        opcoes = [
            {'servico': 'PAC', 'valor': 24.90, 'prazo': '3-5 dias úteis'},
            {'servico': 'SEDEX', 'valor': 38.90, 'prazo': '2-3 dias úteis'},
        ]
    else:
        opcoes = [
            {'servico': 'PAC', 'valor': 39.90, 'prazo': '5-9 dias úteis'},
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
    plano = db_execute("SELECT * FROM clube_planos WHERE slug=%s AND ativo",
                       [slug], fetch='one')
    if not plano:
        return jsonify({'erro': 'Plano inválido'}), 404
    # Já tem assinatura?
    ja = db_execute("""SELECT id FROM clube_assinaturas
                       WHERE cliente_id=%s AND status='ativa'""",
                    [c['id']], fetch='one')
    if ja:
        return jsonify({'erro': 'Você já tem uma assinatura ativa. '
                                 'Cancele a atual antes de trocar.'}), 400
    if not c.get('cpf'):
        return jsonify({'erro': 'Preencha seu CPF antes de assinar'}), 400
    # Cria assinatura local (aguardando)
    nova = db_execute("""
        INSERT INTO clube_assinaturas
            (cliente_id, plano_id, status, proximo_envio)
        VALUES (%s,%s,'aguardando_pagto', CURRENT_DATE + INTERVAL '7 days')
        RETURNING id""",
        [c['id'], plano['id']], fetch='one')
    aid = nova['id']
    # Asaas: customer + subscription
    customer_id = asaas_criar_customer(c['nome'], c['email'],
                                       c['cpf'], c.get('telefone'))
    if not customer_id:
        db_execute("UPDATE clube_assinaturas SET status='erro_asaas' WHERE id=%s",
                   [aid])
        return jsonify({'erro': 'Falha ao criar cliente no gateway. '
                                 'Chama no WhatsApp pra ativar manualmente.'}), 502
    billing = {'pix': 'PIX', 'boleto': 'BOLETO',
               'cartao': 'CREDIT_CARD'}.get(forma, 'PIX')
    sub = asaas_criar_assinatura(
        customer_id, float(plano['preco_mensal']),
        descricao=f'Clube Luqui — {plano["nome"]}',
        billing_type=billing,
        externa_ref=f'clube-{aid}',
    )
    if not sub:
        db_execute("UPDATE clube_assinaturas SET status='erro_asaas' WHERE id=%s",
                   [aid])
        return jsonify({'erro': 'Falha ao criar assinatura no Asaas'}), 502
    sub_id = sub.get('id')
    db_execute("""UPDATE clube_assinaturas SET asaas_assinatura_id=%s
                  WHERE id=%s""", [sub_id, aid])
    # Pega a primeira cobrança (já é gerada pelo Asaas)
    try:
        r = requests.get(f'{ASAAS_BASE}/subscriptions/{sub_id}/payments',
                         headers=_asaas_headers(), timeout=10)
        if r.status_code == 200:
            payments = (r.json().get('data') or [])
            if payments:
                first = payments[0]
                url = first.get('invoiceUrl') or first.get('bankSlipUrl')
                return jsonify({'ok': True, 'assinatura_id': aid,
                                'pagamento_url': url})
    except Exception as e:
        log.error("buscar payments da subscription: %s", e)
    return jsonify({'ok': True, 'assinatura_id': aid,
                    'pagamento_url': '/minha-conta'})


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
@app.route('/login', methods=['GET', 'POST'])
def login():
    erro = None
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        senha = request.form.get('senha') or ''
        c = db_execute("SELECT * FROM clientes_site WHERE LOWER(email)=%s",
                       [email], fetch='one')
        if c and check_password_hash(c['senha_hash'], senha):
            session.permanent = True
            session['cliente_id'] = c['id']
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
             ('nome', 'email', 'senha', 'telefone', 'cpf')}
        if not d['nome'] or not d['email'] or len(d['senha']) < 6:
            erro = 'Preencha nome, e-mail e senha (mín 6 caracteres).'
        elif db_execute("SELECT 1 FROM clientes_site WHERE LOWER(email)=%s",
                        [d['email'].lower()], fetch='one'):
            erro = 'Esse e-mail já está cadastrado. Faça login.'
        else:
            nv = db_execute(
                """INSERT INTO clientes_site
                   (nome, email, senha_hash, telefone, cpf)
                   VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                [d['nome'], d['email'].lower(),
                 generate_password_hash(d['senha']),
                 d['telefone'] or None, d['cpf'] or None],
                fetch='one')
            session.permanent = True
            session['cliente_id'] = nv['id']
            return redirect(url_for('home'))
    return render_template('cadastrar.html', erro=erro,
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


@app.route('/sair')
def sair():
    session.pop('cliente_id', None)
    return redirect(url_for('home'))


@app.route('/minha-conta')
def minha_conta():
    c = cliente_logado()
    if not c:
        return redirect(url_for('login', next=request.path))
    pedidos = db_execute(
        "SELECT * FROM pedidos WHERE cliente_id=%s ORDER BY criado_em DESC",
        [c['id']], fetch='all') or []
    assinatura = db_execute(
        """SELECT a.*, p.nome AS plano_nome, p.preco_mensal
           FROM clube_assinaturas a JOIN clube_planos p ON p.id=a.plano_id
           WHERE a.cliente_id=%s AND a.status IN ('ativa','aguardando_pagto')
           ORDER BY a.id DESC LIMIT 1""",
        [c['id']], fetch='one')
    return render_template('minha_conta.html',
                           cliente=c, pedidos=pedidos, assinatura=assinatura,
                           categorias=listar_categorias(),
                           carrinho=carrinho_ler())


# ─── Admin (área restrita) ────────────────────────────────────────────────────
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    erro = None
    if request.method == 'POST':
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


@app.route('/admin')
@requer_admin
def admin_home():
    pedidos_recentes = db_execute(
        "SELECT * FROM pedidos ORDER BY criado_em DESC LIMIT 20",
        fetch='all') or []
    assinaturas_ativas = db_execute(
        """SELECT a.*, p.nome AS plano_nome, c.nome AS cliente_nome
           FROM clube_assinaturas a
           JOIN clube_planos p ON p.id=a.plano_id
           JOIN clientes_site c ON c.id=a.cliente_id
           WHERE a.status='ativa' ORDER BY a.proximo_envio""",
        fetch='all') or []
    stats = db_execute(
        """SELECT
              (SELECT COUNT(*) FROM pedidos) AS pedidos_total,
              (SELECT COUNT(*) FROM pedidos WHERE status='pago') AS pedidos_pagos,
              (SELECT COUNT(*) FROM clientes_site) AS clientes,
              (SELECT COUNT(*) FROM clube_assinaturas WHERE status='ativa') AS assinantes
        """, fetch='one') or {}
    return render_template('admin_home.html',
                           pedidos=pedidos_recentes,
                           assinaturas=assinaturas_ativas,
                           stats=stats)


@app.route('/admin/pedidos')
@requer_admin
def admin_pedidos():
    pedidos = db_execute(
        "SELECT * FROM pedidos ORDER BY criado_em DESC LIMIT 200",
        fetch='all') or []
    return render_template('admin_pedidos.html', pedidos=pedidos)


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
    body = {'name': nome, 'email': email, 'cpfCnpj': cpf_d, 'mobilePhone': telefone}
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
                                'subject': assunto, 'html': html},
                          timeout=15)
        if r.status_code in (200, 202):
            return True
        log.error("Resend %s: %s", r.status_code, r.text[:300])
    except Exception as e:
        log.error("Resend exc: %s", e)
    return False


# ─── Checkout: finalizar pedido ───────────────────────────────────────────────
@app.route('/api/checkout/finalizar', methods=['POST'])
def checkout_finalizar():
    d = request.get_json() or {}
    itens = carrinho_ler()
    if not itens:
        return jsonify({'erro': 'Carrinho vazio'}), 400
    # Validação básica
    obrig = ['nome', 'email', 'telefone', 'cpf', 'cep', 'endereco',
             'numero', 'bairro', 'cidade', 'uf', 'forma_pagto']
    for c in obrig:
        if not (d.get(c) or '').strip():
            return jsonify({'erro': f'Campo {c} obrigatório'}), 400
    if d['forma_pagto'] not in ('pix', 'cartao', 'boleto'):
        return jsonify({'erro': 'Forma de pagamento inválida'}), 400
    # Calcula totais
    subtotal = sum(float(it['preco']) * float(it['qtd']) for it in itens)
    frete = float(d.get('frete_valor') or 0)
    desconto_pct = float(cfg('desconto_pix_pct', '5'))
    desconto = 0.0
    if d['forma_pagto'] in ('pix', 'boleto'):
        desconto = round(subtotal * desconto_pct / 100, 2)
    total = round(subtotal + frete - desconto, 2)
    parcelas = max(1, min(int(cfg('parcelamento_max', '12')),
                          int(d.get('parcelas') or 1)))
    # Cria pedido no banco (status aguardando_pagto)
    cli = cliente_logado()
    ped = db_execute("""
        INSERT INTO pedidos
          (cliente_id, email, nome, telefone, cpf, cep, endereco, numero,
           complemento, bairro, cidade, uf, subtotal, frete, desconto, total,
           forma_pagto, parcelas, frete_servico, frete_prazo, observacao)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id""",
        [cli['id'] if cli else None,
         d['email'].strip().lower(), d['nome'].strip(), d['telefone'].strip(),
         d['cpf'].strip(), d['cep'].strip(), d['endereco'].strip(),
         d['numero'].strip(), d.get('complemento') or None,
         d['bairro'].strip(), d['cidade'].strip(), d['uf'].strip().upper(),
         subtotal, frete, desconto, total,
         d['forma_pagto'], parcelas,
         d.get('frete_servico') or 'A definir',
         d.get('frete_prazo') or '', d.get('observacao') or None],
        fetch='one')
    pid = ped['id']
    # Insere itens
    for it in itens:
        db_execute("""INSERT INTO pedido_itens
            (pedido_id, produto_pdv_id, codigo_barras, descricao,
             preco_unitario, quantidade, subtotal, foto_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            [pid, it['produto_id'], it.get('codigo_barras'),
             it['descricao'], it['preco'], it['qtd'],
             float(it['preco']) * float(it['qtd']),
             it.get('foto_url')])
    # Cria customer + cobrança no Asaas
    customer_id = asaas_criar_customer(d['nome'], d['email'],
                                       d['cpf'], d['telefone'])
    if not customer_id:
        db_execute("UPDATE pedidos SET status='erro_asaas' WHERE id=%s", [pid])
        return jsonify({'erro': 'Falha ao criar cliente Asaas. '
                                 'Tente novamente ou pedido pelo WhatsApp.'}), 502
    billing = {'pix': 'PIX', 'cartao': 'CREDIT_CARD',
               'boleto': 'BOLETO'}[d['forma_pagto']]
    cobranca = asaas_criar_cobranca(
        customer_id, total, billing,
        descricao=f'Luqui Brinquedos — Pedido #{pid}',
        parcelas=parcelas if d['forma_pagto'] == 'cartao' else 1,
        externa_ref=f'pedido-{pid}',
    )
    if not cobranca:
        db_execute("UPDATE pedidos SET status='erro_asaas' WHERE id=%s", [pid])
        return jsonify({'erro': 'Falha ao gerar cobrança. '
                                 'Tente novamente ou pedido pelo WhatsApp.'}), 502
    cob_id = cobranca.get('id')
    link = cobranca.get('invoiceUrl') or cobranca.get('bankSlipUrl')
    pix_qr = ''
    if d['forma_pagto'] == 'pix':
        pix = asaas_buscar_pix_qr(cob_id)
        pix_qr = pix.get('payload', '')
    db_execute("""UPDATE pedidos SET asaas_cobranca_id=%s, asaas_link=%s,
                  asaas_pix_qrcode=%s, asaas_boleto_url=%s
                  WHERE id=%s""",
               [cob_id, link, pix_qr,
                cobranca.get('bankSlipUrl') if d['forma_pagto'] == 'boleto' else None,
                pid])
    # Limpa carrinho e devolve URL de pagamento
    session['carrinho'] = []
    session.modified = True
    return jsonify({'ok': True, 'pedido_id': pid,
                    'pagamento_url': f'/pedido/{pid}/pagamento'})


@app.route('/pedido/<int:pid>/pagamento')
def pedido_pagamento(pid):
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
        abort(404)
    itens = db_execute(
        "SELECT * FROM pedido_itens WHERE pedido_id=%s ORDER BY id",
        [pid], fetch='all') or []
    return render_template('pedido_pagamento.html',
                           p=p, itens=itens,
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/api/pedido/<int:pid>/status')
def pedido_status(pid):
    p = db_execute("SELECT id, status, pago_em FROM pedidos WHERE id=%s",
                   [pid], fetch='one')
    if not p:
        return jsonify({'erro': 'Pedido não encontrado'}), 404
    return jsonify({'status': p['status'],
                    'pago_em': p['pago_em'].isoformat() if p['pago_em'] else None})


# ─── Webhook Asaas: confirma pagamento ────────────────────────────────────────
@app.route('/webhook/asaas', methods=['POST'])
def webhook_asaas():
    # Autenticação
    if ASAAS_WEBHOOK_TOKEN:
        recv = (request.headers.get('asaas-access-token')
                or request.headers.get('Asaas-Access-Token') or '').strip()
        if recv != ASAAS_WEBHOOK_TOKEN:
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
Dúvidas? <a href='https://wa.me/{cfg('whatsapp_loja', WHATSAPP_LOJA)}'>WhatsApp (45) 99107-7788</a></p>
<p>Abraço,<br>Luqui Brinquedos</p>""")
            except Exception as e:
                log.error("email clube ativa: %s", e)
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
        if p['status'] == 'pago':
            return jsonify({'ok': True, 'ja_pago': True})
        db_execute("""UPDATE pedidos SET status='pago', pago_em=NOW(),
                      atualizado_em=NOW() WHERE id=%s""", [pid])
        # Dispara venda no PDV Pro
        try:
            itens = db_execute(
                "SELECT * FROM pedido_itens WHERE pedido_id=%s",
                [pid], fetch='all') or []
            pdv_payload = {
                'pedido_id': pid,
                'cliente': {'nome': p['nome'], 'email': p['email'],
                            'cpf': p['cpf'], 'telefone': p['telefone']},
                'itens': [{'produto_id': i['produto_pdv_id'],
                           'descricao': i['descricao'],
                           'preco_unitario': float(i['preco_unitario']),
                           'quantidade': float(i['quantidade']),
                           'subtotal': float(i['subtotal'])} for i in itens],
                'total': float(p['total']),
                'desconto': float(p['desconto']),
                'frete': float(p['frete']),
                'forma_pagto': p['forma_pagto'],
            }
            r = requests.post(
                PDVPRO_URL + '/api/integracao/pedido',
                json=pdv_payload,
                headers={'X-API-Key': PDVPRO_API_KEY},
                timeout=20)
            if r.status_code == 200:
                pdv_vid = (r.json() or {}).get('venda_id')
                if pdv_vid:
                    db_execute(
                        "UPDATE pedidos SET pdv_venda_id=%s WHERE id=%s",
                        [pdv_vid, pid])
                    log.info("pedido %s → PDV venda %s", pid, pdv_vid)
            else:
                log.error("PDV /pedido %s: %s", r.status_code, r.text[:300])
        except Exception as e:
            log.error("falha ao enviar pedido pro PDV Pro: %s", e)
        # Email de confirmação
        try:
            enviar_email(p['email'],
                         f'Pedido #{pid} confirmado — Luqui Brinquedos',
                         f"""<p>Olá {p['nome'].split()[0]}! 💛</p>
<p>Seu pagamento foi <b>confirmado</b> e estamos preparando seu pedido com muito carinho.</p>
<p><b>Pedido:</b> #{pid}<br>
<b>Total pago:</b> R$ {p['total']}<br>
<b>Entrega em:</b> {p['endereco']}, {p['numero']} — {p['cidade']}/{p['uf']}</p>
<p>Te avisamos quando sair pra entrega! 🚚</p>
<p>Dúvidas? <a href='https://wa.me/{cfg('whatsapp_loja', WHATSAPP_LOJA)}'>WhatsApp (45) 99107-7788</a></p>
<p>Abraço,<br>Luqui Brinquedos 🧸</p>""")
        except Exception as e:
            log.error("email confirma: %s", e)
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
