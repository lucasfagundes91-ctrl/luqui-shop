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
WHATSAPP_LOJA = os.environ.get('WHATSAPP_LOJA', '5545991119800')
ASAAS_API_KEY = os.environ.get('ASAAS_API_KEY', '')
ASAAS_WEBHOOK_TOKEN = os.environ.get('ASAAS_WEBHOOK_TOKEN', '')
ASAAS_BASE = 'https://api.asaas.com/v3'
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
EMAIL_FROM = os.environ.get('EMAIL_FROM',
                            'Luqui Brinquedos <contato@luquibrinquedos.com.br>')
SITE_URL = os.environ.get('SITE_URL', 'https://www.luquibrinquedos.com.br')
ZAPI_INSTANCE = os.environ.get('ZAPI_INSTANCE', '')
ZAPI_TOKEN = os.environ.get('ZAPI_TOKEN', '')
ZAPI_CLIENT_TOKEN = os.environ.get('ZAPI_CLIENT_TOKEN', '')
ADMIN_WHATSAPP = os.environ.get('ADMIN_WHATSAPP', '5545991119800')
META_PIXEL_ID = os.environ.get('META_PIXEL_ID', '')
GOOGLE_TAG_ID = os.environ.get('GOOGLE_TAG_ID', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


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
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cupom_codigo VARCHAR(40)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cupom_desconto NUMERIC(10,2) DEFAULT 0",
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
        # Wishlist / favoritos
        """CREATE TABLE IF NOT EXISTS wishlist (
            id SERIAL PRIMARY KEY,
            cliente_id INT REFERENCES clientes_site(id) ON DELETE CASCADE,
            produto_pdv_id INT NOT NULL,
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(cliente_id, produto_pdv_id)
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
    return {'ano': datetime.now(SP_TZ).year,
            'META_PIXEL_ID': META_PIXEL_ID,
            'GOOGLE_TAG_ID': GOOGLE_TAG_ID}


@app.route('/api/checkout/cupom')
def checkout_aplicar_cupom():
    codigo = (request.args.get('codigo') or '').strip().upper()
    subtotal = float(request.args.get('subtotal') or 0)
    if not codigo:
        return jsonify({'erro': 'Digite o código'}), 400
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
<h3>Frete grátis</h3>
<p>Entrega <b>GRÁTIS</b> para Cascavel e Toledo (PR)! Prazo: 1 a 2 dias úteis.</p>

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
  <li>Parcelamento em até <b>12x sem juros</b></li>
  <li>Pagamento processado com segurança via <b>Asaas</b></li>
  <li>Aprovação imediata na maioria dos casos</li>
</ul>

<h3>📱 PIX</h3>
<p>Forma mais rápida e com <b>5% de desconto</b>!</p>
<ul>
  <li>Desconto aplicado automaticamente no checkout</li>
  <li>Confirmação em segundos</li>
  <li>Pedido entra em separação na hora</li>
</ul>

<h3>📄 Boleto bancário</h3>
<p>Também tem <b>5% de desconto</b>:</p>
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


def _render_pagina_legal(slug):
    p = PAGINAS_LEGAIS.get(slug)
    if not p:
        abort(404)
    return render_template('pagina.html',
                           titulo=p['titulo'], conteudo=p['conteudo'],
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/sobre')
def pag_sobre():
    return render_template('sobre.html',
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


@app.route('/api/produto/<int:pid>/avaliacao', methods=['POST'])
def avaliacao_criar(pid):
    c = cliente_logado()
    d = request.get_json() or {}
    estrelas = max(1, min(5, int(d.get('estrelas') or 0)))
    titulo = (d.get('titulo') or '')[:120]
    comentario = (d.get('comentario') or '')[:2000]
    if not comentario.strip():
        return jsonify({'erro': 'Escreve algo no comentário'}), 400
    # Auto-aprova se o cliente já comprou esse produto
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
        (produto_pdv_id, cliente_id, pedido_id, estrelas, titulo, comentario, aprovado)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        [pid, c['id'] if c else None, pedido_id, estrelas,
         titulo or None, comentario, aprovado])
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


@app.route('/cron/email-pos-compra')
def cron_email_pos_compra():
    """Roda diário: pedidos pagos há ~7 dias e ainda sem email de avaliação."""
    if request.args.get('token') != os.environ.get('CRON_TOKEN', 'troque'):
        return 'unauthorized', 401
    candidatos = db_execute("""
        SELECT * FROM pedidos
         WHERE status IN ('pago','enviado','entregue')
           AND pago_em IS NOT NULL
           AND pago_em < NOW() - INTERVAL '7 days'
           AND pago_em > NOW() - INTERVAL '14 days'
           AND COALESCE(observacao,'') NOT LIKE '%[avaliacao-enviada]%'
        LIMIT 50""", fetch='all') or []
    enviados = 0
    for p in candidatos:
        try:
            enviar_email(p['email'],
                f'Como foi seu pedido #{p["id"]}? 💛',
                f"""<p>Oi {p['nome'].split()[0]}! Tudo bem?</p>
<p>Faz uma semana que seu pedido <b>#{p['id']}</b> foi confirmado.
Esperamos que tudo tenha chegado certinho! 🧸</p>
<p>Que tal contar pra gente o que você achou? Sua avaliação ajuda outras famílias
a escolherem com confiança!</p>
<p><a href="https://www.luquibrinquedos.com.br/pedido/{p['id']}/tracking"
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


LUQUIZINHA_SYSTEM = """Voce eh a Luquizinha, atendente virtual da Luqui Brinquedos
(loja em Cascavel/PR). Atende no site/WhatsApp com tom carinhoso e direto.

CONHECIMENTO DA LOJA:
- Endereco: R. Engenheiro Reboucas, 2053 - Centro - Cascavel/PR
- Horario: Seg-Sex 9h-18h, Sab 9h-13h, Dom fechado
- WhatsApp humano: (45) 99111-9800
- Frete GRATIS em Cascavel e Toledo/PR
- Resto do Brasil: PAC R$ 39,90 (5-9 dias) ou SEDEX R$ 62,90 (2-4 dias)
- Resto do PR: PAC R$ 24,90 ou SEDEX R$ 38,90
- Pagamento: cartao ate 12x sem juros; PIX e boleto com 5% desconto
- Trocas: 7 dias direito de arrependimento (CDC) + 30 dias defeito
- Clube Caixa Misteriosa: Smart R$ 79,99/mes, Essencial R$ 129,99, Premium R$ 199,99
  Cobranca mensal automatica, sem fidelidade.

TOM:
- Curto (1-3 linhas), 1-2 emojis por mensagem (💛 🧸 🎁), sem markdown pesado
- "vc", "ta", "ne" se cliente puxar
- Acolhedor: "ahh que fofo!", "amei!", "vai amar de mais!"
- NAO inventa preco de produto especifico nem promete prazo certo -
  manda pro WhatsApp humano: "Pra confirmar isso da uma chamada
  no WhatsApp (45) 99111-9800 que a gente te ajuda direitinho 💛"
- Se cliente quer comprar especifico: indica a busca do site ou WhatsApp
- Se cliente pergunta produto que voce nao sabe: nao inventa, manda pro humano

NUNCA fale sobre Anthropic/Claude/IA. Voce eh a Luquizinha."""


@app.route('/api/luquizinha', methods=['POST'])
def luquizinha_chat():
    if not ANTHROPIC_API_KEY:
        return jsonify({'erro': 'Chat indisponível. Chame no WhatsApp (45) 99111-9800 💛'}), 503
    d = request.get_json() or {}
    historico = d.get('historico') or []  # [{role, content}]
    nova = (d.get('mensagem') or '').strip()
    if not nova:
        return jsonify({'erro': 'Mensagem vazia'}), 400
    # Adiciona a mensagem nova no histórico que vamos mandar
    msgs = historico[-10:]  # últimas 10 trocas
    msgs.append({'role': 'user', 'content': nova[:2000]})
    try:
        r = requests.post('https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': 'claude-haiku-4-5-20251001',
                'max_tokens': 400,
                'system': LUQUIZINHA_SYSTEM,
                'messages': msgs,
            }, timeout=20)
        if r.status_code != 200:
            log.error("Claude API %s: %s", r.status_code, r.text[:300])
            return jsonify({'erro': 'Tô meio sobrecarregada agora 😅 '
                                    'Chama no WhatsApp (45) 99111-9800!'}), 502
        data = r.json()
        resposta = ''.join(b.get('text', '') for b in data.get('content', []))
        return jsonify({'resposta': resposta or 'Desculpa, não entendi! Pode repetir? 💛'})
    except Exception as e:
        log.error("luquizinha exc: %s", e)
        return jsonify({'erro': 'Tive um probleminha técnico. WhatsApp: (45) 99111-9800'}), 500


