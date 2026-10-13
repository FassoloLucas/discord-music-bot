import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.spotify import SpotifyResolver


def test_parse_url_track():
    kind, spotify_id = SpotifyResolver.parse_url(
        "https://open.spotify.com/track/6habFhsOp2NvshLv26DqMb?si=abc"
    )
    assert kind == "track"
    assert spotify_id == "6habFhsOp2NvshLv26DqMb"


def test_parse_url_album():
    kind, spotify_id = SpotifyResolver.parse_url("https://open.spotify.com/album/1A2B3C4D5E6F7G8H9I0J1K")
    assert kind == "album"


def test_parse_url_playlist_with_intl_prefix():
    # o Spotify às vezes injeta um prefixo de idioma no link (intl-pt/...)
    kind, spotify_id = SpotifyResolver.parse_url(
        "https://open.spotify.com/intl-pt/playlist/37i9dQZF1DXcBWIGoYBM5M"
    )
    assert kind == "playlist"
    assert spotify_id == "37i9dQZF1DXcBWIGoYBM5M"


def test_parse_url_unrecognized_returns_none():
    assert SpotifyResolver.parse_url("https://example.com/not-spotify") is None


def test_track_to_item_with_artists():
    item = SpotifyResolver._track_to_item(
        {"name": "Bohemian Rhapsody", "artists": [{"name": "Queen"}]}
    )
    assert item.title == "Queen - Bohemian Rhapsody"
    assert item.query == "Queen - Bohemian Rhapsody audio"


def test_track_to_item_multiple_artists():
    item = SpotifyResolver._track_to_item(
        {"name": "Faixa", "artists": [{"name": "A"}, {"name": "B"}]}
    )
    assert item.title == "A, B - Faixa"


def test_track_to_item_without_artists_falls_back_to_title_only():
    item = SpotifyResolver._track_to_item({"name": "Sem Artista", "artists": []})
    assert item.title == "Sem Artista"


def test_track_to_item_captures_duration_and_album():
    item = SpotifyResolver._track_to_item(
        {
            "name": "Loyal",
            "artists": [{"name": "Chris Brown"}],
            "duration_ms": 264000,
            "album": {"name": "X (Expanded Edition)"},
        }
    )
    assert item.duration_ms == 264000
    assert item.album == "X (Expanded Edition)"


def test_track_to_item_missing_duration_and_album_defaults_to_none():
    # Faixas vindas de /albums/{id}/tracks não trazem "album" (já estão
    # dentro de um álbum), e algumas respostas restritas não trazem
    # duration_ms — não pode quebrar.
    item = SpotifyResolver._track_to_item({"name": "Faixa", "artists": []})
    assert item.duration_ms is None
    assert item.album is None
