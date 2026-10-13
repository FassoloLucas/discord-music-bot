#!/usr/bin/env bash
# Provisiona uma VPS Ubuntu (Oracle Cloud Always Free - E2.1.Micro) do zero
# para rodar o bot de música. Rode como root (ou via sudo) uma única vez.
#
# Uso (com repositório git):
#   sudo ./setup_vps.sh https://github.com/seu-usuario/seu-repo.git
#
# Uso (arquivos já enviados via scp/sftp para uma pasta no servidor):
#   sudo ./setup_vps.sh /home/ubuntu/botdiscord

set -euo pipefail

SOURCE="${1:-}"
APP_USER="musicbot"
APP_DIR="/opt/musicbot"
SWAP_FILE="/swapfile"
SWAP_SIZE="2G"

echo "== 1/9: Atualizando o sistema =="
apt-get update -y
apt-get upgrade -y

echo "== 2/9: Instalando Python 3.11 (via uv) + ffmpeg, git, ufw =="
# Ubuntu 20.04/22.04 não trazem Python 3.10+ nos repositórios padrão, e o
# discord.py atual precisa de uma versão recente. Em vez de depender de PPAs
# de terceiros (o deadsnakes já parou de publicar para releases antigas do
# Ubuntu), usamos o "uv" (astral.sh) para baixar um binário pré-compilado do
# Python 3.11 — rápido e não depende de compilar nada.
apt-get install -y ffmpeg git ufw curl ca-certificates unzip

if ! command -v deno &>/dev/null; then
    # O YouTube exige resolver um desafio em JS pra liberar vários formatos
    # de áudio; o yt-dlp resolve isso sozinho usando um runtime JS (Deno).
    curl -fsSL https://deno.land/install.sh | env DENO_INSTALL=/usr/local sh
fi

if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

export UV_PYTHON_INSTALL_DIR=/opt/uv-python
mkdir -p "$UV_PYTHON_INSTALL_DIR"
/usr/local/bin/uv python install 3.11
chmod -R a+rX "$UV_PYTHON_INSTALL_DIR"
PYTHON_BIN="$(/usr/local/bin/uv python find 3.11)"
echo "Usando $PYTHON_BIN ($($PYTHON_BIN --version))"

echo "== 3/9: Instalando Cloudflare WARP (contorna bloqueio do YouTube por IP de datacenter) =="
# Mesmo com cookies válidos e PO Token, o YouTube ainda derruba boa parte
# do conteúdo de gravadora grande com LOGIN_REQUIRED quando a requisição
# vem de um IP de datacenter conhecido — é reputação do IP, não falta de
# autenticação. O WARP em modo proxy contorna isso sem afetar o resto do
# tráfego da VPS (só as requisições ao YouTube usam esse proxy — ver
# YOUTUBE_WARP_PROXY em cogs/music.py). Serviço grátis da Cloudflare.
if ! command -v warp-cli &>/dev/null; then
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" > /etc/apt/sources.list.d/cloudflare-client.list
    apt-get update -y
    apt-get install -y cloudflare-warp
fi
systemctl enable --now warp-svc
sleep 2
warp-cli --accept-tos registration new || true  # "|| true": não falha se já tiver registrado antes
warp-cli --accept-tos mode proxy
warp-cli --accept-tos connect
sleep 2
warp-cli --accept-tos status

echo "== 4/9: Criando swap de ${SWAP_SIZE} (a E2.1.Micro só tem 1GB de RAM) =="
if [ ! -f "$SWAP_FILE" ]; then
    fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE"
    swapon "$SWAP_FILE"
    echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
    sysctl -w vm.swappiness=10
    echo "vm.swappiness=10" >> /etc/sysctl.conf
else
    echo "Swap já existe, pulando."
fi

echo "== 5/9: Configurando firewall (ufw) =="
# O bot só faz conexões de SAÍDA (WebSocket/voice com o Discord), então a
# única porta de ENTRADA necessária é o SSH.
ufw allow OpenSSH
ufw --force enable

echo "== 6/9: Criando usuário de serviço sem privilégios =="
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

echo "== 7/9: Obtendo o código do bot =="
if [[ "$SOURCE" =~ ^(https?://|git@) ]]; then
    if [ -d "$APP_DIR/.git" ]; then
        sudo -u "$APP_USER" git -C "$APP_DIR" pull
    else
        sudo -u "$APP_USER" git clone "$SOURCE" "$APP_DIR"
    fi
elif [ -n "$SOURCE" ] && [ -d "$SOURCE" ]; then
    echo "Copiando arquivos de $SOURCE para $APP_DIR"
    mkdir -p "$APP_DIR"
    cp -a "$SOURCE"/. "$APP_DIR"/
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
else
    echo "Nenhuma origem informada — assumindo que os arquivos já estão em $APP_DIR."
fi

# Gerador de PO Token (bgutil-ytdlp-pot-provider) REMOVIDO: a versão
# 1.3.2 quebra a extração inteira do yt-dlp 2025.10.14 com um erro de
# incompatibilidade ("debug() got an unexpected keyword argument
# 'once'"). Cookies frescos (ver README.md > "Cookies do YouTube") já
# bastam sem PO Token na prática — só reinstalar se confirmar
# compatibilidade de versão antes.

echo "== 8/8: Criando venv, instalando dependências Python e o serviço systemd =="
sudo -u "$APP_USER" "$PYTHON_BIN" -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
    sudo -u "$APP_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    # WARP já foi instalado e conectado no passo 3/9 nesse endereço fixo.
    sudo -u "$APP_USER" sed -i 's|^YOUTUBE_WARP_PROXY=$|YOUTUBE_WARP_PROXY=socks5://127.0.0.1:40000|' "$APP_DIR/.env"
    echo ">>> IMPORTANTE: edite $APP_DIR/.env e coloque o DISCORD_TOKEN antes de iniciar o serviço."
fi

chmod +x "$APP_DIR/deploy/alert_webhook.sh"
cp "$APP_DIR/deploy/musicbot.service" /etc/systemd/system/musicbot.service
cp "$APP_DIR/deploy/musicbot-alert.service" /etc/systemd/system/musicbot-alert.service
systemctl daemon-reload
systemctl enable musicbot

echo
echo "Tudo pronto. Próximos passos:"
echo "  1. sudo nano $APP_DIR/.env        # cole o DISCORD_TOKEN"
echo "  2. sudo systemctl start musicbot"
echo "  3. sudo journalctl -u musicbot -f # ver os logs em tempo real"
