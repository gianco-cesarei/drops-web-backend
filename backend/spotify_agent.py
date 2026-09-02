"""Import Spotify favorites as metadata and prepare authorized Drops jobs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yt_dlp
from discogs_agent import DiscogsClient
from media_core import ytdlp_cookiefile, ytdlp_extractor_args

SPOTIFY_AUTHORIZE = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
DEFAULT_SPOTIFY_CLIENT_ID = "7b99b9653fba45ae974edcd553312387"
SCOPE = "user-library-read playlist-read-private playlist-read-collaborative user-read-private"

DROPS_HOME = Path(
    os.environ.get("DROPS_STATE_DIR", str(Path.home() / ".drops"))
).expanduser()
TOKEN_FILE = DROPS_HOME / "spotify-token.json"
ACCOUNT_FILE = DROPS_HOME / "spotify-account.json"
CATALOG_FILE = DROPS_HOME / "spotify-library.json"
ARTIST_CACHE_FILE = DROPS_HOME / "spotify-artist-genres.json"
MUSIC_ROOT = Path(
    os.environ.get("DROPS_MUSIC_DIR", str(Path.home() / "Documents" / "Music Gianco"))
).expanduser()

GENRE_RULES = (
    (("italo", "hi-nrg"), "Italo Disco - Hi-NRG"),
    (("french house", "filter house"), "French Touch"),
    (("deep house",), "Deep House"),
    (("tech house",), "Tech House"),
    (("house",), "House"),
    (("synthpop", "synth-pop", "new wave", "post-punk"), "Synth-pop - Post-punk"),
    (("neo soul", "neo-soul", "soul", "r&b"), "Soul - R&B"),
    (("trip hop", "trip-hop"), "Trip-hop"),
    (("ambient",), "Ambient"),
    (("progressive rock", "psychedelic rock"), "Progressive - Psychedelic Rock"),
    (("alternative rock", "nu metal", "nu-metal"), "Alternative Rock - Nu-metal"),
    (("rock",), "Rock"),
    (("jazz", "funk"), "Jazz - Funk"),
    # --- Regole aggiunte per recuperare gli "Altri Generi" (ordine: specifico
    #     prima di generico; "electronic" resta ultimo come fallback). ---
    (("hip hop", "hip-hop", "rap", "trap", "grime"), "Hip-hop - Rap"),
    (("techno",), "Techno"),
    (("nu disco", "nu-disco", "disco"), "Disco - Nu-disco"),
    (("punk", "ska"), "Punk - Ska"),
    (("latin", "reggaeton", "ranchera", "cumbia", "salsa"), "Latin"),
    (("pop",), "Pop"),
    (("electronic", "electronica", "edm", "electroclash", "idm", "dance and electronica"), "Elettronica"),
)


class SpotifyAgentError(RuntimeError):
    pass


def normalize_track_text(value: str | None) -> str:
    """Normalize catalog identity fields without guessing metadata."""
    ascii_value = unicodedata.normalize("NFKD", (value or "").casefold()).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_value).split())


class WebSpotifyClient:
    """Single-owner Spotify OAuth client used by authenticated web routes."""

    def __init__(self, state_dir: Path, catalog_dir: Path | None = None, discogs: DiscogsClient | None = None):
        self.state_dir = state_dir
        self.token_file = state_dir / "spotify-token.json"
        self.account_file = state_dir / "spotify-account.json"
        self.auth_state_file = state_dir / "spotify-auth-state.json"
        self.catalog_dir = catalog_dir or Path(
            os.environ.get("DROPS_CATALOG_DIR", Path(__file__).parents[1] / "data" / "catalog")
        ).expanduser()
        self.discogs = discogs or DiscogsClient(state_dir)

    @property
    def redirect_uri(self) -> str:
        value = os.environ.get("SPOTIFY_REDIRECT_URI", "").strip()
        if not value:
            raise SpotifyAgentError("Configura SPOTIFY_REDIRECT_URI")
        return value

    def create_authorization(self, redirect_uri: str | None = None) -> str:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        _atomic_json(self.auth_state_file, {"state": state, "verifier": verifier, "created_at": time.time()})
        params = {
            "client_id": _client_id(),
            "response_type": "code",
            "redirect_uri": redirect_uri or self.redirect_uri,
            "scope": SCOPE,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
            "show_dialog": "true",
        }
        return f"{SPOTIFY_AUTHORIZE}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str, state: str, redirect_uri: str | None = None) -> dict[str, Any]:
        saved = _read_json(self.auth_state_file, {})
        if not saved or not secrets.compare_digest(state, saved.get("state", "")):
            raise SpotifyAgentError("Stato OAuth Spotify non valido")
        if time.time() - float(saved.get("created_at", 0)) > 600:
            raise SpotifyAgentError("Login Spotify scaduto: ripeti connessione")
        token = _request_json(SPOTIFY_TOKEN, method="POST", form={
            "client_id": _client_id(), "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri or self.redirect_uri, "code_verifier": saved["verifier"],
        })
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
        _atomic_json(self.token_file, token)
        profile = self.get("/me")
        account = _account_payload(profile)
        _atomic_json(self.account_file, account)
        self.auth_state_file.unlink(missing_ok=True)
        return account

    def _token(self) -> dict[str, Any]:
        token = _read_json(self.token_file, {})
        if not token:
            refresh = os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()
            if refresh:
                token = {"refresh_token": refresh, "expires_at": 0}
        if not token:
            raise SpotifyAgentError("Spotify non collegato")
        if time.time() >= float(token.get("expires_at", 0)):
            refresh = token.get("refresh_token") or os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()
            if not refresh:
                raise SpotifyAgentError("Sessione Spotify scaduta: ricollega account")
            renewed = _request_json(SPOTIFY_TOKEN, method="POST", form={
                "client_id": _client_id(), "grant_type": "refresh_token", "refresh_token": refresh,
            })
            renewed["refresh_token"] = renewed.get("refresh_token", refresh)
            renewed["expires_at"] = time.time() + int(renewed.get("expires_in", 3600)) - 60
            _atomic_json(self.token_file, renewed)
            token = renewed
        return token

    def get(self, path_or_url: str) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("https://") else SPOTIFY_API + path_or_url
        return _request_json(url, headers={"Authorization": f"Bearer {self._token()['access_token']}"})

    def status(self) -> dict[str, Any]:
        try:
            profile = self.get("/me")
        except SpotifyAgentError as exc:
            if "non collegato" in str(exc).lower() or "scaduta" in str(exc).lower():
                return {"connected": False, "display_name": None}
            raise
        account = _account_payload(profile)
        _atomic_json(self.account_file, account)
        return {"connected": True, "display_name": account.get("display_name") or account.get("id")}

    def _catalog_tracks(self) -> list[dict[str, Any]]:
        files = sorted(self.catalog_dir.rglob("*.json")) if self.catalog_dir.is_dir() else []
        fallback = self.state_dir / "spotify-library.json"
        if fallback.is_file():
            files.append(fallback)
        tracks: list[dict[str, Any]] = []
        for path in files:
            payload = _read_json(path, {})
            if isinstance(payload, list):
                tracks.extend(item for item in payload if isinstance(item, dict))
            elif isinstance(payload, dict):
                values = payload.get("tracks") or payload.get("items") or []
                tracks.extend(item for item in values if isinstance(item, dict))
        return tracks

    def _catalog_indexes(self) -> tuple[dict[str, dict], dict[str, dict], dict[tuple[str, str], dict]]:
        by_spotify: dict[str, dict] = {}
        by_isrc: dict[str, dict] = {}
        by_name: dict[tuple[str, str], dict] = {}
        for item in self._catalog_tracks():
            spotify_id = item.get("spotify_id") or item.get("spotify_track_id") or item.get("id")
            isrc = item.get("isrc") or (item.get("external_ids") or {}).get("isrc")
            artists = item.get("artists") or item.get("artist") or []
            if isinstance(artists, str):
                artists = [artists]
            elif isinstance(artists, dict):
                artists = [artists]
            artist_key = normalize_track_text(" ".join(str(x.get("name") if isinstance(x, dict) else x) for x in artists))
            title_key = normalize_track_text(item.get("title") or item.get("name"))
            if spotify_id:
                by_spotify[str(spotify_id)] = item
            if isrc:
                by_isrc[str(isrc).upper()] = item
            if artist_key and title_key:
                by_name[(artist_key, title_key)] = item
        return by_spotify, by_isrc, by_name

    def _album_labels(self, tracks: list[dict[str, Any]]) -> dict[str, str | None]:
        # Spotify Development Mode removed album.label and GET /albums batch.
        # Keep labels nullable; Drops catalog remains source for BPM only.
        return {}

    @staticmethod
    def _fallback_track(item: dict[str, Any]) -> dict[str, Any] | None:
        track = item.get("item") or item.get("track") or item
        if not isinstance(track, dict) or not track.get("id"):
            return None
        artists = track.get("artists") or []
        names = [artist.get("name", "") for artist in artists if isinstance(artist, dict) and artist.get("name")]
        album = track.get("album") or {}
        images = album.get("images") or []
        isrc = (track.get("external_ids") or {}).get("isrc")
        return {
            "id": track.get("id"), "title": track.get("name") or "", "artists": names,
            "album": album.get("name") or "", "label": None,
            "cover_url": images[0].get("url") if images and isinstance(images[0], dict) else None,
            "isrc": isrc, "added_at": item.get("added_at"), "duration_ms": track.get("duration_ms"),
            "bpm": None, "in_catalog": False,
        }

    def enrich_best_effort(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            return self.enrich(items, include_discogs=False)
        except Exception:
            return [fallback for item in items if (fallback := self._fallback_track(item)) is not None]

    def enrich(self, items: list[dict[str, Any]], *, include_discogs: bool = False) -> list[dict[str, Any]]:
        pairs = [(item, item.get("item") or item.get("track") or item) for item in items]
        pairs = [(item, track) for item, track in pairs if isinstance(track, dict) and track.get("id")]
        raw_tracks = [track for _, track in pairs]
        labels = self._album_labels(raw_tracks)
        by_spotify, by_isrc, by_name = self._catalog_indexes()
        enriched = []
        for item, track in pairs:
            artists = [artist.get("name", "") for artist in track.get("artists") or [] if isinstance(artist, dict) and artist.get("name")]
            isrc = (track.get("external_ids") or {}).get("isrc")
            catalog = by_spotify.get(str(track.get("id")))
            if catalog is None and isrc:
                catalog = by_isrc.get(str(isrc).upper())
            if catalog is None:
                catalog = by_name.get((normalize_track_text(" ".join(artists)), normalize_track_text(track.get("name"))))
            album = track.get("album") or {}
            images = album.get("images") or []
            bpm = catalog.get("bpm") if catalog else None
            if isinstance(bpm, str):
                try:
                    bpm = float(bpm)
                except ValueError:
                    bpm = None
            discogs_result = None
            if not labels.get(album.get("id")):
                if include_discogs:
                    discogs_result = self.discogs.enrich(" ".join(artists), track.get("name") or "", isrc=isrc)
                else:
                    discogs_result = self.discogs.cached_label(" ".join(artists), track.get("name") or "", isrc)
            label = labels.get(album.get("id")) or (discogs_result or {}).get("label")
            enriched.append({
                "id": track.get("id"), "title": track.get("name") or "", "artists": artists,
                "album": album.get("name") or "", "label": label,
                "cover_url": images[0].get("url") if images else None, "isrc": isrc,
                "added_at": item.get("added_at"), "duration_ms": track.get("duration_ms"),
                "bpm": bpm if isinstance(bpm, (int, float)) else None, "in_catalog": catalog is not None,
                "year": (discogs_result or {}).get("year"), "country": (discogs_result or {}).get("country"),
                "styles": (discogs_result or {}).get("styles", []), "catalog_no": (discogs_result or {}).get("catalog_no"),
                "discogs_url": (discogs_result or {}).get("discogs_url"),
            })
        return enriched

    def liked(self, limit: int, offset: int) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        remaining = limit
        cursor = offset
        total = 0
        while remaining:
            size = min(50, remaining)
            page = self.get(f"/me/tracks?limit={size}&offset={cursor}")
            page_items = page.get("items") or []
            total = int(page.get("total") or 0)
            items.extend(page_items)
            if len(page_items) < size:
                break
            remaining -= len(page_items)
            cursor += len(page_items)
        return {"total": total, "limit": limit, "offset": offset, "tracks": self.enrich_best_effort(items)}

    def playlists(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        next_url: str | None = "/me/playlists?limit=50"
        while next_url:
            page = self.get(next_url)
            items.extend(page.get("items") or [])
            next_url = page.get("next")
        return {"playlists": [{"id": x.get("id"), "name": x.get("name") or "Senza nome", "tracks_total": (x.get("items") or x.get("tracks") or {}).get("total", 0)} for x in items if x.get("id")]}

    def playlist_tracks(self, playlist_id: str) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        next_url: str | None = f"/playlists/{urllib.parse.quote(playlist_id, safe='')}/items?limit=50"
        total = 0
        while next_url:
            page = self.get(next_url)
            total = int(page.get("total") or total)
            items.extend(page.get("items") or [])
            next_url = page.get("next")
        return {"total": total, "tracks": self.enrich_best_effort(items)}


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    _attempt: int = 0,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(form).encode() if form else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if form:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Ritenta su errori transitori: 429 (rate limit, rispetta Retry-After) e
        # 502/503/504 (servizio momentaneamente non disponibile, es. MusicBrainz).
        # Solo se l'attesa e' breve; altrimenti rilancia l'errore.
        if exc.code in (429, 502, 503, 504) and _attempt < 5:
            if exc.code == 429:
                try:
                    wait = int(exc.headers.get("Retry-After", "2") or "2")
                except (TypeError, ValueError):
                    wait = 2
            else:
                wait = 2 * (_attempt + 1)  # backoff lineare per 5xx transitori
            if wait <= 30:
                time.sleep(wait + 1)
                return _request_json(
                    url,
                    method=method,
                    headers=headers,
                    form=form,
                    _attempt=_attempt + 1,
                )
        detail = exc.read().decode("utf-8", "replace")
        endpoint = urllib.parse.urlsplit(url).path
        raise SpotifyAgentError(
            f"Spotify HTTP {exc.code} su {endpoint}: {detail[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SpotifyAgentError(f"Spotify non raggiungibile: {exc.reason}") from exc


def _client_id() -> str:
    # Client ID OAuth pubblico: può stare nel bundle. Non è un client secret.
    value = os.environ.get("SPOTIFY_CLIENT_ID", DEFAULT_SPOTIFY_CLIENT_ID).strip()
    if not value:
        raise SpotifyAgentError("Configura SPOTIFY_CLIENT_ID")
    return value


def _redirect_uri() -> str:
    return os.environ.get(
        "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/spotify/callback"
    )


def create_authorization() -> dict[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    _atomic_json(
        DROPS_HOME / "spotify-auth-state.json",
        {"state": state, "verifier": verifier, "created_at": time.time()},
    )
    params = {
        "client_id": _client_id(),
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "scope": SCOPE,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
        "show_dialog": "true",
    }
    return {"authorization_url": f"{SPOTIFY_AUTHORIZE}?{urllib.parse.urlencode(params)}"}


def _account_payload(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": profile.get("id"),
        "display_name": profile.get("display_name"),
        "email": profile.get("email"),
        "product": profile.get("product"),
        "country": profile.get("country"),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }


def exchange_code(code: str, state: str) -> dict[str, Any]:
    saved = _read_json(DROPS_HOME / "spotify-auth-state.json", {})
    if not saved or not secrets.compare_digest(state, saved.get("state", "")):
        raise SpotifyAgentError("Stato OAuth Spotify non valido")
    if time.time() - float(saved.get("created_at", 0)) > 600:
        raise SpotifyAgentError("Login Spotify scaduto: ripeti connessione")
    token = _request_json(
        SPOTIFY_TOKEN,
        method="POST",
        form={
            "client_id": _client_id(),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "code_verifier": saved["verifier"],
        },
    )
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
    _atomic_json(TOKEN_FILE, token)
    profile = _spotify_get("/me")
    account = _account_payload(profile)
    _atomic_json(ACCOUNT_FILE, account)
    return {"connected": True, "account": account}


def _access_token() -> str:
    token = _read_json(TOKEN_FILE, {})
    if not token:
        raise SpotifyAgentError("Spotify non collegato")
    if time.time() >= float(token.get("expires_at", 0)):
        refresh = token.get("refresh_token")
        if not refresh:
            raise SpotifyAgentError("Sessione Spotify scaduta: ricollega account")
        renewed = _request_json(
            SPOTIFY_TOKEN,
            method="POST",
            form={
                "client_id": _client_id(),
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
        )
        renewed["refresh_token"] = renewed.get("refresh_token", refresh)
        renewed["expires_at"] = time.time() + int(renewed.get("expires_in", 3600)) - 60
        _atomic_json(TOKEN_FILE, renewed)
        token = renewed
    return token["access_token"]


def _spotify_get(path_or_url: str) -> dict[str, Any]:
    url = path_or_url if path_or_url.startswith("https://") else SPOTIFY_API + path_or_url
    return _request_json(url, headers={"Authorization": f"Bearer {_access_token()}"})


def explain_connection_error(exc: SpotifyAgentError) -> tuple[str, bool]:
    raw = str(exc)
    lowered = raw.lower()
    if "user is not registered" in lowered:
        return (
            "Account non autorizzato per questa app Spotify. Premi Ricollega e usa "
            "l'account inserito in User Management.",
            True,
        )
    if "invalid_grant" in lowered or "refresh token revoked" in lowered:
        return ("Sessione Spotify scaduta o revocata. Premi Ricollega.", True)
    if "spotify non collegato" in lowered or "sessione spotify scaduta" in lowered:
        return (raw, True)
    return (raw, False)


def connection_status() -> dict[str, Any]:
    """Stato OAuth sicuro per UI; non restituisce mai access/refresh token."""
    token = _read_json(TOKEN_FILE, {})
    account = _read_json(ACCOUNT_FILE, {})
    if not token:
        return {
            "connected": False,
            "needs_reconnect": True,
            "account": account or None,
            "error": "Spotify non collegato",
        }

    try:
        profile = _spotify_get("/me")
        account = _account_payload(profile)
        _atomic_json(ACCOUNT_FILE, account)
        return {
            "connected": True,
            "needs_reconnect": False,
            "account": account,
            "scope": token.get("scope", ""),
            "error": None,
        }
    except SpotifyAgentError as exc:
        message, needs_reconnect = explain_connection_error(exc)
        return {
            "connected": not needs_reconnect,
            "needs_reconnect": needs_reconnect,
            "account": account or None,
            "scope": token.get("scope", ""),
            "error": message,
        }


def export_saved_tracks(destination_dir: Path) -> dict[str, Any]:
    """Esporta metadata Liked Songs in JSON locale leggibile, mai audio Spotify."""
    tracks: list[dict[str, Any]] = []
    next_url: str | None = f"{SPOTIFY_API}/me/tracks?limit=50"
    while next_url:
        page = _spotify_get(next_url)
        for item in page.get("items", []):
            track = item.get("track") or {}
            if not track.get("id"):
                continue
            tracks.append(
                {
                    "name": track.get("name", ""),
                    "artists": [
                        artist.get("name", "") for artist in track.get("artists", [])
                    ],
                    "album": (track.get("album") or {}).get("name", ""),
                    "release_date": (track.get("album") or {}).get("release_date", ""),
                    "duration_ms": track.get("duration_ms"),
                    "popularity": track.get("popularity"),
                    "added_at": item.get("added_at"),
                    "spotify_url": (track.get("external_urls") or {}).get("spotify"),
                }
            )
        next_url = page.get("next")

    now = datetime.now(timezone.utc)
    destination_dir.mkdir(parents=True, exist_ok=True)
    output = destination_dir / f"spotify-favorites-{now.strftime('%Y-%m-%d-%H%M%S')}.json"
    _atomic_json(
        output,
        {
            "exported_at": now.isoformat(),
            "total": len(tracks),
            "tracks": tracks,
        },
    )
    return {"total": len(tracks), "path": str(output)}


def genre_folder(genres: list[str]) -> str:
    normalized = [genre.lower() for genre in genres]
    for needles, folder in GENRE_RULES:
        if any(needle in genre for genre in normalized for needle in needles):
            return folder
    return "Altri Generi"


def safe_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    return cleaned[:100] or "Senza nome"


# --- Fonte generi indipendente da Spotify: MusicBrainz ---------------------
# Nessuna autenticazione, nessuna quota; richiede solo un User-Agent e ~1 req/s.
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/artist"
MUSICBRAINZ_UA = "Drops/1.0 (organizzatore musicale personale)"
MB_THROTTLE_S = 1.1          # rispetta il rate limit MusicBrainz (~1 req/s)
MB_MAX_NEW_PER_RUN = 200     # nuove ricerche per import: tiene la richiesta breve


def _musicbrainz_tags(artist_name: str) -> list[str] | None:
    """Tag/generi di un artista da MusicBrainz.

    Ritorna una lista (anche vuota se l'artista esiste ma non ha tag), oppure
    None se la chiamata fallisce (rete/5xx): in quel caso NON va messa in cache,
    va ritentata a un import successivo.
    """
    query = urllib.parse.quote(f'artist:"{artist_name}"')
    url = f"{MUSICBRAINZ_URL}?query={query}&fmt=json&limit=1"
    try:
        data = _request_json(url, headers={"User-Agent": MUSICBRAINZ_UA})
    except SpotifyAgentError:
        return None
    artists = data.get("artists") or []
    if not artists:
        return []
    top = artists[0]
    tags = [t.get("name") for t in top.get("tags", []) if t.get("name")]
    genres = [g.get("name") for g in top.get("genres", []) if g.get("name")]
    return sorted(set(tags + genres))


def import_saved_tracks() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    next_url: str | None = f"{SPOTIFY_API}/me/tracks?limit=50"
    while next_url:
        page = _spotify_get(next_url)
        items.extend(page.get("items", []))
        next_url = page.get("next")

    # I generi arrivano da MusicBrainz (per nome artista), non piu' da Spotify:
    # niente quota, niente 429. Cache su disco per nome (lowercase): ogni import
    # riparte da dove era arrivato. Per tenere breve la richiesta si risolvono
    # al massimo MB_MAX_NEW_PER_RUN nuovi artisti a run; rilanciare per finire.
    artist_genres: dict[str, list[str]] = _read_json(ARTIST_CACHE_FILE, {})
    artist_names = sorted(
        {
            (artist.get("name") or "").strip()
            for item in items
            for artist in (item.get("track") or {}).get("artists", [])
            if (artist.get("name") or "").strip()
        }
    )
    genres_incomplete = False
    resolved_this_run = 0
    consecutive_failures = 0
    for name in artist_names:
        key = name.lower()
        if key in artist_genres:
            continue
        if resolved_this_run >= MB_MAX_NEW_PER_RUN:
            genres_incomplete = True
            break
        tags = _musicbrainz_tags(name)
        if tags is None:
            # Errore transitorio: non cachare, ritenta al prossimo import.
            genres_incomplete = True
            consecutive_failures += 1
            if consecutive_failures >= 5:
                break  # MusicBrainz probabilmente down: fermati e riprova dopo
            time.sleep(MB_THROTTLE_S)
            continue
        consecutive_failures = 0
        artist_genres[key] = tags
        resolved_this_run += 1
        time.sleep(MB_THROTTLE_S)
    _atomic_json(ARTIST_CACHE_FILE, artist_genres)

    tracks = []
    for item in items:
        track = item.get("track") or {}
        if not track.get("id"):
            continue
        artists = track.get("artists", [])
        genres = sorted(
            {
                genre
                for artist in artists
                for genre in artist_genres.get((artist.get("name") or "").strip().lower(), [])
            }
        )
        tracks.append(
            {
                "spotify_id": track["id"],
                "name": track.get("name", ""),
                "artists": [artist.get("name", "") for artist in artists],
                "album": (track.get("album") or {}).get("name", ""),
                "duration_ms": track.get("duration_ms"),
                "isrc": (track.get("external_ids") or {}).get("isrc"),
                "spotify_url": (track.get("external_urls") or {}).get("spotify"),
                "added_at": item.get("added_at"),
                "genres": genres,
                "genre_folder": genre_folder(genres),
                "candidates": [],
                "status": "pending_search",
            }
        )
    catalog = {"updated_at": time.time(), "total": len(tracks), "tracks": tracks}
    _atomic_json(CATALOG_FILE, catalog)
    resolved = sum(1 for name in artist_names if name.lower() in artist_genres)
    return {
        "total": len(tracks),
        "catalog_file": str(CATALOG_FILE),
        "artists_total": len(artist_names),
        "artists_resolved": resolved,
        "artists_resolved_this_run": resolved_this_run,
        "genre_source": "musicbrainz",
        "genres_complete": not genres_incomplete,
        "hint": (
            "Generi completi."
            if not genres_incomplete
            else f"Risolti {resolved}/{len(artist_names)} artisti. Rilancia "
            "/spotify/import per continuare (riparte dalla cache)."
        ),
    }


def get_catalog(offset: int = 0, limit: int = 100) -> dict[str, Any]:
    catalog = _read_json(CATALOG_FILE, {"tracks": []})
    tracks = catalog.get("tracks", [])
    return {
        "total": len(tracks),
        "offset": offset,
        "limit": limit,
        "tracks": tracks[offset : offset + limit],
    }


def find_track(track_id: str) -> dict[str, Any]:
    for track in _read_json(CATALOG_FILE, {"tracks": []}).get("tracks", []):
        if track.get("spotify_id") == track_id:
            return track
    raise SpotifyAgentError("Brano non trovato nel catalogo importato")


def search_candidates(track_id: str, limit: int = 5) -> dict[str, Any]:
    catalog = _read_json(CATALOG_FILE, {"tracks": []})
    track = next(
        (item for item in catalog.get("tracks", []) if item.get("spotify_id") == track_id),
        None,
    )
    if not track:
        raise SpotifyAgentError("Brano non trovato nel catalogo importato")
    query = f"{' '.join(track['artists'])} {track['name']} official audio"
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "extractor_args": ytdlp_extractor_args(),
    }
    cookies = ytdlp_cookiefile()
    if cookies:
        options["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(options) as ydl:
        result = ydl.extract_info(
            f"ytsearch{max(1, min(limit, 10))}:{query}", download=False
        )
    candidates = [
        {
            "url": entry.get("webpage_url") or entry.get("url"),
            "title": entry.get("title"),
            "channel": entry.get("channel") or entry.get("uploader"),
            "duration": entry.get("duration"),
        }
        for entry in result.get("entries", [])
        if entry
    ]
    track["candidates"] = candidates
    track["status"] = "awaiting_approval"
    _atomic_json(CATALOG_FILE, catalog)
    return {"track": track, "candidates": candidates}


def approved_download_context(track_id: str, url: str) -> dict[str, Any]:
    track = find_track(track_id)
    if url not in {candidate.get("url") for candidate in track.get("candidates", [])}:
        raise SpotifyAgentError("URL non presente tra candidati verificati")
    return {
        "spotify_id": track_id,
        "isrc": track.get("isrc"),
        "artists": track["artists"],
        "name": track["name"],
        "genre_folder": safe_component(track["genre_folder"]),
        "target_dir": str(MUSIC_ROOT / safe_component(track["genre_folder"])),
    }
