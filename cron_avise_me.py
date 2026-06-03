"""Cron Railway: dispara avisos de produtos que voltaram ao estoque.

Roda periodicamente (a cada 30min) via cronSchedule. Chama o endpoint
/cron/avise-me do LuquiShop, que verifica produtos com cadastro
pendente em avise_me e envia email + WhatsApp pros cadastrados quando
o estoque voltou.

Sai com exit != 0 em caso de falha pra Railway marcar deploy CRASHED
e mandar email de aviso (assim a gente fica sabendo se quebrou).
"""
import os
import sys
import urllib.request
import urllib.error

URL = os.environ.get(
    'CRON_URL',
    'https://www.luquibrinquedos.com.br/cron/avise-me'
)
TOKEN = os.environ.get('CRON_TOKEN', '')

if not TOKEN:
    print('CRON_TOKEN não configurado', file=sys.stderr)
    sys.exit(1)

req = urllib.request.Request(
    f'{URL}?token={TOKEN}',
    headers={'User-Agent': 'luquishop-cron-avise-me/1.0'},
)
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read().decode('utf-8', errors='replace')
        print(f'[{r.status}] {body}')
        sys.exit(0)
except urllib.error.HTTPError as e:
    body = e.read().decode('utf-8', errors='replace')
    print(f'[{e.code}] {body}', file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f'Erro: {e}', file=sys.stderr)
    sys.exit(1)
