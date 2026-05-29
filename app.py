"""LuquiShop — Loja online + Clube Caixa Misteriosa da Luqui Brinquedos.

Stack Flask+PG. Produtos/estoque/promoções são puxados do PDV Pro em tempo real
via API (X-API-Key). Quando um pedido é pago, dispara webhook que cria a venda
no PDV Pro automaticamente.
"""
import json
import logging
import os
import secrets
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
        # OAuth Google: cadastro sem senha (sub = unique id do Google)
        "ALTER TABLE clientes_site ALTER COLUMN senha_hash DROP NOT NULL",
        "ALTER TABLE clientes_site ADD COLUMN IF NOT EXISTS google_sub VARCHAR(40)",
        "ALTER TABLE clientes_site ADD COLUMN IF NOT EXISTS foto_url TEXT",
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
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS nfe_ref VARCHAR(80)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS nfe_numero VARCHAR(20)",
        "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS nfe_modelo VARCHAR(5)",
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
            'desconto_boleto_pct': '5',
            'parcelamento_max': '12',
            'parcelas_sem_juros_max': '1',  # so 1x sem juros; 2x+ ja tem juros
            'parcela_minima': '50',  # legado (nao usado mais no calculo)
            'juros_parcelamento_am': '2.49',  # % ao mes, acima do limite sem juros
            'whatsapp_loja': WHATSAPP_LOJA,
            # Melhor Envio — preencher em /admin/melhorenvio
            'me_cep_origem': '85801080',  # Luqui Brinquedos Cascavel
            'me_remetente_nome': 'Luqui Brinquedos',
            'me_remetente_cnpj': '',
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
            'loja_horario_funcionamento': 'Seg a Sex: 8h às 18h · Sáb: 9h às 13h',
            'loja_tempo_separacao_min': '30',
        }
        for k, v in defaults.items():
            db_execute("""INSERT INTO site_config (chave, valor) VALUES (%s,%s)
                          ON CONFLICT (chave) DO NOTHING""", [k, v])
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
        # Atualiza o subtitulo do banner "PAGUE NO PIX" se ainda estiver
        # no texto antigo de 10%
        db_execute(
            "UPDATE banners SET subtitulo = REPLACE(subtitulo, '10% de desconto', '3% de desconto') "
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
    'shipping-companies')


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