@app.route('/api/newsletter', methods=['POST'])
def newsletter_signup():
    email = ((request.get_json() or {}).get('email') or '').strip().lower()
    nome = ((request.get_json() or {}).get('nome') or '').strip()
    if '@' not in email or '.' not in email:
        return jsonify({'erro': 'E-mail inválido'}), 400
    db_execute("""INSERT INTO newsletter (email, nome) VALUES (%s, %s)
                  ON CONFLICT (email) DO UPDATE SET ativo=true,
                  nome=COALESCE(EXCLUDED.nome, newsletter.nome)""",
               [email, nome or None])
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
    txt = """User-agent: *
Allow: /
Disallow: /admin
Disallow: /api/
Disallow: /pedido/

Sitemap: https://www.luquibrinquedos.com.br/sitemap.xml
"""
    from flask import Response
    return Response(txt, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    """Sitemap dinâmico: estáticas + categorias visíveis + produtos da vitrine."""
    from flask import Response
    base = 'https://www.luquibrinquedos.com.br'
    urls = [
        (base + '/', '1.0', 'daily'),
        (base + '/clube', '0.9', 'weekly'),
        (base + '/sobre', '0.7', 'monthly'),
        (base + '/trocas-devolucoes', '0.5', 'yearly'),
        (base + '/entregas', '0.5', 'yearly'),
        (base + '/formas-pagamento', '0.5', 'yearly'),
        (base + '/privacidade', '0.4', 'yearly'),
        (base + '/termos', '0.4', 'yearly'),
    ]
    for c in (listar_categorias() or []):
        urls.append((f"{base}/categoria/{c['slug']}", '0.8', 'weekly'))
    # Produtos da vitrine (até 1000 pra não estourar)
    rs = pdv_get('/api/integracao/produtos', {'limite': 100, 'offset': 0})
    if rs and rs.get('produtos'):
        for p in rs['produtos']:
            urls.append((f"{base}/produto/{p['id']}", '0.6', 'weekly'))
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for u, prio, freq in urls:
        xml += f'  <url><loc>{u}</loc><priority>{prio}</priority><changefreq>{freq}</changefreq></url>\n'
    xml += '</urlset>'
    return Response(xml, mimetype='application/xml')


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
    return render_template('produto.html',
                           p=p, avaliacoes=avals, media_estrelas=media,
                           relacionados=relacionados[:4],
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
    return render_template('minha_conta.html',
                           cliente=c, pedidos=pedidos, assinatura=assinatura,
                           envios=envios, planos=planos,
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


STATUS_TIMELINE = ['aguardando_pagto', 'pago', 'preparando', 'enviado', 'entregue']
STATUS_LABELS = {
    'aguardando_pagto': 'Aguardando pagamento',
    'pago':             'Pagamento confirmado',
    'preparando':       'Preparando seu pedido',
    'enviado':          'Saiu pra entrega',
    'entregue':         'Entregue ✓',
    'cancelado':        'Cancelado',
    'atrasado':         'Pagamento atrasado',
}


@app.route('/pedido/<int:pid>/tracking')
def pedido_tracking(pid):
    p = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not p:
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
    # Notifica cliente
    try:
        msgs = {
            'preparando': (f"📦 Oi {p['nome'].split()[0]}! Seu *Pedido #{pid}* está sendo "
                           f"preparado com muito carinho 💛"),
            'enviado': (f"🚚 Oi {p['nome'].split()[0]}! Seu *Pedido #{pid}* "
                        f"acabou de sair pra entrega!"
                        + (f"\n\n*Rastreio:* {rastreio}" if rastreio else "")
                        + f"\n\nAcompanhe: https://www.luquibrinquedos.com.br/pedido/{pid}/tracking"),
            'entregue': (f"💛 *Pedido #{pid} entregue!* Esperamos que ame!\n\n"
                         f"Que tal nos avaliar? "
                         f"https://www.luquibrinquedos.com.br/pedido/{pid}/tracking"),
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


@app.route('/admin/banners', methods=['GET', 'POST'])
@requer_admin
def admin_banners():
    if request.method == 'POST':
        bid = request.form.get('id')
        d = {k: (request.form.get(k) or '').strip() for k in
             ('titulo', 'subtitulo', 'imagem_url', 'link', 'cta_texto', 'cor_fundo')}
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
    # Cupom
    cupom_codigo = (d.get('cupom_codigo') or '').strip().upper()
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
    total = max(0, round(subtotal + frete - desconto - cupom_desconto, 2))
    parcelas = max(1, min(int(cfg('parcelamento_max', '12')),
                          int(d.get('parcelas') or 1)))
    # Cria pedido no banco (status aguardando_pagto)
    cli = cliente_logado()
    ped = db_execute("""
        INSERT INTO pedidos
          (cliente_id, email, nome, telefone, cpf, cep, endereco, numero,
           complemento, bairro, cidade, uf, subtotal, frete, desconto, total,
           forma_pagto, parcelas, frete_servico, frete_prazo, observacao,
           cupom_codigo, cupom_desconto)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id""",
        [cli['id'] if cli else None,
         d['email'].strip().lower(), d['nome'].strip(), d['telefone'].strip(),
         d['cpf'].strip(), d['cep'].strip(), d['endereco'].strip(),
         d['numero'].strip(), d.get('complemento') or None,
         d['bairro'].strip(), d['cidade'].strip(), d['uf'].strip().upper(),
         subtotal, frete, desconto + cupom_desconto, total,
         d['forma_pagto'], parcelas,
         d.get('frete_servico') or 'A definir',
         d.get('frete_prazo') or '', d.get('observacao') or None,
         cupom_codigo or None, cupom_desconto],
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
                'endereco': {
                    'cep': p['cep'], 'endereco': p['endereco'],
                    'numero': p['numero'], 'complemento': p.get('complemento'),
                    'bairro': p['bairro'], 'cidade': p['cidade'],
                    'uf': p['uf']
                },
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
                resp_pdv = r.json() or {}
                pdv_vid = resp_pdv.get('venda_id')
                nfe_ref = resp_pdv.get('nfe_ref')
                if pdv_vid:
                    db_execute(
                        "UPDATE pedidos SET pdv_venda_id=%s WHERE id=%s",
                        [pdv_vid, pid])
                    log.info("pedido %s → PDV venda %s (NF ref=%s)",
                             pid, pdv_vid, nfe_ref or 'n/a')
                if nfe_ref:
                    db_execute(
                        "UPDATE pedidos SET observacao = COALESCE(observacao,'') "
                        "|| %s WHERE id=%s",
                        [f' [NF {resp_pdv.get("nfe_modelo")}/{resp_pdv.get("nfe_numero")}]',
                         pid])
                if resp_pdv.get('nfe_erro'):
                    log.error("NF auto: %s", resp_pdv['nfe_erro'])
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
<p>Dúvidas? <a href='https://wa.me/{cfg('whatsapp_loja', WHATSAPP_LOJA)}'>WhatsApp (45) 99111-9800</a></p>
<p>Abraço,<br>Luqui Brinquedos 🧸</p>""")
        except Exception as e:
            log.error("email confirma: %s", e)
        # WhatsApp pro CLIENTE
        try:
            enviar_whatsapp(p['telefone'],
                f"💛 Oi {p['nome'].split()[0]}! Sou a Luqui Brinquedos.\n\n"
                f"Seu pagamento do *Pedido #{pid}* foi confirmado! 🎉\n"
                f"Total: *R$ {p['total']}*\n"
                f"Entrega: {p['cidade']}/{p['uf']}\n\n"
                f"Já estamos preparando tudo com muito carinho 🧸\n"
                f"Te aviso quando sair pra entrega!")
        except Exception as e:
            log.error("WA cliente: %s", e)
        # WhatsApp pro ADMIN (você)
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
