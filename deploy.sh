#!/usr/bin/env bash
# deploy.sh — push pro main + confere que o deploy realmente subiu no Railway.
# Sem isso, auto-deploy do GitHub às vezes falha silenciosamente e o site
# continua rodando uma versão antiga sem aviso.
#
# Uso:  ./deploy.sh                  (push + confere; força redeploy se travar)
#       ./deploy.sh --no-push        (só confere, sem fazer push)
#       ./deploy.sh --force-redeploy (pula o auto-deploy, vai direto pra railway up)
#
# Env:  PROD_URL  (default https://www.luquibrinquedos.com.br)
set -euo pipefail

PROD_URL="${PROD_URL:-https://www.luquibrinquedos.com.br}"
NO_PUSH=0; FORCE_REDEPLOY=0
for a in "$@"; do
  case "$a" in
    --no-push) NO_PUSH=1 ;;
    --force-redeploy) FORCE_REDEPLOY=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
  esac
done

check_prod_mtime() {
  curl -s --max-time 6 "$PROD_URL/__version?v=$(date +%s)" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('mtime','0'))" 2>/dev/null \
    || echo "0"
}

MTIME_ANTES=$(check_prod_mtime)
LOCAL_SHA=$(git rev-parse HEAD | cut -c1-12)
echo "🔵 SHA local: $LOCAL_SHA · mtime prod ANTES: $MTIME_ANTES"

if [[ $NO_PUSH -eq 0 ]]; then
  echo "🔵 git push origin main..."
  git push origin main
fi

wait_until_redeploy() {
  local timeout_sec="$1" label="$2"
  local deadline=$(( $(date +%s) + timeout_sec ))
  while (( $(date +%s) < deadline )); do
    local agora
    agora=$(check_prod_mtime)
    if [[ "$agora" != "$MTIME_ANTES" && "$agora" != "0" ]]; then
      echo "✅ $label: container reiniciado (mtime $MTIME_ANTES → $agora)"
      return 0
    fi
    echo "   ($label) mtime=$agora ainda igual ao anterior — aguardando…"
    sleep 10
  done
  echo "⏱  $label: timeout."
  return 1
}

if [[ $FORCE_REDEPLOY -eq 0 ]]; then
  echo "🔵 Aguardando auto-deploy do GitHub (até 3 min)..."
  if wait_until_redeploy 180 "auto-deploy"; then
    exit 0
  fi
  echo "⚠️  Auto-deploy parece travado — forçando railway up."
fi

echo "🔵 railway up --detach (a env precisa estar linkada via 'railway link')…"
railway up --detach

echo "🔵 Aguardando build manual (até 5 min)…"
if wait_until_redeploy 300 "railway up"; then
  exit 0
fi

echo "❌ Deploy não subiu mesmo depois do redeploy manual."
echo "   Confere logs em https://railway.com/dashboard"
exit 1