def me_remetente_dict():
    """Monta o payload `from` esperado pelo Melhor Envio nos endpoints
    de carrinho (precisa de dados completos do remetente)."""
    cnpj = ''.join(c for c in cfg('me_remetente_cnpj', '') if c.isdigit())
    return {
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
    """Retorna lista de opções de frete (servico, valor, prazo, id)."""
    cep_destino = ''.join(c for c in (cep_destino or '') if c.isdigit())
    if len(cep_destino) != 8:
        return []
    body = {
        'from':     {'postal_code': ''.join(c for c in cfg('me_cep_origem','')
                                            if c.isdigit())},
        'to':       {'postal_code': cep_destino},
        'products': me_volume_dos_itens(itens),
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
    return out


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


def _admin_ou_api_key():
    """Permite admin logado OU X-API-Key do PDV Pro."""
    from flask import session
    if session.get('admin'):
        return True
    return _verifica_api_key_pdv()


@app.route('/api/admin/pedidos/<int:pid>/etiqueta', methods=['POST'])
def admin_pedido_gerar_etiqueta(pid):
    """Fluxo completo Melhor Envio: cart → checkout → generate → print.
    Aceita admin logado OU X-API-Key do PDV Pro (integracao reversa)."""
    if not _admin_ou_api_key():
        return jsonify({'erro': 'unauthorized'}), 401
    d = request.get_json() or {}
    service_id = d.get('service_id')
    if not service_id:
        return jsonify({'erro': 'service_id obrigatório'}), 400
    ped = db_execute("SELECT * FROM pedidos WHERE id=%s", [pid], fetch='one')
    if not ped:
        return jsonify({'erro': 'pedido não encontrado'}), 404
    if ped.get('melhorenvio_etiqueta_id'):
        return jsonify({'erro': 'pedido já tem etiqueta gerada'}), 400
    itens = db_execute(
        "SELECT * FROM pedido_itens WHERE pedido_id=%s", [pid], fetch='all') or []
    if not itens:
        return jsonify({'erro': 'pedido sem itens'}), 400
    for it in itens:
        try:
            it['produto'] = buscar_produto(it['produto_pdv_id']) or {}
        except Exception:
            it['produto'] = {}
        it['qtd'] = it['quantidade']
        it['preco'] = it['preco_unitario']
    vol = me_volume_dos_itens(itens)
    vol_resumo = [{'height': v['height'], 'width': v['width'],
                   'length': v['length'], 'weight': v['weight']} for v in vol]
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
            'address':     ped.get('logradouro') or '',
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
            'insurance_value': float(ped.get('subtotal') or 0),
            'receipt': False, 'own_hand': False,
            'reverse': False, 'non_commercial': False,
        },
    }
    try:
        r = me_request('POST', '/api/v2/me/cart', json_body=body)
        if not r.ok:
            return jsonify({'erro': 'cart falhou', 'detalhe': r.text[:500]}), 502
        cart = r.json()
        order_id = cart.get('id')
        if not order_id:
            return jsonify({'erro': 'sem id do envio', 'detalhe': cart}), 502
        r2 = me_request('POST', '/api/v2/me/shipment/checkout',
                        json_body={'orders': [order_id]})
        if not r2.ok:
            return jsonify({'erro': 'checkout falhou — confira saldo',
                            'detalhe': r2.text[:500]}), 502
        r3 = me_request('POST', '/api/v2/me/shipment/generate',
                        json_body={'orders': [order_id]})
        if not r3.ok:
            return jsonify({'erro': 'generate falhou',
                            'detalhe': r3.text[:500]}), 502
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
                        melhorenvio_pago_em      = NOW(),
                        atualizado_em            = NOW()
                       WHERE id=%s""",
                   [order_id, url_pdf, rastreio, str(service_id),
                    (d.get('servico_nome') or '')[:80], pid])
    except Exception as e:
        log.exception("ME etiqueta")
        return jsonify({'erro': str(e)}), 500
    return jsonify({'ok': True, 'etiqueta_id': order_id,
                    'rastreio': rastreio, 'pdf_url': url_pdf})


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


_FILTROS_VALIDOS = ('departamento', 'grupo', 'subgrupo', 'marca', 'faixa_etaria', 'destaque')


def listar_produtos(busca=None, categoria=None, limite=24, offset=0, **filtros):
    """`categoria` ainda é aceito como alias de `departamento` pra compat.
    Filtros extras (departamento/grupo/subgrupo/marca/faixa_etaria) podem ser
    string única ou lista — viram CSV pro PDV."""
    p = {'limite': limite, 'offset': offset}
    if busca:
        p['busca'] = busca
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
               embrulho_mensagem
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
        'fechar a compra, escolha quantos pontos usar — até 50% do valor do pedido.</p>'
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


@app.route('/cron/aniversariantes')
def cron_aniversariantes():
    """Gera cupom personalizado pros aniversariantes do dia + WA + email."""
    if request.args.get('token') != os.environ.get('CRON_TOKEN', 'troque'):
        return 'unauthorized', 401
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
    c = cliente_logado()
    if not c or c.get('ganhou_primeira'):
        return jsonify({'pode': False})
    return jsonify({'pode': True, 'codigo': 'PRIMEIRO10', 'desconto_pct': 10})


@app.route('/cron/carrinho-abandonado')
def cron_carrinho_abandonado():
    """Pedidos aguardando_pagto há 24-48h: dispara WhatsApp/email lembrando."""
    if request.args.get('token') != os.environ.get('CRON_TOKEN', 'troque'):
        return 'unauthorized', 401
    rows = db_execute("""
        SELECT * FROM pedidos
         WHERE status='aguardando_pagto'
           AND criado_em < NOW() - INTERVAL '24 hours'
           AND criado_em > NOW() - INTERVAL '48 hours'
           AND COALESCE(observacao,'') NOT LIKE '%[lembrete-enviado]%'
        LIMIT 50""", fetch='all') or []
    enviados = 0
    for p in rows:
        try:
            enviar_whatsapp(p['telefone'],
                f"💛 Oi {p['nome'].split()[0]}! "
                f"Vi que você começou um pedido aqui na Luqui mas ainda não finalizou.\n\n"
                f"Total: *{rs(p['total'])}*\n\n"
                f"Tá tudo certinho? Quer finalizar?\n"
                f"👉 https://www.luquibrinquedos.com.br/pedido/{p['id']}/pagamento")
            enviar_email(p['email'],
                f'Esqueceu de finalizar seu pedido #{p["id"]}?',
                f"""<p>Oi {p['nome'].split()[0]}! 💛</p>
