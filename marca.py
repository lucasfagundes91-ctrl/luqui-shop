"""Marca da loja (ShopPro) — tudo que muda de uma loja pra outra.

O mesmo código roda um deploy por loja (serviço + Postgres próprios no
Railway). Nome, CNPJ, telefone, endereços, cores e textos de vitrine vêm de
variáveis MARCA_* do ambiente; sem nenhuma setada, os valores são os da Luqui
Brinquedos e o site sai exatamente como sempre saiu.

Lista completa das variáveis: deploy/README-marca.md.
"""
import json
import logging
import os
from urllib.parse import quote_plus

log = logging.getLogger('luquishop')


def _env(chave, padrao):
    """MARCA_<CHAVE> do ambiente. Vazio usa o padrão; "-" zera o campo
    (ex.: MARCA_FACEBOOK=- pra loja sem Facebook)."""
    v = os.environ.get('MARCA_' + chave.upper())
    if v is None or v.strip() == '':
        return padrao
    return '' if v.strip() == '-' else v.strip()


def _env_bool(chave, padrao):
    v = os.environ.get('MARCA_' + chave.upper())
    if v is None or v.strip() == '':
        return padrao
    return v.strip().lower() in ('1', 'true', 'sim', 'on')


def _env_json(chave, padrao):
    v = os.environ.get('MARCA_' + chave.upper())
    if not v or not v.strip():
        return padrao
    try:
        return json.loads(v)
    except ValueError as e:
        # JSON torto não pode derrubar o site: loga e segue com o padrão.
        log.error('MARCA_%s inválido (%s) — usando o padrão', chave.upper(), e)
        return padrao


# Endereço da Luqui. `curto` é a forma abreviada usada em WhatsApp/e-mail
# ("R. Eng. Rebouças, 2053"); sem ele, cai em "logradouro, número".
_ENDERECOS_LUQUI = [{
    'nome': 'Luqui Brinquedos',
    'logradouro': 'R. Engenheiro Rebouças',
    'numero': '2053',
    'bairro': 'Centro',
    'cidade': 'Cascavel',
    'uf': 'PR',
    'cep': '85812-130',
    'obs': 'Estacionamento gratuito em frente',
    'curto': 'R. Eng. Rebouças, 2053',
}]

# Horário de funcionamento: linhas pro texto e a versão schema.org pro JSON-LD.
# O site da Luqui escreve o horário de quatro jeitos diferentes (página Retire
# na loja, /sobre, frete e WhatsApp). Pra não mudar uma vírgula na Luqui, cada
# um tem seu padrão; outra loja só seta MARCA_HORARIO e o resto deriva dele.
_HORARIO_LUQUI = ['Seg a sex: 09:00 às 18:00', 'Sábado: 09:00 às 13:00',
                  'Domingo: fechado']
_HORARIO_DERIVADOS_LUQUI = {
    'horario_sobre':    ['Segunda a sexta: 9h às 18h', 'Sábado: 9h às 13h',
                         'Domingo e feriado: fechado'],
    'horario_resumo':   'Seg a Sex: 9h às 18h · Sáb: 9h às 13h',
    'horario_whatsapp': 'Seg a Sex: 8h-18h · Sáb: 9h-13h',
    'horario_texto':    'segunda a sexta 9h-18h · sábado 9h-13h',
}
_HORARIO_SCHEMA_LUQUI = [
    {'dias': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
     'abre': '09:00', 'fecha': '18:00'},
    {'dias': 'Saturday', 'abre': '09:00', 'fecha': '13:00'},
]


def cep_digitos(cep):
    """Só os dígitos (CEP, telefone)."""
    return ''.join(c for c in (cep or '') if c.isdigit())


