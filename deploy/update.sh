#!/usr/bin/env bash
# Atualiza o bot já instalado (git pull + reinstala deps + reinicia o serviço).
# Rode como root/sudo na VPS, dentro ou fora de /opt/musicbot.
#
# Uso: sudo ./update.sh

set -euo pipefail

APP_USER="musicbot"
APP_DIR="/opt/musicbot"

echo "== Buscando atualizações =="
sudo -u "$APP_USER" git -C "$APP_DIR" pull

# Gerador de PO Token (bgutil-ytdlp-pot-provider) REMOVIDO: a versão
# 1.3.2 quebra a extração inteira do yt-dlp 2025.10.14 com um erro de
# incompatibilidade ("debug() got an unexpected keyword argument
# 'once'"). Se uma instalação antiga tiver isso instalado, desinstala:
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" uninstall -y bgutil-ytdlp-pot-provider 2>/dev/null || true
sudo rm -rf "$APP_DIR/bgutil-ytdlp-pot-provider"

echo "== Preparando o Cloudflare WARP (contorna bloqueio do YouTube por IP de datacenter) =="
if ! command -v warp-cli &>/dev/null; then
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" > /etc/apt/sources.list.d/cloudflare-client.list
    apt-get update -y
    apt-get install -y cloudflare-warp
fi
systemctl enable --now warp-svc
sleep 2
warp-cli --accept-tos registration new || true
warp-cli --accept-tos mode proxy
warp-cli --accept-tos connect
if ! grep -q '^YOUTUBE_WARP_PROXY=' "$APP_DIR/.env" 2>/dev/null; then
    echo "YOUTUBE_WARP_PROXY=socks5://127.0.0.1:40000" | sudo -u "$APP_USER" tee -a "$APP_DIR/.env" > /dev/null
elif grep -q '^YOUTUBE_WARP_PROXY=$' "$APP_DIR/.env" 2>/dev/null; then
    sudo -u "$APP_USER" sed -i 's|^YOUTUBE_WARP_PROXY=$|YOUTUBE_WARP_PROXY=socks5://127.0.0.1:40000|' "$APP_DIR/.env"
fi

echo "== Atualizando dependências Python =="
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" --upgrade

echo "== Reiniciando o serviço =="
chmod +x "$APP_DIR/deploy/alert_webhook.sh"
cp "$APP_DIR/deploy/musicbot.service" /etc/systemd/system/musicbot.service
cp "$APP_DIR/deploy/musicbot-alert.service" /etc/systemd/system/musicbot-alert.service
systemctl daemon-reload
systemctl restart musicbot

echo "== Status =="
systemctl --no-pager status musicbot
