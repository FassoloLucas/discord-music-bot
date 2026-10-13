"""Resolve links do Spotify (faixa/álbum/playlist) em consultas de busca.

O Spotify não expõe stream de áudio bruto para bots (é DRM) — nenhum bot
"toca" áudio direto do Spotify de verdade, nem os grandes. A abordagem
padrão (usada por praticamente todo bot de música) é: pegar título +
artista via API do Spotify e tocar a faixa equivalente encontrada no
SoundCloud. Esse módulo só cuida da primeira parte; quem busca/toca é o
mesmo `extract_track` do cogs/music.py.
"""
from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass

import aiohttp

SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-\w+/)?(track|album|playlist)/([a-zA-Z0-9]+)"
)

MAX_PLAYLIST_TRACKS = 25  # evita enfileirar 300 músicas de uma vez numa VPS de 1GB


@dataclass
class SpotifyItem:
    query: str   # string pronta para buscar no SoundCloud (o áudio em si)
    title: str   # "artista - faixa", para mostrar pro usuário
    duration_ms: int | None = None  # só disponível quando vem de busca/faixa avulsa
    album: str | None = None        # idem


class SpotifyResolver:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @staticmethod
    def parse_url(url: str) -> tuple[str, str] | None:
        match = SPOTIFY_URL_RE.search(url)
        if not match:
            return None
        return match.group(1), match.group(2)  # (tipo, id)

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {auth}"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        self._token = data["access_token"]
        self._token_expires_at = time.monotonic() + data["expires_in"] - 30
        return self._token

    async def resolve(self, url: str) -> list[SpotifyItem]:
        """Retorna as faixas de um link do Spotify (1 para track, várias para álbum/playlist)."""
        parsed = self.parse_url(url)
        if not parsed:
            raise ValueError("Link do Spotify não reconhecido.")
        kind, spotify_id = parsed

        async with aiohttp.ClientSession() as session:
            token = await self._get_token(session)
            headers = {"Authorization": f"Bearer {token}"}

            if kind == "track":
                async with session.get(
                    f"https://api.spotify.com/v1/tracks/{spotify_id}", headers=headers
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                return [self._track_to_item(data)]

            if kind == "album":
                async with session.get(
                    f"https://api.spotify.com/v1/albums/{spotify_id}/tracks",
                    headers=headers,
                    params={"limit": MAX_PLAYLIST_TRACKS},
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                return [self._track_to_item(t) for t in data["items"]]

            # kind == "playlist"
            async with session.get(
                f"https://api.spotify.com/v1/playlists/{spotify_id}/tracks",
                headers=headers,
                params={"limit": MAX_PLAYLIST_TRACKS, "fields": "items(track(name,artists))"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            tracks = [item["track"] for item in data["items"] if item.get("track")]
            return [self._track_to_item(t) for t in tracks]

    async def search(self, query: str, limit: int = 5) -> list[SpotifyItem]:
        """Busca faixas no catálogo do Spotify por texto livre (nome, artista...).

        Usado como prioridade na busca por texto do /play e /playlist add
        — o catálogo do Spotify casa a intenção da busca muito melhor do
        que buscar o texto cru direto no SoundCloud (que mistura remixes,
        covers e uploads de fã com a faixa oficial). O áudio em si
        continua vindo do SoundCloud depois, via SpotifyItem.query.
        """
        async with aiohttp.ClientSession() as session:
            token = await self._get_token(session)
            headers = {"Authorization": f"Bearer {token}"}
            async with session.get(
                "https://api.spotify.com/v1/search",
                headers=headers,
                params={"q": query, "type": "track", "limit": limit},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        tracks = (data.get("tracks") or {}).get("items", [])
        return [self._track_to_item(t) for t in tracks]

    @staticmethod
    def _track_to_item(track: dict) -> SpotifyItem:
        title = track.get("name", "Desconhecida")
        artists = ", ".join(a["name"] for a in track.get("artists", []))
        display = f"{artists} - {title}" if artists else title
        album = (track.get("album") or {}).get("name")
        return SpotifyItem(
            query=f"{display} audio", title=display, duration_ms=track.get("duration_ms"), album=album
        )