def _monta_endereco(e):
    """Completa os campos derivados que template e texto usam."""
    e = {k: (str(v).strip() if v is not None else '') for k, v in (e or {}).items()}
    for k in ('nome', 'logradouro', 'numero', 'bairro', 'cidade', 'uf', 'cep', 'obs'):
        e.setdefault(k, '')
    rua = e['logradouro'] + (', ' + e['numero'] if e['numero'] else '')
    cidade_uf = e['cidade'] + ('/' + e['uf'] if e['uf'] else '')
    e['rua'] = rua                                    # R. Engenheiro Rebouças, 2053
    e['cidade_uf'] = cidade_uf                        # Cascavel/PR
    e['curto'] = e.get('curto') or rua               # R. Eng. Rebouças, 2053
    # R. Engenheiro Rebouças, 2053 — Centro — Cascavel/PR
    e['linha'] = ' — '.join(x for x in (rua, e['bairro'], cidade_uf) if x)
    # Busca do Google Maps (embed do /sobre)
    q = rua
    if e['bairro']:
        q += ' - ' + e['bairro']
    q += ', ' + e['cidade'] + (' - ' + e['uf'] if e['uf'] else '')
    if e['cep']:
        q += ', ' + e['cep']
    e['maps_q'] = quote_plus(q)
    return e