<p>Notamos que você começou um pedido aqui na Luqui mas ainda não finalizou o pagamento.</p>
<p><b>Total:</b> {rs(p['total'])}</p>
<p><a href="https://www.luquibrinquedos.com.br/pedido/{p['id']}/pagamento"
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


@app.route('/promocoes')
def pag_promocoes():
    """Página com produtos em promoção (puxa do PDV Pro)."""
    rs_promos = pdv_get('/api/integracao/promocoes') or {}
    return render_template('promocoes.html',
                           promocoes=rs_promos.get('promocoes', []),
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


@app.errorhandler(404)
def pag_404(e):
    sugestoes, _ = listar_produtos(limite=8)
    return render_template('404.html',
                           sugestoes=sugestoes or [],
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler()), 404


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
    return render_template('busca.html',
                           produtos=produtos, total=total,
                           termo=q or 'Busca', termo_q=q,
                           categorias=listar_categorias(),
                           filtros=listar_filtros_planos(),
                           filtros_ativos=extras,
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler())


def _pagina_destaque(tag, titulo):
    """Renderiza a pagina /novidades, /mais-vendidos, /liquida-luqui usando
    o template de busca, filtrando produtos com a flag de destaque no PDV."""
    extras = filtros_da_querystring(request)
    # garante que o destaque sempre fica fixado mesmo que o usuario clique filtros
    extras['destaque'] = [tag]
    produtos, total = listar_produtos(limite=48, **extras)
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
    return _pagina_destaque('novidade', '✨ Novidades')


@app.route('/mais-vendidos')
def pag_mais_vendidos():
    return _pagina_destaque('mais_vendido', '⭐ Mais vendidos')


@app.route('/liquida-luqui')
def pag_liquida_luqui():
    return _pagina_destaque('liquida', '💥 LiquidaLuqui')


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
    return render_template('produto.html',
                           p=p, avaliacoes=avals, media_estrelas=media,
                           relacionados=relacionados[:4],
                           categorias=listar_categorias(),
                           cliente=cliente_logado(),
                           carrinho=carrinho_ler(),
                           desconto_pix_pct=pix_pct,
                           parcelamento_max=parc_max,
                           parcelas_sem_juros_max=parc_sj,
                           parcela_sj_valor=parcela_sj_valor,
                           parcela_max_valor=parcela_max_valor,
                           juros_parcelamento_am=juros_am)


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
    # Consulta saldo de pontos no PDV (se cliente logado e tem CPF)
    cli = cliente_logado()
    pontos_info = None
    if cli and cli.get('cpf'):
        pontos_info = pdv_consultar_pontos(cli['cpf'])
    return render_template('checkout.html',
                           itens=itens, subtotal=sub,
                           categorias=listar_categorias(),
                           cliente=cli,
                           carrinho=itens,
                           desconto_pix_pct=float(cfg('desconto_pix_pct', '3')),
                           desconto_boleto_pct=float(cfg('desconto_boleto_pct', '5')),
                           parcelamento_max=int(cfg('parcelamento_max', '12')),
                           parcelas_sem_juros_max=int(cfg('parcelas_sem_juros_max', '1')),
                           parcela_minima=float(cfg('parcela_minima', '50')),
                           juros_parcelamento_am=float(cfg('juros_parcelamento_am', '2.49')),
                           pontos_info=pontos_info)


@app.route('/api/checkout/consultar-pontos')
def checkout_consultar_pontos():
    """Consulta pontos pelo CPF informado no formulario (cliente sem login
    ou pra revalidar). Retorna saldo e valor disponivel."""
    cpf = (request.args.get('cpf') or '').strip()
    info = pdv_consultar_pontos(cpf)
    if not info:
        return jsonify({'cliente_existe': False, 'saldo': 0, 'valor_disponivel': 0})
    return jsonify(info)


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
            'prazo': 'Pronto em ~'+cfg('loja_tempo_separacao_min', '30')+' min',
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
    # Fallback hardcode (sem ME ou sem CEP) — sempre devolve algo
    if not [o for o in opcoes if (o.get('id') or '') != 'LOCAL']:
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
# ─── OAuth Google ────────────────────────────────────────────────────────────
# Login com conta Google. Setup:
# 1. Google Cloud Console → APIs & Services → Credentials → Create OAuth client ID
# 2. Tipo: Web application
# 3. Authorized redirect URI: https://www.luquibrinquedos.com.br/auth/google/callback
# 4. Pegar Client ID + Client Secret, colocar em GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET
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
    return redirect(next_url)


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
o brinquedo perfeito pra criancinha dela. Use a tool buscar_produtos
quando ela mencionar idade/interesse/tipo de brinquedo e MOSTRE
sugestoes diretas no chat.

TOM:
- Frases CURTAS (1-3 linhas). 1-2 emojis por mensagem.
- "Que delicia! 💛", "Vai amar de mais!", "Que ideia linda!"
- Espelhe a energia. NAO seja formal. NAO use markdown.

PRIMEIRA MENSAGEM (saudacao):
"Oi! 💛 Sou a Luquizinha 🧸 Vou te ajudar a achar o brinquedo perfeito!
Como vc se chama?"

DEPOIS, conversando, vc tenta descobrir (sem corrida, 1 pergunta por vez):
- Nome da pessoa (ja peguei? memorize)
- Idade da crianca
- Menino ou menina
- Tipo de brinquedo / interesse (boneca, carrinho, jogo, etc) — opcional

ASSIM QUE TIVER idade + sexo (ou tipo), use buscar_produtos pra trazer 3-6
sugestoes. NAO espere ter tudo — uma sugestao parcial ja vale a pena.

INFO QUE VOCE PODE DAR DIRETO (sempre que perguntarem):
💳 PIX 3% off, cartao 1x sem juros (2x+ tem juros, ate 12x)
🚚 Cascavel R$ 10 fixo, retire na loja gratis, outras cidades cota no checkout
🎁 Clube de Pontos: 1pt por R$1, vale R$0,10/pt, max 50% da compra
📍 Rua Engenheiro Reboucas, 2053 — Cascavel/PR
⏰ Seg-sex 9-18h · Sab 9-13h · Dom fechado

CUPOM DE PRIMEIRA COMPRA:
Se a pessoa parecer indecisa ou for cliente novo (sem login), mencione o
cupom PRIMEIRO10 (10% off em compras a partir de R$ 50).

QUANDO MARCAR LEAD:
Quando vc ja tiver pelo menos nome + idade + sexo da crianca, e a pessoa
demonstrou interesse mas nao finalizou, chame a tool registrar_lead pra
o vendedor humano dar acompanhamento. Faz isso 1 vez so por conversa.
Se a pessoa pediu pra "falar com o vendedor", chame tool tambem.

REGRAS:
- NAO invente valores que voce nao recebeu da tool. Se buscar_produtos
  nao trouxe nada, fala "deixa eu ver opcoes outras... que tal me contar
  mais um pouco do que vc procura?"
- Se a pessoa pedir produto que claramente nao existe (ex: "iphone"),
  diga gentilmente que voces sao loja de brinquedos.
- Se a pessoa quiser SO conversar / nao quer comprar nada, seja gentil
  mas curta. Nao force.
"""

LUQUIZINHA_TOOLS = [
    {
        "name": "buscar_produtos",
        "description": (
            "Busca brinquedos no catalogo pra recomendar a cliente. "
            "Use idade_anos, sexo, termo, preco_max. Devolve ate 8 "
            "produtos com {id, nome, preco, foto, url}. Os produtos sao "
            "automaticamente exibidos como cards no chat pra cliente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "idade_anos": {"type": "integer", "description": "Idade da crianca em anos (ex: 5)."},
                "sexo": {"type": "string", "enum": ["menino", "menina"], "description": "Sexo da crianca, se souber."},
                "termo": {"type": "string", "description": "Tipo de brinquedo (ex: 'boneca', 'carrinho', 'jogo de tabuleiro')."},
                "preco_max": {"type": "number", "description": "Limite de preco em reais (opcional)."},
            },
        },
    },
    {
        "name": "registrar_lead",
        "description": (
            "Marca a conversa como lead pro vendedor humano dar followup "
            "via WhatsApp. So chame quando tiver pelo menos nome + idade + "
            "sexo da crianca, e a pessoa demonstrou interesse real. Apos "
            "chamar, avise a cliente que o vendedor entra em contato."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nome": {"type": "string"},
                "telefone": {"type": "string", "description": "WhatsApp da cliente. Se nao tiver, deixe vazio."},
                "idade_crianca": {"type": "integer"},
                "sexo_crianca": {"type": "string", "enum": ["menino", "menina"]},
                "observacao": {"type": "string", "description": "Resumo curto do que a cliente procura (ex: 'menino 5 anos, gosta de carrinho hot wheels')."},
            },
            "required": ["nome", "idade_crianca", "sexo_crianca"],
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


def _luq_tool_buscar_produtos(args):
    """Tool: busca produtos via PDV e devolve lista resumida."""
    termo = (args.get('termo') or '').strip()
    idade = args.get('idade_anos')
    sexo = (args.get('sexo') or '').strip().lower()
    preco_max = args.get('preco_max')
    extras = {}
    if termo:
        pass  # busca textual via busca=
    # Mapeia sexo pra termo + departamento heuristico
    termos = []
    if termo:
        termos.append(termo)
    # Tentar incluir faixa etaria — depende dos valores cadastrados
    # Pra MVP, vamos so usar busca textual + filtro preco
    busca = ' '.join(termos) or None
    produtos, _ = listar_produtos(busca=busca, limite=8)
    out = []
    for p in produtos[:8]:
        preco = float(p.get('preco_promo') or p.get('preco_venda') or 0)
        if preco_max and preco > float(preco_max):
            continue
        out.append({
            'id': p.get('id'),
            'nome': p.get('descricao'),
            'preco': preco,
            'foto': p.get('foto_url') or '',
            'url': f"/produto/{p.get('id')}",
        })
    return {'produtos': out}


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
    messages = _luq_carregar_historico(conv['id'])
    # Loop tool use (max 4 turnos)
    resposta_texto = ''
    produtos_exibir = []
    for _ in range(4):
        payload = {
            'model': 'claude-haiku-4-5-20251001',  # mais barato pra chat
            'max_tokens': 800,
            'system': LUQUIZINHA_SITE_PROMPT,
            'tools': LUQUIZINHA_TOOLS,
            'messages': messages,
        }
        try:
            r = requests.post('https://api.anthropic.com/v1/messages',
                              headers={'Content-Type': 'application/json',
                                       'x-api-key': ANTHROPIC_API_KEY,
                                       'anthropic-version': '2023-06-01'},
                              json=payload, timeout=30)
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
        novo_conteudo = (request.form.get('conteudo') or '').strip()
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


@app.route('/admin/clientes')
@requer_admin
def admin_clientes():
    rows = db_execute("""
      SELECT c.id, c.nome, c.email, c.telefone, c.cpf, c.cidade, c.uf, c.criado_em,
             COUNT(DISTINCT p.id) FILTER (WHERE p.status IN ('pago','enviado','entregue')) AS qtd_pedidos,
             COALESCE(SUM(p.total) FILTER (WHERE p.status IN ('pago','enviado','entregue')),0) AS total_gasto,
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
                                  WHERE m.conversa_id=c.id AND m.role='user') AS msgs_user
                          FROM site_chat_conversas c
                          ORDER BY ultimo_msg_em DESC NULLS LAST LIMIT 200""",
                       fetch='all') or []
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
    if d['forma_pagto'] not in ('pix', 'cartao'):
        return jsonify({'erro': 'Forma de pagamento inválida'}), 400
    # Calcula totais
    subtotal = sum(float(it['preco']) * float(it['qtd']) for it in itens)
    frete = float(d.get('frete_valor') or 0)
    desconto_pix_pct = float(cfg('desconto_pix_pct', '10'))
    desconto_boleto_pct = float(cfg('desconto_boleto_pct', '5'))
    desconto = 0.0
    if d['forma_pagto'] == 'pix':
        desconto = round(subtotal * desconto_pix_pct / 100, 2)
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
    # Pontos do Clube (max 50% do total apos descontos PIX/cupom)
    pontos_resgatados = 0.0
    desconto_pontos = 0.0
    pontos_pedidos = 0.0
    try:
        pontos_pedidos = float(d.get('pontos_usar') or 0)
    except (TypeError, ValueError):
        pontos_pedidos = 0.0
    if pontos_pedidos > 0:
        info_pontos = pdv_consultar_pontos(d.get('cpf'))
        if info_pontos and info_pontos.get('cliente_existe'):
            saldo = float(info_pontos.get('saldo') or 0)
            vpp = float(info_pontos.get('valor_por_ponto') or 0)
            pontos_pedidos = min(pontos_pedidos, saldo)
            valor_em_reais = round(pontos_pedidos * vpp, 2)
            # Limite de 50% do total apos descontos
            parcial = max(0, subtotal + frete - desconto - cupom_desconto)
            limite_50 = round(parcial * 0.5, 2)
            if valor_em_reais > limite_50:
                valor_em_reais = limite_50
                pontos_pedidos = round(limite_50 / vpp, 2) if vpp > 0 else 0
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
    # Cria pedido no banco (status aguardando_pagto)
    cli = cliente_logado()
    embrulho = bool(d.get('embrulho_presente'))
    embrulho_msg = ((d.get('embrulho_mensagem') or '').strip()[:300]) if embrulho else None
    embrulho_tipo_raw = (d.get('embrulho_tipo') or '').strip().lower()
    embrulho_tipo = embrulho_tipo_raw if (embrulho and embrulho_tipo_raw in ('menino', 'menina', 'neutro')) else None
    entrega_agendada = ((d.get('entrega_agendada') or '').strip()[:40]) or None
    ped = db_execute("""
        INSERT INTO pedidos
          (cliente_id, email, nome, telefone, cpf, cep, endereco, numero,
           complemento, bairro, cidade, uf, subtotal, frete, desconto, total,
           forma_pagto, parcelas, frete_servico, frete_prazo, observacao,
           cupom_codigo, cupom_desconto, embrulho_presente, embrulho_mensagem,
           embrulho_tipo, juros_valor, entrega_agendada,
           pontos_resgatados, desconto_pontos)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id""",
        [cli['id'] if cli else None,
         d['email'].strip().lower(), d['nome'].strip(), d['telefone'].strip(),
         d['cpf'].strip(), d['cep'].strip(), d['endereco'].strip(),
         d['numero'].strip(), d.get('complemento') or None,
         d['bairro'].strip(), d['cidade'].strip(), d['uf'].strip().upper(),
         subtotal, frete, desconto + cupom_desconto + desconto_pontos, total,
         d['forma_pagto'], parcelas,
         d.get('frete_servico') or 'A definir',
         d.get('frete_prazo') or '', d.get('observacao') or None,
         cupom_codigo or None, cupom_desconto,
         embrulho, embrulho_msg, embrulho_tipo, juros_valor, entrega_agendada,
         pontos_resgatados, desconto_pontos],
        fetch='one')
    pid = ped['id']
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
    billing = {'pix': 'PIX', 'cartao': 'CREDIT_CARD'}[d['forma_pagto']]
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
                        """UPDATE pedidos SET nfe_ref=%s, nfe_numero=%s,
                           nfe_modelo=%s WHERE id=%s""",
                        [nfe_ref, str(resp_pdv.get('nfe_numero') or ''),
                         str(resp_pdv.get('nfe_modelo') or ''), pid])
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
