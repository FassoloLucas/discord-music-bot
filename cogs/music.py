"""Cog de música: busca no SoundCloud (texto) ou resolve links diretos via
yt-dlp, e toca via FFmpeg.

Não usa Lavalink de propósito — em uma VPS com 1GB de RAM (Oracle Free
Tier E2.1.Micro), rodar uma JVM extra para o Lavalink é caro demais.
Este cog toca o áudio diretamente pelo processo de voz nativo do
discord.py, o que consome bem menos memória.
"""
from __future__ import annotations  # permite "int | None" etc. mesmo em Python 3.8/3.9

import asyncio
import logging
import os
import random
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

from utils.playlist_store import PlaylistEntry, PlaylistStore
from utils.spotify import MAX_PLAYLIST_TRACKS, SpotifyResolver

logger = logging.getLogger("musicbot.music")

IDLE_TIMEOUT_SECONDS = 5 * 60  # desconecta do canal de voz após 5 min sem tocar nada

# Depois de resolvida, uma URL de stream do YouTube costuma valer por
# algumas horas — mas pode expirar bem antes disso. Se uma faixa ficou
# parada na fila por mais tempo que isso, resolvemos de novo antes de
# tocar em vez de arriscar uma URL vencida (era exatamente esse o bug:
# faixas de playlist eram todas resolvidas de uma vez no momento do
# /play, então a última da fila quase sempre falhava ao tocar).
STREAM_URL_FRESH_SECONDS = 60

SEARCH_RESULTS = 5  # quantas opções mostrar quando /play é uma busca por texto

URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# O YouTube bloqueia extração de áudio vinda de IPs de datacenter/VPS
# ("Sign in to confirm you're not a bot"), mesmo pra vídeos públicos. A
# busca por texto (incluindo faixas resolvidas via Spotify) usa
# SoundCloud por padrão, que não precisa disso — só link do YouTube
# colado direto no /play ainda passa por aqui. A correção é usar cookies
# de uma conta logada — ver README.md > "Cookies do YouTube".
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip() or None
if YOUTUBE_COOKIES_FILE and not os.path.isfile(YOUTUBE_COOKIES_FILE):
    logger.warning(
        "YOUTUBE_COOKIES_FILE aponta para '%s', mas o arquivo não existe. "
        "Links diretos do YouTube provavelmente vão falhar com 'Sign in to confirm you're not a bot'.",
        YOUTUBE_COOKIES_FILE,
    )
    YOUTUBE_COOKIES_FILE = None

# Usada pra tocar de fato: resolve um link direto de qualquer site
# suportado pelo yt-dlp (YouTube, SoundCloud, Bandcamp, Vimeo, Twitch,
# etc.), e é o fallback de busca por texto (SoundCloud, ver comentário
# em "default_search" abaixo).
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    # Busca por texto vai pro SoundCloud, não pro YouTube: vídeos oficiais
    # de gravadora grande (VEVO/UMG/Sony/Warner) vêm travando com
    # "Sign in to confirm you're not a bot"/LOGIN_REQUIRED mesmo com
    # cookies válidos e PO Token configurado — é política do YouTube pra
    # esse tipo de conteúdo, sem correção conhecida. SoundCloud não tem
    # essa restrição, e a esmagadora maioria das músicas populares também
    # está lá. Link direto do YouTube colado no /play continua funcionando
    # normalmente (só a busca por nome que muda de fonte).
    "default_search": "scsearch",
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "source_address": "0.0.0.0",  # evita problemas de IPv6 em algumas VPS
    # O YouTube passou a exigir resolver um desafio em JS pra liberar a URL
    # de vários formatos de áudio. yt-dlp resolve isso sozinho com um
    # runtime JS (instalamos o Deno no setup_vps.sh), mas só baixa o script
    # resolvedor se autorizado explicitamente aqui.
    "remote_components": ["ejs:github"],
}

# Usada só para "espiar" uma busca ou link antes de tocar: descobre
# rapidamente títulos/duração (playlist, álbum, ou até 5 resultados de
# busca) sem resolver o stream de áudio de cada faixa — isso é feito
# depois, sob demanda, só na música que realmente vai tocar.
YTDL_PROBE_OPTIONS = {
    "extract_flat": True,
    "noplaylist": False,
    "playlistend": MAX_PLAYLIST_TRACKS,
    "skip_download": True,
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
}

if YOUTUBE_COOKIES_FILE:
    YTDL_OPTIONS["cookiefile"] = YOUTUBE_COOKIES_FILE
    YTDL_PROBE_OPTIONS["cookiefile"] = YOUTUBE_COOKIES_FILE

# Opcional: mesmo com cookies válidos e PO Token, o YouTube ainda bloqueia
# com LOGIN_REQUIRED boa parte do conteúdo de gravadora grande quando a
# requisição vem de um IP de datacenter conhecido (Oracle, AWS, etc.) —
# não é falta de autenticação, é reputação do IP. Rodar o Cloudflare WARP
# em modo proxy na VPS (grátis, ver README.md > "Cloudflare WARP") e
# apontar pra ele aqui contorna isso na prática. Só afeta URLs do
# YouTube — SoundCloud/Spotify continuam na conexão normal, sem esse
# ponto a mais de falha. Deixe vazio pra não usar.
YOUTUBE_WARP_PROXY = os.getenv("YOUTUBE_WARP_PROXY", "").strip() or None

YOUTUBE_HOST_RE = re.compile(r"(?:youtube\.com|youtu\.be)", re.IGNORECASE)

