# Bot de música para Discord

Bot Python com comandos de barra, controles por botões, fila por servidor e playlists persistentes. Usa discord.py, yt-dlp e FFmpeg, sem servidor Lavalink. Inclui scripts de implantação Ubuntu/systemd e testes de lógica independente da rede.

## Arquitetura e recursos

`bot.py` configura o cliente e sincroniza comandos; `cogs/music.py` gerencia busca, áudio e fila; `utils/spotify.py` resolve metadados do Spotify; `utils/playlist_store.py` salva playlists em `data/playlists/`. `deploy/` contém implantação, atualização e alerta opcional por webhook.

Comandos: `/play`, `/skip`, `/pause`, `/resume`, `/stop`, `/leave`, `/queue`, `/remove`, `/clear`, `/shuffle`, `/nowplaying`, `/volume` e `/loop`. O grupo `/playlist` permite adicionar, tocar, listar, remover e limpar a playlist salva. Qualquer membro com acesso aos comandos pode controlar a reprodução; não há restrição por solicitante ou cargo.

## Instalação local

Pré-requisitos: Git, Python 3.11 como referência do script de implantação, FFmpeg no PATH e um bot criado por você no portal Discord Developer.

```powershell
git clone https://github.com/FassoloLucas/discord-music-bot.git
cd discord-music-bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Preencha `DISCORD_TOKEN` em seu `.env`. Convide seu bot com os escopos `bot` e `applications.commands` e permissões Connect, Speak, Send Messages e Use Slash Commands. O código usa comandos de barra, sem intents privilegiadas. `GUILD_ID` é opcional para sincronização de desenvolvimento num servidor específico.

```powershell
.\.venv\Scripts\python.exe bot.py
```

Entre em um canal de voz e use `/play` com uma busca ou link. Nenhum token, cookie ou webhook é fornecido.

## Busca e fontes

A busca por texto usa primeiro o catálogo Spotify, quando as credenciais opcionais estão configuradas, para identificar título/artista. O áudio correspondente é procurado no SoundCloud. Sem Spotify, a busca usa SoundCloud diretamente. A duração pode ajudar a escolher uma versão, sem garantir correspondência exata.

Links Spotify são resolvidos em metadados e buscas no SoundCloud: o bot não reproduz áudio do Spotify. Links diretos de sites suportados são processados pelo yt-dlp. YouTube não é a fonte padrão da busca por texto.

`SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` habilitam o catálogo. `YOUTUBE_COOKIES_FILE` aponta para um arquivo local de sessão, caso necessário para conteúdo autorizado. Cookies são credenciais: não os envie ao GitHub. `ALERT_WEBHOOK_URL` é opcional apenas para alertas na VPS.

## Implantação e testes

Os scripts de `deploy/` assumem Ubuntu, privilégios administrativos, usuário `musicbot`, instalação em `/opt/musicbot` e systemd. Leia-os antes de executar: atualizam pacotes, instalam ferramentas externas, configuram swap/firewall e também incluem instalação do Cloudflare WARP. Não são uma instalação mínima e não foram executados nesta publicação.

```bash
sudo bash deploy/setup_vps.sh https://github.com/FassoloLucas/discord-music-bot.git
```

Configure o `.env` criado no servidor e acompanhe `journalctl -u musicbot`. Para validação da lógica local:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

## Limitações

- Extração e reprodução dependem das fontes, disponibilidade e permissões do conteúdo. Use conteúdo que você está autorizado a acessar.
- Links YouTube podem falhar por login, restrições ou IP de datacenter, mesmo com cookies. Não há garantia de funcionamento nem solução universal.
- `YOUTUBE_WARP_PROXY` permanece vazio no exemplo: a documentação original relata URLs resolvidas que falham na reprodução. WARP não é garantia de correção.
- Fila ao vivo é temporária; playlist salva sobrevive a reinícios. Arquivos de dados e downloads locais são ignorados.
- Se faltar áudio, confira FFmpeg no PATH, permissões de voz e logs locais. Testes unitários não substituem uma demonstração num servidor Discord.

A publicação inclui código próprio e arquivos de implantação; não inclui dependências vendorizadas, cookies, tokens, logs ou playlists pessoais. A revisão foi estática, sem iniciar o bot, instalar dependências ou executar testes.
