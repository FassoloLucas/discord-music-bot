#!/usr/bin/env bash
# Dispara um aviso num webhook do Discord quando o musicbot.service falha
# repetidamente e o systemd desiste de reiniciar sozinho (StartLimitBurst
# excedido — ver musicbot.service). Não roda em toda falha isolada, só
# quando o serviço fica "failed" de vez, então não vira spam.
#
# Configuração: defina ALERT_WEBHOOK_URL no /opt/musicbot/.env com uma URL
# de webhook de um canal do Discord (Config do servidor > Integrações >
# Webhooks > Novo Webhook > Copiar URL). Deixe vazio pra desativar.
set -euo pipefail

ENV_FILE="/opt/musicbot/.env"
[ -f "$ENV_FILE" ] || exit 0

WEBHOOK_URL="$(grep -E '^ALERT_WEBHOOK_URL=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
[ -n "$WEBHOOK_URL" ] || exit 0

HOST="$(hostname)"
curl -fsS -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "{\"content\": \"🔴 **musicbot** caiu e esgotou as tentativas de reinício automático em \`$HOST\`. Verifique: \`journalctl -u musicbot -n 100\`\"}" \
    >/dev/null