def _ffmpeg_before_options(headers: dict[str, str] | None) -> str:
    """Monta o before_options do FFmpeg pra uma faixa, incluindo os headers
    HTTP (User-Agent, Referer etc.) que o yt-dlp usou pra resolver a URL.

    Sem isso, CDNs que checam esses headers (SoundCloud é um caso confirmado)
    rejeitam com 403 as requisições que o FFmpeg faz sozinho na hora de
    tocar — e como -reconnect fica tentando de novo, o processo não morre
    nem loga erro, só nunca produz áudio (trava em silêncio).

    Tem que ser string, não lista: essa versão do discord.py (2.7.1) só
    trata before_options como before_options de verdade quando é uma
    `str` (isinstance(before_options, str)) — passado como lista, o
    FFmpegAudio ignora silenciosamente e nem "-reconnect" chega a ser
    aplicado. A string inteira do bloco de headers (com "\\r\\n" cru
    separando cada linha) vai entre aspas duplas pra sobreviver ao
    shlex.split que o discord.py faz nessa string antes de montar os
    argumentos do processo.
    """
    parts = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if headers:
        header_str = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        header_str = header_str.replace('"', "'")  # aspas quebrariam o shlex.split acima
        parts += ["-headers", f'"{header_str}"']
    return " ".join(parts)

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
probe_ytdl = yt_dlp.YoutubeDL(YTDL_PROBE_OPTIONS)

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "musicbot_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Baixa a faixa de verdade (em vez de só resolver a URL do stream e deixar
# o FFmpeg buscar sozinho pela rede) e toca o arquivo local depois — ver
# resolve_playable. Isso evita uma classe inteira de problema descoberta
# na prática: CDNs que exigem os mesmos headers HTTP que o yt-dlp usou
# (senão dão 403) e, pior, casos em que o próprio FFmpeg trava num
# deadlock interno de threads lendo HLS direto da rede (confirmado com o
# SoundCloud: dados chegavam no socket mas o processo nunca lia, preso
# num futex pra sempre — sem erro, sem crash, só nunca tocava e travava
# a fila inteira, já que o player só avança quando o FFmpeg termina). O
# arquivo baixado é apagado assim que a faixa termina (ver
# GuildPlayer._player_loop). Não se aplica a lives de verdade (ver
# is_live em resolve_playable) — baixar uma live indefinidamente
# travaria pra sempre, então essas continuam sendo tocadas direto da
# URL de stream (com os headers do yt-dlp repassados pro FFmpeg, ver
# _ffmpeg_before_options).
ytdl_download = yt_dlp.YoutubeDL({**YTDL_OPTIONS, "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s")})

ytdl_youtube = probe_ytdl_youtube = ytdl_youtube_download = None
if YOUTUBE_WARP_PROXY:
    ytdl_youtube = yt_dlp.YoutubeDL({**YTDL_OPTIONS, "proxy": YOUTUBE_WARP_PROXY})
    probe_ytdl_youtube = yt_dlp.YoutubeDL({**YTDL_PROBE_OPTIONS, "proxy": YOUTUBE_WARP_PROXY})

    # O FFmpeg não passa pela WARP, e o YouTube assina a URL de stream
    # pro IP específico que a resolveu — a WARP não garante o mesmo IP
    # de saída entre conexões diferentes, então "resolver a URL pela
    # WARP e deixar o FFmpeg buscar sozinho" dá 403 quase sempre (
    # confirmado testando). A correção é baixar o áudio de verdade
    # através da própria conexão da WARP (mesmo processo, sem esse
    # descompasso de IP).
    ytdl_youtube_download = yt_dlp.YoutubeDL(
        {**YTDL_OPTIONS, "proxy": YOUTUBE_WARP_PROXY, "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s")}
    )


def _ytdl_for(url_or_query: str) -> yt_dlp.YoutubeDL:
    if ytdl_youtube and YOUTUBE_HOST_RE.search(url_or_query):
        return ytdl_youtube
    return ytdl


def _probe_ytdl_for(url_or_query: str) -> yt_dlp.YoutubeDL:
    if probe_ytdl_youtube and YOUTUBE_HOST_RE.search(url_or_query):
        return probe_ytdl_youtube
    return probe_ytdl


def format_duration(seconds: int | None) -> str:
    """Formata segundos como m:ss ou h:mm:ss. Usado em embeds e no picker de busca."""
    if not seconds:
        return "?"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class Track:
    title: str
    source: str  # o que resolve_playable recebe: URL direta ou termo de busca
    webpage_url: str | None
    duration: int | None
    thumbnail: str | None
    requester: discord.abc.User
    stream_url: str | None = None
    http_headers: dict[str, str] | None = None  # headers que o yt-dlp usou pra resolver stream_url (ver _ffmpeg_before_options)
    resolved_at: float = 0.0
    local_file: str | None = None  # ver DOWNLOAD_DIR — faixa baixada, tocada sem acesso à rede


async def resolve_playable(track: Track, loop: asyncio.AbstractEventLoop) -> None:
    """Resolve (ou re-resolve) o áudio de uma faixa, in-place.

    Chamado sempre pouco antes de tocar — nunca no momento do /play — pra
    garantir dados frescos e pra preencher metadados que faixas vindas de
    playlist ainda não tinham (foram enfileiradas de forma leve, sem
    resolver nada, pra não travar o /play numa playlist grande).

    Quando `track.source` é texto de busca (não um link — normalmente
    faixas vindas do Spotify, cujo áudio real precisa ser achado no
    SoundCloud), busca explicitamente e escolhe a versão com duração mais
    parecida com `track.duration` (quando já conhecida), em vez de deixar
    o yt-dlp pegar cegamente o 1º resultado — o SoundCloud é cheio de
    remix/extended mix/mashup misturados com a faixa original nos
    resultados de busca.

    Faixas normais são BAIXADAS aqui (não só tem a URL de stream
    resolvida) — ver DOWNLOAD_DIR/ytdl_download. Deixar o FFmpeg buscar
    sozinho pela rede se mostrou frágil na prática: CDNs que exigem os
    headers HTTP do yt-dlp (senão dão 403) e casos em que o próprio
    FFmpeg trava lendo HLS direto (deadlock interno de threads,
    confirmado com o SoundCloud). Baixar primeiro evita as duas coisas.
    Exceção: lives de verdade (`is_live`) continuam tocando direto da
    URL de stream — baixar uma live travaria pra sempre.
    """
    source = track.source
    if not URL_RE.match(source):
        choices = await search_choices(source, loop)
        if choices:
            source = _rank_by_duration(choices, track.duration)[0]["source"]

    dl = _ytdl_for(source)
    via_warp = dl is ytdl_youtube
    download_dl = ytdl_youtube_download if via_warp else ytdl_download

    # Extrai metadados sem baixar primeiro, só pra descobrir se é uma
    # live de verdade antes de decidir como tocar.
    probe_extractor = partial(dl.extract_info, source, download=False)
    data = await loop.run_in_executor(None, probe_extractor)

    if data is None:
        raise ValueError("Não encontrei nada com esse termo.")

    if "entries" in data:
        entries = [e for e in data["entries"] if e]
        if not entries:
            raise ValueError("Não encontrei nada com esse termo.")
        data = entries[0]

    is_live = bool(data.get("is_live") or data.get("live_status") == "is_live")

    if is_live:
        if "url" not in data:
            raise ValueError("Não consegui obter um stream de áudio pra isso.")
        track.stream_url = data["url"]
        track.http_headers = data.get("http_headers") or None
        track.local_file = None
    else:
        download_extractor = partial(download_dl.extract_info, source, download=True)
        data = await loop.run_in_executor(None, download_extractor)

        if data is None:
            raise ValueError("Não encontrei nada com esse termo.")

        if "entries" in data:
            entries = [e for e in data["entries"] if e]
            if not entries:
                raise ValueError("Não encontrei nada com esse termo.")
            data = entries[0]

        local_path = download_dl.prepare_filename(data)
        if not os.path.isfile(local_path):
            raise ValueError("O download não gerou um arquivo utilizável.")

        track.local_file = local_path
        track.stream_url = None
        track.http_headers = None

    track.resolved_at = time.monotonic()
    track.title = data.get("title") or track.title
    track.webpage_url = data.get("webpage_url") or track.webpage_url
    track.duration = data.get("duration") if data.get("duration") is not None else track.duration
    track.thumbnail = data.get("thumbnail") or track.thumbnail


