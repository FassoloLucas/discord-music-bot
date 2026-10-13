"""Testes do PlaylistStore: persistência em disco da playlist salva por
guild (usada por /playlist add/play/show/remove/clear). Usa um DATA_DIR
temporário isolado, nunca toca em data/playlists/ de verdade.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import playlist_store as ps


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "DATA_DIR", tmp_path / "playlists")
    return ps.PlaylistStore()


def _entry(title, added_by="Alguém"):
    return ps.PlaylistEntry(
        title=title, source=f"busca {title}", webpage_url=None, duration=None, thumbnail=None, added_by=added_by
    )


def test_empty_playlist_starts_empty(store):
    assert store.get(1) == []


def test_add_then_get(store):
    asyncio.run(store.add(1, _entry("Música A")))
    titles = [e.title for e in store.get(1)]
    assert titles == ["Música A"]


def test_add_many(store):
    asyncio.run(store.add_many(1, [_entry("A"), _entry("B"), _entry("C")]))
    assert [e.title for e in store.get(1)] == ["A", "B", "C"]


def test_guilds_are_isolated(store):
    asyncio.run(store.add(1, _entry("Da guild 1")))
    asyncio.run(store.add(2, _entry("Da guild 2")))
    assert [e.title for e in store.get(1)] == ["Da guild 1"]
    assert [e.title for e in store.get(2)] == ["Da guild 2"]


def test_persists_across_new_store_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "DATA_DIR", tmp_path / "playlists")
    store1 = ps.PlaylistStore()
    asyncio.run(store1.add_many(1, [_entry("A"), _entry("B")]))

    # Uma nova instância (equivalente a um restart do bot) tem que
    # reler do disco, não começar vazia.
    store2 = ps.PlaylistStore()
    assert [e.title for e in store2.get(1)] == ["A", "B"]


def test_remove_by_1_indexed_position(store):
    asyncio.run(store.add_many(1, [_entry("A"), _entry("B"), _entry("C")]))
    removed = asyncio.run(store.remove(1, 2))
    assert removed.title == "B"
    assert [e.title for e in store.get(1)] == ["A", "C"]


def test_remove_invalid_position_returns_none(store):
    asyncio.run(store.add(1, _entry("A")))
    assert asyncio.run(store.remove(1, 0)) is None
    assert asyncio.run(store.remove(1, 99)) is None
    assert len(store.get(1)) == 1


def test_clear_returns_count_and_empties(store):
    asyncio.run(store.add_many(1, [_entry("A"), _entry("B")]))
    count = asyncio.run(store.clear(1))
    assert count == 2
    assert store.get(1) == []


def test_corrupted_file_starts_empty_instead_of_crashing(tmp_path, monkeypatch):
    data_dir = tmp_path / "playlists"
    data_dir.mkdir(parents=True)
    (data_dir / "1.json").write_text("isso não é json válido {{{", encoding="utf-8")
    monkeypatch.setattr(ps, "DATA_DIR", data_dir)

    store = ps.PlaylistStore()
    assert store.get(1) == []
