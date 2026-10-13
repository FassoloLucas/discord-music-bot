"""Testes das partes de cogs/music.py que não dependem de rede nem do
gateway do Discord: formatação e parsing de entradas do yt-dlp. Não cobre
extract_track/resolve_playable (que chamam a internet via yt-dlp) nem os
slash commands em si.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogs.music import _entry_source, _rank_by_duration, format_duration


# --- format_duration -------------------------------------------------

def test_format_duration_none():
    assert format_duration(None) == "?"


def test_format_duration_zero():
    assert format_duration(0) == "?"


def test_format_duration_seconds_only():
    assert format_duration(65) == "1:05"


def test_format_duration_with_hours():
    assert format_duration(3725) == "1:02:05"


# --- _entry_source -----------------------------------------------------

def test_entry_source_flat_youtube_id():
    # extract_flat costuma devolver só o video ID em "url" pro YouTube.
    entry = {"url": "dQw4w9WgXcQ"}
    assert _entry_source(entry) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_entry_source_full_url_passthrough():
    entry = {"url": "https://soundcloud.com/artist/track"}
    assert _entry_source(entry) == "https://soundcloud.com/artist/track"


def test_entry_source_falls_back_to_webpage_url():
    entry = {"webpage_url": "https://example.com/watch"}
    assert _entry_source(entry) == "https://example.com/watch"


def test_entry_source_falls_back_to_id():
    entry = {"id": "abc123"}
    assert _entry_source(entry) == "abc123"


def test_entry_source_prefers_webpage_url_over_api_url():
    # Resultado de busca no SoundCloud: "url" é um endpoint interno da
    # API (api.soundcloud.com/tracks/...), webpage_url é o link público
    # de verdade — o público tem que vencer.
    entry = {
        "url": "https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A125459218",
        "webpage_url": "https://soundcloud.com/chris_brown/chris-brown-loyal-east-coast",
    }
    assert _entry_source(entry) == "https://soundcloud.com/chris_brown/chris-brown-loyal-east-coast"


# --- _rank_by_duration ---------------------------------------------------

def _choice(source, duration):
    return {"title": source, "duration": duration, "uploader": "", "source": source}


def test_rank_by_duration_no_target_keeps_original_order():
    choices = [_choice("a", 100), _choice("b", 300), _choice("c", 200)]
    assert _rank_by_duration(choices, None) == choices


def test_rank_by_duration_prefers_closest_match():
    # faixa oficial tem 200s; "extended" (300s) e "radio edit" (190s, bem
    # perto) não podem ficar na frente da versão certa.
    original = _choice("original", 200)
    extended = _choice("extended mix", 340)
    close_but_not_exact = _choice("radio edit", 190)
    choices = [extended, close_but_not_exact, original]

    ranked = _rank_by_duration(choices, target_seconds=200)

    assert ranked[0] is original
    assert ranked[1] is close_but_not_exact
    assert ranked[2] is extended


def test_rank_by_duration_unknown_duration_goes_last():
    known_far = _choice("far", 500)
    unknown = _choice("unknown", None)
    known_close = _choice("close", 201)

    ranked = _rank_by_duration([unknown, known_far, known_close], target_seconds=200)

    assert ranked[0] is known_close
    assert ranked[1] is known_far
    assert ranked[2] is unknown  # duração desconhecida nunca deveria vencer uma conhecida