async def extract_track(
    query: str,
    loop: asyncio.AbstractEventLoop,
    requester: discord.abc.User,
    *,
    expected_duration: int | None = None,
) -> Track:
    """Busca/valida a query via yt-dlp (bloqueante) em uma thread separada, já resolvendo o stream.

    `expected_duration` (segundos), quando informado, é usado só pra
    ajudar a escolher a versão certa no SoundCloud quando `query` é texto
    de busca — ver resolve_playable. Depois de resolvida, a faixa fica
    com a duração real (não a esperada).
    """
    track = Track(
        title=query,
        source=query,
        webpage_url=None,
        duration=expected_duration,
        thumbnail=None,
        requester=requester,
    )
    await resolve_playable(track, loop)
    return track


def _entry_source(entry: dict) -> str:
    """Reconstrói uma URL tocável a partir de uma entrada "achatada" (extract_flat)."""
    # webpage_url primeiro: pro SoundCloud, "url" é um endpoint interno da
    # API (api.soundcloud.com/tracks/...), não a página pública — usar
    # webpage_url é o link canônico e sempre existe quando disponível.
    url = entry.get("webpage_url") or entry.get("url") or ""
    if url and not URL_RE.match(url):
        # Em geral é só o video ID do YouTube quando vem achatado.
        return f"https://www.youtube.com/watch?v={url}"
    return url or entry.get("id", "")