def _carrega():
    _pct_online = _env('desconto_online_pct', '15').strip('%')
    m = {
        # Identidade
        'nome':             _env('nome', 'Luqui Brinquedos'),
        'nome_curto':       _env('nome_curto', 'Luqui'),
        'razao_social':     _env('razao_social', 'Luqui Brinquedos LTDA'),
        'cnpj':             _env('cnpj', '32.650.888/0001-02'),
        'slogan':           _env('slogan', 'Aqui você compra brincando'),
        'frase_rodape':     _env('frase_rodape', 'A loja de brinquedos da família.'),
        # "brinquedos" mantém os textos de vitrine da Luqui (pelúcias, Hot
        # Wheels, Luquizinha...). Qualquer outro valor usa os textos genéricos.
        'segmento':         _env('segmento', 'brinquedos'),
        'tipo_loja':        _env('tipo_loja', 'loja de brinquedos'),
        'item':             _env('item', 'brinquedo'),
        'itens':            _env('itens', 'brinquedos'),
        'meta_descricao':   _env('meta_descricao',
                                 'A loja de brinquedos da família — pelúcias, jogos, bonecas, '
                                 'hot wheels, lego e muito mais. Loja física em Cascavel/PR.'),
        'og_descricao':     _env('og_descricao',
                                 'Loja de brinquedos da família. Pelúcias, jogos, Hot Wheels, '
                                 'Lego, Pokémon e muito mais. Cascavel/PR — envio pra todo Brasil.'),
        'schema_tipo':      _env('schema_tipo', 'ToyStore'),
        # Contato
        'telefone_exibicao': _env('telefone_exibicao', '(45) 99111-9800'),
        'telefone_e164':    _env('telefone_e164', '+5545991119800'),
        'email':            _env('email', 'contato@luquibrinquedos.com.br'),
        'email_privacidade': _env('email_privacidade', ''),
        'instagram':        _env('instagram', 'luquibrinquedos'),
        'facebook':         _env('facebook', 'luquibrinquedos'),
        # Visual
        'logo':             _env('logo', '/static/logo.png'),
        'favicon':          _env('favicon', '/static/favicon.png'),
        'apple_touch_icon': _env('apple_touch_icon', '/static/apple-touch-icon.png'),
        # Nome-base dos PNG em static/brand (sem o -180/-192/-512)
        'icone':            _env('icone', 'luqui-icon'),
        'icone_admin':      _env('icone_admin', 'luqui-admin-icon'),
        'slug':             _env('slug', 'luqui'),
        'cor_tema':         _env('cor_tema', '#f5b20a'),
        'cor_primaria':        _env('cor_primaria', '#1652C7'),
        'cor_primaria_escura': _env('cor_primaria_escura', '#0E3D9E'),
        'cor_primaria_clara':  _env('cor_primaria_clara', '#3B82F6'),
        'cor_destaque':        _env('cor_destaque', '#FFC700'),
        'cor_destaque_escura': _env('cor_destaque_escura', '#E0A800'),
        # Recursos
        'assistente_ativo': _env_bool('assistente_ativo', True),
        'assistente_nome':  _env('assistente_nome', 'Luquizinha'),
        'lista_aniversario_ativa': _env_bool('lista_aniversario_ativa', True),
        'clube_nome':       _env('clube_nome', 'Clube Luqui'),
        # Nome curto que aparece na fatura do cartão (Pagar.me: até 13 letras)
        'descritor_fatura': _env('descritor_fatura', 'LUQUIBRINQ'),
        # Faixa da home "online você paga X% menos que na loja física" (0 = some)
        'desconto_online_pct': '' if _pct_online == '0' else _pct_online,
        # Emoji que fecha as mensagens de WhatsApp/e-mail
        'emoji':            _env('emoji', '🧸'),
        # CEP de origem do Melhor Envio (default da config /admin/melhorenvio)
        'cep_origem':       cep_digitos(_env('cep_origem', '85801080')),
        'horario':          _env_json('horario', _HORARIO_LUQUI),
        'horario_schema':   _env_json('horario_schema', _HORARIO_SCHEMA_LUQUI),
    }
    # Variações do horário: horario_sobre (linhas do /sobre), horario_resumo
    # (frete "Retirar na loja"), horario_whatsapp (pedido pronto) e
    # horario_texto (página de entregas). Sem MARCA_HORARIO, as da Luqui.
    tem_horario = bool((os.environ.get('MARCA_HORARIO') or '').strip())
    junta = ' · '.join(m['horario'])
    for k, padrao in _HORARIO_DERIVADOS_LUQUI.items():
        if not tem_horario:
            derivado = padrao
        elif k == 'horario_sobre':
            derivado = m['horario']
        else:
            derivado = junta
        m[k] = (_env_json(k, derivado) if k == 'horario_sobre'
                else _env(k, derivado))
    ends = _env_json('enderecos', _ENDERECOS_LUQUI)
    if not isinstance(ends, list) or not ends:
        log.error('MARCA_ENDERECOS precisa ser uma lista JSON não vazia — usando o padrão')
        ends = _ENDERECOS_LUQUI
    m['enderecos'] = [_monta_endereco(e) for e in ends]
    m['endereco'] = m['enderecos'][0]                 # loja principal
    m['brinquedos'] = m['segmento'] == 'brinquedos'
    # Perfis pro sameAs do JSON-LD (só os que existem)
    m['redes'] = ([f"https://instagram.com/{m['instagram']}"] if m['instagram'] else []) + \
                 ([f"https://facebook.com/{m['facebook']}"] if m['facebook'] else [])
    luqui = m['nome'] == 'Luqui Brinquedos'
    # Pedido LGPD: a Luqui tem caixa própria; outra loja usa o e-mail de contato.
    m['email_privacidade'] = m['email_privacidade'] or (
        'privacidade@luquibrinquedos.com.br' if luqui else m['email'])
    # Painel: a Luqui sempre se chamou "LuquiShop"; outra loja usa o próprio nome.
    m['admin_nome'] = _env('admin_nome', 'LuquiShop' if luqui else m['nome'])
    m['admin_titulo'] = _env('admin_titulo', m['admin_nome'] + ' Admin')
    m['gestao_nome'] = _env('gestao_nome', m['nome_curto'] + ' Gestão')
    # data-sistema do botão "Pedir ajuste" (BankPro)
    m['sistema_ajuste'] = _env('sistema_ajuste', 'Luqui Shop' if luqui else m['nome'])
    return m


MARCA = _carrega()


def enderecos_curtos(sep='\n', prefixo='📍 ', traco=' — '):
    """Endereços em texto corrido (WhatsApp, e-mail, prompt da IA).

    Com uma loja só sai igual ao texto antigo ("📍 R. Eng. Rebouças, 2053 —
    Cascavel/PR"); com várias, uma por linha com o nome da loja na frente."""
    ends = MARCA['enderecos']
    varias = len(ends) > 1
    return sep.join(prefixo + (e['nome'] + ': ' if varias and e['nome'] else '')
                    + e['curto'] + traco + e['cidade_uf'] for e in ends)
