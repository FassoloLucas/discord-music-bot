"""Playlist persistente por servidor (guild): uma lista compartilhada que
qualquer um vai completando com `/playlist add`, e que sobrevive a
reinícios do bot — fica salva em disco, em `data/playlists/<guild_id>.json`.

Não é a fila "ao vivo" (essa é o GuildPlayer.queue, em memória, definida em
cogs/music.py) — `/playlist play` copia o conteúdo salvo pra fila ao vivo,
sob demanda. Guarda só metadados leves (título, link/termo de busca,
duração, thumbnail) — nunca a URL de stream, que expira e é resolvida sob
demanda quando a faixa realmente toca.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger("musicbot.playlist_store")

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "playlists"


@dataclass
class PlaylistEntry:
    title: str
    source: str  # URL direta ou termo de busca — o que resolve_playable/extract_track recebe
    webpage_url: str | None
    duration: int | None
    thumbnail: str | None
    added_by: str  # nome de exibição de quem adicionou, só pra mostrar em /playlist show


class PlaylistStore:
    """Uma playlist em JSON por guild, com cache em memória.

    Escrita atômica (escreve num .tmp e troca com os.replace) pra nunca
    deixar o arquivo pela metade se o processo cair no meio de uma
    escrita.
    """

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._cache: dict[int, list[PlaylistEntry]] = {}
        self._lock = asyncio.Lock()

    def _path(self, guild_id: int) -> Path:
        return DATA_DIR / f"{guild_id}.json"

    def _load(self, guild_id: int) -> list[PlaylistEntry]:
        if guild_id in self._cache:
            return self._cache[guild_id]

        entries: list[PlaylistEntry] = []
        path = self._path(guild_id)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                entries = [PlaylistEntry(**item) for item in raw.get("entries", [])]
            except Exception:
                logger.exception("Playlist salva da guild %s corrompida, começando vazia", guild_id)

        self._cache[guild_id] = entries
        return entries

    async def _save(self, guild_id: int) -> None:
        entries = self._cache.get(guild_id, [])
        payload = json.dumps({"entries": [asdict(e) for e in entries]}, ensure_ascii=False, indent=2)
        path = self._path(guild_id)
        tmp_path = path.with_suffix(".json.tmp")

        def _write():
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(path)  # troca atômica: nunca deixa um .json pela metade

        async with self._lock:
            await asyncio.to_thread(_write)

    def get(self, guild_id: int) -> list[PlaylistEntry]:
        return list(self._load(guild_id))

    async def add(self, guild_id: int, entry: PlaylistEntry) -> None:
        self._load(guild_id).append(entry)
        await self._save(guild_id)

    async def add_many(self, guild_id: int, entries: list[PlaylistEntry]) -> int:
        self._load(guild_id).extend(entries)
        await self._save(guild_id)
        return len(entries)

    async def remove(self, guild_id: int, index: int) -> PlaylistEntry | None:
        """index é 1-based, como mostrado em /playlist show."""
        entries = self._load(guild_id)
        if index < 1 or index > len(entries):
            return None
        removed = entries.pop(index - 1)
        await self._save(guild_id)
        return removed

    async def clear(self, guild_id: int) -> int:
        entries = self._load(guild_id)
        count = len(entries)
        entries.clear()
        await self._save(guild_id)
        return count