async def probe_query(
    query: str, loop: asyncio.AbstractEventLoop, spotify: SpotifyResolver | None
) -> list[tuple[str, str | None, int | None]]:
    """Descobre o que uma /play precisa tocar, sem resolver stream de áudio ainda.

    Retorna uma lista de (fonte, título_conhecido, duração_conhecida_seg).
    "Fonte" é o que o player vai receber depois (texto de busca ou URL
    direta) pra resolver o stream sob demanda. A duração (quando
    disponível) serve pra escolher a versão certa no SoundCloud depois —
    ver _rank_by_duration — e evitar remix/extended mix/mashup quando já
    sabemos a duração oficial (ex.: via Spotify). Um link avulso -> lista
    com 1 item. Playlist/álbum -> várias.
    """
    if spotify and "open.spotify.com" in query:
        items = await spotify.resolve(query)
        if not items:
            raise ValueError("Não achei faixas nesse link do Spotify.")
        return [(item.query, item.title, (item.duration_ms // 1000) if item.duration_ms else None) for item in items]

    extractor = partial(_probe_ytdl_for(query).extract_info, query, download=False)
    data = await loop.run_in_executor(None, extractor)
    if data is None:
        raise ValueError("Não encontrei nada nesse link.")

    if "entries" in data:
        entries = [e for e in data["entries"] if e]
        if not entries:
            raise ValueError("Essa playlist está vazia (ou é privada).")
        return [(_entry_source(e), e.get("title"), e.get("duration")) for e in entries]

    return [(query, data.get("title"), data.get("duration"))]


async def search_choices(query: str, loop: asyncio.AbstractEventLoop) -> list[dict]:
    """Busca até SEARCH_RESULTS resultados de texto no SoundCloud (não no
    YouTube — ver comentário em YTDL_OPTIONS), sem resolver stream ainda."""
    extractor = partial(probe_ytdl.extract_info, f"scsearch{SEARCH_RESULTS}:{query}", download=False)
    data = await loop.run_in_executor(None, extractor)
    if not data or "entries" not in data:
        return []

    choices = []
    for entry in data["entries"]:
        if not entry:
            continue
        choices.append(
            {
                "title": entry.get("title") or "Desconhecido",
                "duration": entry.get("duration"),
                "uploader": entry.get("uploader") or "",
                "source": _entry_source(entry),
            }
        )
    return choices


async def youtube_search_choices(query: str, loop: asyncio.AbstractEventLoop) -> list[dict]:
    """Busca até SEARCH_RESULTS resultados de texto no YouTube via WARP.

    Só faz sentido chamar isso quando YOUTUBE_WARP_PROXY está configurado
    (probe_ytdl_youtube não é None) — sem a WARP, essa busca cai no mesmo
    LOGIN_REQUIRED de sempre pra conteúdo de gravadora grande. Ver
    Music._search_choices, que tenta isso primeiro e cai pro Spotify +
    SoundCloud se falhar ou não achar nada.
    """
    extractor = partial(probe_ytdl_youtube.extract_info, f"ytsearch{SEARCH_RESULTS}:{query}", download=False)
    data = await loop.run_in_executor(None, extractor)
    if not data or "entries" not in data:
        return []

    choices = []
    for entry in data["entries"]:
        if not entry:
            continue
        choices.append(
            {
                "title": entry.get("title") or "Desconhecido",
                "duration": entry.get("duration"),
                "uploader": entry.get("uploader") or "",
                "source": _entry_source(entry),
            }
        )
    return choices


# Quanto de diferença (em segundos) ainda conta como "a mesma faixa" pra
# efeito de ranquear resultados do SoundCloud. Acima disso, é bem provável
# que seja remix/extended mix/mashup em vez da versão original.
DURATION_MATCH_TOLERANCE = 15


def _rank_by_duration(choices: list[dict], target_seconds: int | None) -> list[dict]:
    """Reordena resultados de busca preferindo duração parecida com a
    faixa "oficial" — o SoundCloud é cheio de remix/extended mix/mashup
    misturados com a versão original nos resultados de busca; quando
    já sabemos a duração certa (normalmente via Spotify), isso evita que
    um desses apareça primeiro só por acaso.

    Sem duração de referência, devolve a lista como veio (ordem de
    relevância da busca).
    """
    if not target_seconds:
        return choices

    def distance(choice: dict) -> float:
        duration = choice.get("duration")
        if not duration:
            return float("inf")  # duração desconhecida vai pro fim, não pro topo
        return abs(duration - target_seconds)

    return sorted(choices, key=distance)


def track_embed(track: Track, *, author: str = "🎶 Tocando agora") -> discord.Embed:
    embed = discord.Embed(title=track.title, url=track.webpage_url, color=discord.Color.blurple())
    embed.set_author(name=author)
    embed.add_field(name="Duração", value=format_duration(track.duration))
    embed.add_field(name="Pedido por", value=track.requester.display_name)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def _added_to_queue_embed(track: Track) -> discord.Embed:
    embed = discord.Embed(description=f"✅ Adicionado à fila: **{track.title}**", color=discord.Color.green())
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


class PlayerControls(discord.ui.View):
    """Botões anexados à mensagem "tocando agora": pausar/retomar, pular,
    parar, e ver o vídeo (o Discord não deixa um bot transmitir vídeo de
    verdade na call — isso é exclusivo de conta de usuário/"Go Live". O
    que dá pra fazer é mandar o link cru: o Discord já unfurl automático
    num player jogável, exatamente como quando alguém cola o link no
    chat. Manda só pra quem clicou, pra não poluir o canal pra geral)."""

    def __init__(self, player: "GuildPlayer"):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Não há nada tocando agora.", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Pausado.", ephemeral=True)
        else:
            vc.resume()
            await interaction.response.send_message("▶️ Retomado.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Não há nada tocando agora.", ephemeral=True)
            return
        self.player.loop_current = False
        vc.stop()
        await interaction.response.send_message("⏭️ Música pulada.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: Music | None = interaction.client.get_cog("Music")
        if cog is None:
            return
        await cog.do_stop(self.player, interaction.guild_id)
        await interaction.response.send_message("⏹️ Parado e desconectado.", ephemeral=True)

    @discord.ui.button(label="Ver vídeo", emoji="🎬", style=discord.ButtonStyle.secondary)
    async def show_video(self, interaction: discord.Interaction, button: discord.ui.Button):
        track = self.player.current
        if not track or not track.webpage_url:
            await interaction.response.send_message("Sem link de vídeo disponível pra essa faixa.", ephemeral=True)
            return
        # Manda o link cru (não dentro de um embed nosso) pra o Discord
        # unfurl automaticamente num preview jogável — só visível pra
        # quem clicou.
        await interaction.response.send_message(f"🎬 **{track.title}**\n{track.webpage_url}", ephemeral=True)


QUEUE_PAGE_SIZE = 10


class QueueView(discord.ui.View):
    """Pagina uma lista de linhas em blocos de QUEUE_PAGE_SIZE, só quem
    chamou o comando navega. Usada por /queue e /playlist show."""

    def __init__(self, owner_id: int, lines: list[str], *, title: str = "🎵 Fila de músicas"):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.lines = lines
        self.title = title
        self.page = 0
        self.max_page = max(0, (len(lines) - 1) // QUEUE_PAGE_SIZE)
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.max_page

    def embed(self) -> discord.Embed:
        start = self.page * QUEUE_PAGE_SIZE
        chunk = self.lines[start : start + QUEUE_PAGE_SIZE] or ["Vazia."]
        embed = discord.Embed(title=self.title, description="\n".join(chunk), color=discord.Color.blurple())
        embed.set_footer(text=f"Página {self.page + 1}/{self.max_page + 1}")
        return embed

    async def _turn_page(self, interaction: discord.Interaction, delta: int):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Só quem usou /queue pode navegar.", ephemeral=True)
            return
        self.page = max(0, min(self.max_page, self.page + delta))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn_page(interaction, -1)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._turn_page(interaction, 1)


class TrackPickerView(discord.ui.View):
    """Menu de seleção mostrado quando uma busca por texto tem várias
    opções — usado tanto por /play (enfileira na hora) quanto por
    /playlist add (salva na playlist). `on_choice` decide o que fazer com
    a escolha e devolve o embed de confirmação."""

    def __init__(
        self,
        requester: discord.abc.User,
        choices: list[dict],
        on_choice,  # async def on_choice(chosen: dict) -> discord.Embed
        *,
        command_hint: str = "/play",
    ):
        super().__init__(timeout=60)
        self.requester = requester
        self.choices = choices
        self.on_choice = on_choice
        self.command_hint = command_hint
        self.message: discord.Message | None = None

        options = [
            discord.SelectOption(
                label=choice["title"][:100],
                description=f'{choice["uploader"]} · {format_duration(choice["duration"])}'[:100],
                value=str(i),
            )
            for i, choice in enumerate(choices)
        ]
        select = discord.ui.Select(placeholder="Escolha uma música...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                f"Só quem usou {self.command_hint} pode escolher.", ephemeral=True
            )
            return

        self.stop()
        idx = int(interaction.data["values"][0])
        chosen = self.choices[idx]
        await interaction.response.edit_message(content=f"🔎 Buscando **{chosen['title']}**...", view=None)

        try:
            embed = await self.on_choice(chosen)
        except Exception as exc:
            logger.warning("Falha ao buscar '%s': %s", chosen["source"], exc)
            await interaction.edit_original_response(content=f"⚠️ Não consegui: {exc}")
            return

        await interaction.edit_original_response(content=None, embed=embed)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content=f"⌛ Tempo esgotado, use `{self.command_hint}` de novo.", view=None)
            except discord.HTTPException:
                pass


class GuildPlayer:
    """Estado de reprodução isolado por servidor (guild)."""

    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: deque[Track] = deque()
        self.voice_client: discord.VoiceClient | None = None
        self.current: Track | None = None
        self.volume: float = 0.5
        self.loop_current: bool = False
        self.text_channel: discord.abc.Messageable | None = None
        self._play_next_event = asyncio.Event()
        self._idle_task: asyncio.Task | None = None
        self._player_task = self.bot.loop.create_task(self._player_loop())

    def cancel_idle_timer(self):
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None

    def start_idle_timer(self):
        self.cancel_idle_timer()
        self._idle_task = self.bot.loop.create_task(self._idle_disconnect())

    async def _idle_disconnect(self):
        await asyncio.sleep(IDLE_TIMEOUT_SECONDS)
        if self.voice_client and not self.voice_client.is_playing():
            await self.voice_client.disconnect()
            if self.text_channel:
                await self.text_channel.send("👋 Saí do canal de voz por inatividade.")

    def _after_playback(self, error):
        if error:
            logger.error("Erro na reprodução: %s", error)
        self.bot.loop.call_soon_threadsafe(self._play_next_event.set)

    async def _player_loop(self):
        while True:
            self._play_next_event.clear()

            if self.loop_current and self.current:
                track = self.current
            else:
                if not self.queue:
                    self.current = None
                    self.start_idle_timer()
                    await self._play_next_event.wait()
                    continue
                track = self.queue.popleft()

            self.cancel_idle_timer()
            self.current = track

            if not self.voice_client or not self.voice_client.is_connected():
                # Desconectado enquanto esperava; descarta e segue.
                continue

            # Resolve (ou re-resolve) só agora, na hora de tocar — nunca
            # no momento do /play. Isso evita tocar uma URL vencida
            # (faixas de playlist chegam aqui sem nada resolvido ainda) e
            # detecta faixas que sumiram/ficaram indisponíveis nesse meio
            # tempo, sem travar o resto da fila. Faixas já baixadas
            # (local_file, ver DOWNLOAD_DIR) nunca ficam velhas — não
            # precisam ser resolvidas de novo.
            needs_resolve = track.local_file is None and (
                track.stream_url is None or (time.monotonic() - track.resolved_at) > STREAM_URL_FRESH_SECONDS
            )
            if needs_resolve:
                try:
                    await resolve_playable(track, self.bot.loop)
                except Exception as exc:
                    logger.warning("Falha ao resolver '%s' na hora de tocar: %s", track.title, exc)
                    self.loop_current = False
                    if self.text_channel:
                        await self.text_channel.send(
                            f"⚠️ Pulei **{track.title}** (não consegui tocar: link expirado ou indisponível)."
                        )
                    continue

            if track.local_file:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(track.local_file, options="-vn"), volume=self.volume
                )
            else:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(
                        track.stream_url,
                        before_options=_ffmpeg_before_options(track.http_headers),
                        options="-vn",
                    ),
                    volume=self.volume,
                )
            self.voice_client.play(source, after=self._after_playback)

            if self.text_channel:
                await self.text_channel.send(embed=track_embed(track), view=PlayerControls(self))

            await self._play_next_event.wait()

            # Apaga o arquivo baixado assim que a faixa termina (tocou até
            # o fim, foi pulada, ou falhou) — nunca deixa acumular no
            # disco. Se for repetir (/loop), baixa de novo na próxima
            # volta: needs_resolve vira True de novo porque local_file some.
            if track.local_file:
                try:
                    os.remove(track.local_file)
                except OSError:
                    pass
                track.local_file = None

    def shutdown(self):
        self.cancel_idle_timer()
        self._player_task.cancel()


class Music(commands.Cog):
    playlist_group = app_commands.Group(
        name="playlist",
        description="Playlist salva do servidor — todo mundo pode adicionar, sobrevive a reinício do bot",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.playlists = PlaylistStore()

        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.spotify = SpotifyResolver(client_id, client_secret) if client_id and client_secret else None
        if not self.spotify:
            logger.info("SPOTIFY_CLIENT_ID/SECRET não configurados — links do Spotify ficam desativados.")

        # Limpa arquivos baixados que sobraram de uma queda/restart
        # anterior (o _player_loop normalmente já apaga cada um assim que
        # a faixa termina, mas um crash no meio do processo pode deixar
        # órfãos pra trás).
        if DOWNLOAD_DIR.exists():
            for leftover in DOWNLOAD_DIR.iterdir():
                try:
                    leftover.unlink()
                except OSError:
                    pass

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self.bot, guild_id)
        return self.players[guild_id]

    async def do_stop(self, player: GuildPlayer, guild_id: int):
        """Para, limpa a fila, desconecta e derruba de vez o estado do player.

        Usado por /stop, /leave e pelo botão ⏹️ — centralizado aqui pra
        garantir que o player sempre seja removido de self.players (sem
        isso, a task _player_loop ficava viva pra sempre por guild depois
        de um /stop, um vazamento lento de memória).
        """
        player.queue.clear()
        player.loop_current = False
        if player.voice_client:
            if player.voice_client.is_playing() or player.voice_client.is_paused():
                player.voice_client.stop()
            if player.voice_client.is_connected():
                await player.voice_client.disconnect(force=True)
        player.shutdown()
        self.players.pop(guild_id, None)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        # Se o próprio bot foi desconectado do canal (kick, ou desconexão
        # manual pela UI do Discord) sem passar por /stop, limpa o player
        # do mesmo jeito — mesma proteção contra vazamento de memória.
        if member.id != self.bot.user.id or after.channel or not before.channel:
            return
        player = self.players.get(before.channel.guild.id)
        if player:
            player.queue.clear()
            player.loop_current = False
            player.shutdown()
            self.players.pop(before.channel.guild.id, None)

    async def ensure_voice(self, interaction: discord.Interaction) -> GuildPlayer | None:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "Você precisa estar em um canal de voz para usar esse comando.", ephemeral=True
            )
            return None

        player = self.get_player(interaction.guild_id)
        player.text_channel = interaction.channel

        channel = interaction.user.voice.channel
        if player.voice_client is None or not player.voice_client.is_connected():
            player.voice_client = await channel.connect()
        elif player.voice_client.channel != channel:
            await player.voice_client.move_to(channel)

        return player

    async def _search_choices(self, query: str) -> list[dict]:
        """Busca até SEARCH_RESULTS opções pra uma busca por texto — usada
        por /play e /playlist add.

        Ordem de prioridade:
        1. YouTube via WARP (só se YOUTUBE_WARP_PROXY estiver configurado)
           — melhor catálogo, sem a bagunça de remix/cover do SoundCloud.
        2. Catálogo do Spotify (quando configurado) — casa a intenção da
           busca melhor que jogar o texto cru no SoundCloud; a faixa
           escolhida ainda é resolvida via SoundCloud depois.
        3. Busca direta no SoundCloud, sempre disponível como último
           recurso — se a WARP cair ou o YouTube voltar a bloquear, a
           busca por nome continua funcionando sem quebrar nada.
        """
        if probe_ytdl_youtube:
            try:
                yt_choices = await youtube_search_choices(query, self.bot.loop)
            except Exception as exc:
                logger.warning("Falha ao buscar no YouTube (via WARP) '%s': %s", query, exc)
                yt_choices = []

            if yt_choices:
                return yt_choices

        if self.spotify:
            try:
                items = await self.spotify.search(query, limit=SEARCH_RESULTS)
            except Exception as exc:
                logger.warning("Falha ao buscar no Spotify '%s': %s", query, exc)
                items = []

            if items:
                return [
                    {
                        "title": item.title,
                        "duration": (item.duration_ms // 1000) if item.duration_ms else None,
                        "uploader": item.album or "",
                        "source": item.query,
                    }
                    for item in items
                ]

        return await search_choices(query, self.bot.loop)

    async def _add_single(
        self,
        interaction: discord.Interaction,
        player: GuildPlayer,
        source: str,
        *,
        expected_duration: int | None = None,
    ):
        try:
            track = await extract_track(source, self.bot.loop, interaction.user, expected_duration=expected_duration)
        except Exception as exc:
            logger.warning("Falha ao buscar '%s': %s", source, exc)
            await interaction.followup.send(f"⚠️ Não consegui buscar isso: {exc}")
            return

        player.cancel_idle_timer()
        player.queue.append(track)
        player._play_next_event.set()
        await interaction.followup.send(embed=_added_to_queue_embed(track))

    async def _enqueue_choice(
        self, interaction: discord.Interaction, player: GuildPlayer, chosen: dict
    ) -> discord.Embed:
        """Callback padrão de TrackPickerView pra /play: resolve a escolha e enfileira.

        A duração conhecida da escolha (ex.: vinda do Spotify) segue como
        dica pro resolve_playable escolher a versão certa no SoundCloud —
        ver extract_track/resolve_playable.
        """
        track = await extract_track(
            chosen["source"], self.bot.loop, interaction.user, expected_duration=chosen.get("duration")
        )
        player.cancel_idle_timer()
        player.queue.append(track)
        player._play_next_event.set()
        return _added_to_queue_embed(track)

    @app_commands.command(
        name="play",
        description="Toca uma música (nome, link do YouTube/SoundCloud/etc, ou link do Spotify)",
    )
    @app_commands.describe(query="Nome da música, ou link do YouTube, SoundCloud, Spotify, Bandcamp...")
    @app_commands.checks.cooldown(1, 3.0)
    async def play(self, interaction: discord.Interaction, query: str):
        player = await self.ensure_voice(interaction)
        if player is None:
            return

        await interaction.response.defer()

        is_spotify = "open.spotify.com" in query
        if is_spotify and not self.spotify:
            await interaction.followup.send(
                "⚠️ Suporte ao Spotify não está configurado neste bot "
                "(faltam `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` no `.env`)."
            )
            return

        # Busca por texto simples (não é link): mostra até 5 opções pra
        # escolher, em vez de cravar sempre o primeiro resultado (prioriza
        # o Spotify pra achar a faixa certa, ver _search_choices).
        if not URL_RE.match(query) and not is_spotify:
            try:
                choices = await self._search_choices(query)
            except Exception as exc:
                logger.warning("Falha ao buscar '%s': %s", query, exc)
                await interaction.followup.send(f"⚠️ Não consegui buscar isso: {exc}")
                return

            if not choices:
                await interaction.followup.send("Não encontrei nada com esse termo.")
                return

            player.cancel_idle_timer()

            if len(choices) == 1:
                await self._add_single(
                    interaction, player, choices[0]["source"], expected_duration=choices[0].get("duration")
                )
                return

            view = TrackPickerView(
                interaction.user,
                choices,
                lambda chosen: self._enqueue_choice(interaction, player, chosen),
                command_hint="/play",
            )
            view.message = await interaction.followup.send("🔎 Escolha uma música:", view=view, wait=True)
            return

        try:
            entries = await probe_query(query, self.bot.loop, self.spotify)
        except Exception as exc:
            logger.warning("Falha ao resolver '%s': %s", query, exc)
            await interaction.followup.send(f"⚠️ Não consegui processar isso: {exc}")
            return

        player.cancel_idle_timer()

        if len(entries) == 1:
            source, _title_hint, duration_hint = entries[0]

            if is_spotify:
                # O Spotify já deu o título e a duração certos, mas o
                # áudio de verdade ainda precisa ser achado no
                # SoundCloud — mostra as opções (com a mais parecida em
                # duração primeiro, ver _rank_by_duration) em vez de
                # confiar cegamente no 1º resultado da busca.
                try:
                    sc_choices = await search_choices(source, self.bot.loop)
                except Exception as exc:
                    logger.warning("Falha ao buscar '%s' no SoundCloud: %s", source, exc)
                    await interaction.followup.send(f"⚠️ Não consegui buscar isso: {exc}")
                    return

                if not sc_choices:
                    await interaction.followup.send("Não achei essa faixa no SoundCloud.")
                    return

                sc_choices = _rank_by_duration(sc_choices, duration_hint)

                if len(sc_choices) == 1:
                    await self._add_single(interaction, player, sc_choices[0]["source"])
                    return

                view = TrackPickerView(
                    interaction.user,
                    sc_choices,
                    lambda chosen: self._enqueue_choice(interaction, player, chosen),
                    command_hint="/play",
                )
                view.message = await interaction.followup.send(
                    "🔎 Achei várias versões no SoundCloud, escolha uma:", view=view, wait=True
                )
                return

            await self._add_single(interaction, player, source, expected_duration=duration_hint)
            return

        # Playlist/álbum: enfileira todas de forma leve (só título, sem
        # resolver stream) e sai da frente — cada faixa só é resolvida de
        # verdade quando chega a sua vez de tocar. Faixas quebradas são
        # puladas com um aviso no momento em que seriam tocadas, em vez de
        # travar o /play inteiro validando 25 faixas uma a uma.
        await self._enqueue_many(player, entries, interaction.user)
        await interaction.followup.send(f"✅ {len(entries)} faixa(s) adicionadas à fila.")

    async def _enqueue_many(
        self, player: GuildPlayer, entries: list[tuple[str, str | None, int | None]], requester: discord.abc.User
    ):
        for source, title_hint, duration_hint in entries:
            track = Track(
                title=title_hint or source,
                source=source,
                webpage_url=source if URL_RE.match(source) else None,
                duration=duration_hint,
                thumbnail=None,
                requester=requester,
            )
            player.queue.append(track)
            player._play_next_event.set()
            await asyncio.sleep(0)  # cede o loop de vez em quando em playlists grandes

    @app_commands.command(name="skip", description="Pula a música atual")
    @app_commands.checks.cooldown(1, 2.0)
    async def skip(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or not player.voice_client or not player.voice_client.is_playing():
            await interaction.response.send_message("Não há nada tocando agora.", ephemeral=True)
            return
        player.loop_current = False
        player.voice_client.stop()
        await interaction.response.send_message("⏭️ Música pulada.")

    @app_commands.command(name="pause", description="Pausa a reprodução")
    @app_commands.checks.cooldown(1, 2.0)
    async def pause(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or not player.voice_client or not player.voice_client.is_playing():
            await interaction.response.send_message("Não há nada tocando agora.", ephemeral=True)
            return
        player.voice_client.pause()
        await interaction.response.send_message("⏸️ Pausado.")

    @app_commands.command(name="resume", description="Retoma a reprodução")
    @app_commands.checks.cooldown(1, 2.0)
    async def resume(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or not player.voice_client or not player.voice_client.is_paused():
            await interaction.response.send_message("Não há nada pausado agora.", ephemeral=True)
            return
        player.voice_client.resume()
        await interaction.response.send_message("▶️ Retomado.")

    @app_commands.command(name="stop", description="Para a reprodução, limpa a fila e desconecta")
    @app_commands.checks.cooldown(1, 2.0)
    async def stop(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or not player.voice_client:
            await interaction.response.send_message("Não estou em um canal de voz.", ephemeral=True)
            return
        await self.do_stop(player, interaction.guild_id)
        await interaction.response.send_message("⏹️ Parado e desconectado.")

    @app_commands.command(name="leave", description="Desconecta do canal de voz")
    @app_commands.checks.cooldown(1, 2.0)
    async def leave(self, interaction: discord.Interaction):
        await self.stop.callback(self, interaction)

    @app_commands.command(name="queue", description="Mostra a fila de músicas")
    async def queue_(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or (not player.current and not player.queue):
            await interaction.response.send_message("A fila está vazia.", ephemeral=True)
            return

        lines = []
        if player.current:
            lines.append(f"**Tocando agora:** {player.current.title} ({format_duration(player.current.duration)})")
        for i, track in enumerate(player.queue, start=1):
            lines.append(f"`{i}.` {track.title} — pedido por {track.requester.display_name}")

        view = QueueView(interaction.user.id, lines)
        await interaction.response.send_message(embed=view.embed(), view=view)

    @app_commands.command(name="remove", description="Remove uma música da fila pela posição")
    @app_commands.describe(posicao="Posição da música na fila (veja com /queue)")
    async def remove(self, interaction: discord.Interaction, posicao: app_commands.Range[int, 1, 500]):
        player = self.players.get(interaction.guild_id)
        if not player or not player.queue or posicao > len(player.queue):
            await interaction.response.send_message("Posição inválida.", ephemeral=True)
            return
        track = player.queue[posicao - 1]
        del player.queue[posicao - 1]
        await interaction.response.send_message(f"🗑️ Removido da fila: **{track.title}**")

    @app_commands.command(name="clear", description="Limpa a fila (sem parar a música atual)")
    async def clear(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or not player.queue:
            await interaction.response.send_message("A fila já está vazia.", ephemeral=True)
            return
        removidas = len(player.queue)
        player.queue.clear()
        await interaction.response.send_message(f"🧹 Fila limpa ({removidas} música(s) removida(s)).")

    @app_commands.command(name="shuffle", description="Embaralha a fila")
    async def shuffle(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or len(player.queue) < 2:
            await interaction.response.send_message(
                "Não há músicas suficientes na fila para embaralhar.", ephemeral=True
            )
            return
        random.shuffle(player.queue)
        await interaction.response.send_message("🔀 Fila embaralhada.")

    @app_commands.command(name="nowplaying", description="Mostra a música atual")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or not player.current:
            await interaction.response.send_message("Nada tocando agora.", ephemeral=True)
            return
        await interaction.response.send_message(embed=track_embed(player.current), view=PlayerControls(player))

    @app_commands.command(name="volume", description="Ajusta o volume (0 a 150)")
    @app_commands.describe(level="Volume em porcentagem (0-150)")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 150]):
        player = self.players.get(interaction.guild_id)
        if not player or not player.voice_client:
            await interaction.response.send_message("Não estou em um canal de voz.", ephemeral=True)
            return
        player.volume = level / 100
        if player.voice_client.source:
            player.voice_client.source.volume = player.volume
        await interaction.response.send_message(f"🔊 Volume ajustado para {level}%.")

    @app_commands.command(name="loop", description="Ativa/desativa repetição da música atual")
    async def loop_cmd(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if not player or not player.current:
            await interaction.response.send_message("Nada tocando agora.", ephemeral=True)
            return
        player.loop_current = not player.loop_current
        estado = "ativada" if player.loop_current else "desativada"
        await interaction.response.send_message(f"🔁 Repetição {estado}.")

    # --- /playlist: lista salva do servidor, separada da fila ao vivo. ---
    # Fica gravada em disco (utils/playlist_store.py) e sobrevive a
    # reinícios — a fila normal (GuildPlayer.queue) é só em memória e
    # some quando o bot reinicia.

    @playlist_group.command(name="add", description="Adiciona uma música à playlist salva do servidor")
    @app_commands.describe(query="Nome da música, ou link do YouTube, SoundCloud, Spotify, Bandcamp...")
    async def playlist_add(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        is_spotify = "open.spotify.com" in query
        if is_spotify and not self.spotify:
            await interaction.followup.send(
                "⚠️ Suporte ao Spotify não está configurado neste bot "
                "(faltam `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` no `.env`)."
            )
            return

        # Prioriza o Spotify pra achar a faixa certa (ver _search_choices).
        if not URL_RE.match(query) and not is_spotify:
            try:
                choices = await self._search_choices(query)
            except Exception as exc:
                logger.warning("Falha ao buscar '%s': %s", query, exc)
                await interaction.followup.send(f"⚠️ Não consegui buscar isso: {exc}")
                return

            if not choices:
                await interaction.followup.send("Não encontrei nada com esse termo.")
                return

            async def _save_choice(chosen: dict) -> discord.Embed:
                entry = PlaylistEntry(
                    title=chosen["title"],
                    source=chosen["source"],
                    webpage_url=chosen["source"] if URL_RE.match(chosen["source"]) else None,
                    duration=chosen["duration"],
                    thumbnail=None,
                    added_by=interaction.user.display_name,
                )
                await self.playlists.add(interaction.guild_id, entry)
                return discord.Embed(
                    description=f"✅ Adicionado à playlist salva: **{entry.title}**", color=discord.Color.green()
                )

            if len(choices) == 1:
                await interaction.followup.send(embed=await _save_choice(choices[0]))
                return

            view = TrackPickerView(interaction.user, choices, _save_choice, command_hint="/playlist add")
            view.message = await interaction.followup.send("🔎 Escolha uma música pra playlist:", view=view, wait=True)
            return

        try:
            entries = await probe_query(query, self.bot.loop, self.spotify)
        except Exception as exc:
            logger.warning("Falha ao resolver '%s': %s", query, exc)
            await interaction.followup.send(f"⚠️ Não consegui processar isso: {exc}")
            return

        to_save = [
            PlaylistEntry(
                title=title_hint or source,
                source=source,
                webpage_url=source if URL_RE.match(source) else None,
                duration=duration_hint,
                thumbnail=None,
                added_by=interaction.user.display_name,
            )
            for source, title_hint, duration_hint in entries
        ]
        await self.playlists.add_many(interaction.guild_id, to_save)
        await interaction.followup.send(f"✅ {len(to_save)} faixa(s) adicionada(s) à playlist salva.")

    @playlist_group.command(name="play", description="Toca a playlist salva do servidor (adiciona tudo à fila)")
    async def playlist_play(self, interaction: discord.Interaction):
        entries = self.playlists.get(interaction.guild_id)
        if not entries:
            await interaction.response.send_message(
                "A playlist salva está vazia. Adicione com `/playlist add`.", ephemeral=True
            )
            return

        player = await self.ensure_voice(interaction)
        if player is None:
            return

        await interaction.response.defer()
        player.cancel_idle_timer()
        for entry in entries:
            track = Track(
                title=entry.title,
                source=entry.source,
                webpage_url=entry.webpage_url,
                duration=entry.duration,
                thumbnail=entry.thumbnail,
                requester=interaction.user,
            )
            player.queue.append(track)
            player._play_next_event.set()
            await asyncio.sleep(0)  # cede o loop de vez em quando em playlists salvas grandes
        await interaction.followup.send(f"✅ {len(entries)} faixa(s) da playlist salva adicionadas à fila.")

    @playlist_group.command(name="show", description="Mostra a playlist salva do servidor")
    async def playlist_show(self, interaction: discord.Interaction):
        entries = self.playlists.get(interaction.guild_id)
        if not entries:
            await interaction.response.send_message(
                "A playlist salva está vazia. Adicione com `/playlist add`.", ephemeral=True
            )
            return

        lines = [f"`{i}.` {e.title} — adicionado por {e.added_by}" for i, e in enumerate(entries, start=1)]
        view = QueueView(interaction.user.id, lines, title="📻 Playlist salva do servidor")
        await interaction.response.send_message(embed=view.embed(), view=view)

    @playlist_group.command(name="remove", description="Remove uma música da playlist salva pela posição")
    @app_commands.describe(posicao="Posição na playlist (veja com /playlist show)")
    async def playlist_remove(self, interaction: discord.Interaction, posicao: app_commands.Range[int, 1, 5000]):
        removed = await self.playlists.remove(interaction.guild_id, posicao)
        if removed is None:
            await interaction.response.send_message("Posição inválida.", ephemeral=True)
            return
        await interaction.response.send_message(f"🗑️ Removido da playlist salva: **{removed.title}**")

    @playlist_group.command(name="clear", description="Apaga toda a playlist salva do servidor")
    async def playlist_clear(self, interaction: discord.Interaction):
        count = await self.playlists.clear(interaction.guild_id)
        if count == 0:
            await interaction.response.send_message("A playlist salva já está vazia.", ephemeral=True)
            return
        await interaction.response.send_message(f"🧹 Playlist salva apagada ({count} música(s) removida(s)).")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
