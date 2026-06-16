"""Cron Railway: reconcilia pedidos pago sem pdv_venda_id.

Roda a cada 5 min via cronSchedule. Chama /cron/reconciliar-pedidos-site
do LuquiShop, que pega pedidos pagos no Asaas mas que NÃO foram criados
no PDV Pro (porque o webhook caiu/timeout no meio) e re-tenta o envio.

Sai com exit != 0 em caso de falha pra Railway marcar deploy CRASHED
e mandar email de aviso.
"""
import os
import sys
import urllib.request
import urllib.error

URL = os.environ.get(
    'CRON_URL',
    'https://www.luquibrinquedos.com.br/cron/reconciliar-pedidos-site'
)
TOKEN = os.environ.get('CRON_TOKEN', '')

if not TOKEN:
    print('CRON_TOKEN não configurado', file=sys.stderr)
    sys.exit(1)

req = urllib.request.Request(
    f'{URL}?token={TOKEN}',
    headers={'User-Agent': 'luquishop-cron-reconciliar-pedidos/1.0'},
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
