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
    return render_template('clube_assinar.html',
                           plano=plano,
                           categorias=listar_categorias(),
                           cliente=c,
                           carrinho=carrinho_ler())


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


# ─── Bootstrap ────────────────────────────────────────────────────────────────
with app.app_context():
    try:
        init_db()
        log.info("LuquiShop banco pronto.")
    except Exception as e:
        log.error("init_db: %s", e)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5090)), debug=True)
