"""Best-effort Discogs enrichment for Drops music metadata."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DISCOGS_API = "https://api.discogs.com"
DEFAULT_USER_AGENT = "Drops/1.0 +https://drops.giancarlocesarei.workers.dev"
logger = logging.getLogger("drops.discogs")


class DiscogsAgentError(RuntimeError):
    pass


def _normalize_key(value: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).split())


class DiscogsClient:
    """Authenticated Discogs client with disk cache and 60 req/min pacing."""

    def __init__(self, state_dir: Path, cache_dir: Path | None = None):
        self.token = os.environ.get("DISCOGS_TOKEN", "").strip()
        self.user_agent = os.environ.get("DISCOGS_USER_AGENT", DEFAULT_USER_AGENT).strip()
        self.cache_dir = cache_dir or state_dir / "discogs-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.brain_file = state_dir / "discogs-brain.json"
        self.label_cache_file = state_dir / "discogs-labels.json"
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._hits = 0
        self._misses = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def log_startup_status(self) -> None:
        """Log token presence and, if present, whether it actually authenticates."""
        if not self.token:
            logger.warning("discogs startup: DISCOGS_TOKEN assente, enrichment disabilitato")
            return
        try:
            self._get("/database/search", {"q": "test", "type": "release", "per_page": "1"})
            logger.info("discogs startup: DISCOGS_TOKEN presente e valido (query di test riuscita)")
        except DiscogsAgentError as exc:
            logger.error("discogs startup: DISCOGS_TOKEN presente ma query di test fallita: %s", exc)

    def _label_cache(self) -> dict[str, Any]:
        try:
            return json.loads(self.label_cache_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _label_cache_key(artist: str, title: str, isrc: str | None) -> str:
        if isrc and isrc.strip():
            return f"isrc:{isrc.strip().upper()}"
        return f"at:{_normalize_key(artist)}|{_normalize_key(title)}"

    def cached_label(self, artist: str, title: str, isrc: str | None = None) -> dict[str, Any] | None:
        """Disk-only lookup (no network) for a label already discovered by a prior enrich() call."""
        result = self._label_cache().get(self._label_cache_key(artist, title, isrc))
        if result:
            self._hits += 1
        else:
            self._misses += 1
        logger.debug("discogs label cache hits=%s misses=%s", self._hits, self._misses)
        return result

    def _store_label(self, artist: str, title: str, isrc: str | None, enriched: dict[str, Any]) -> None:
        with self._lock:
            cache = self._label_cache()
            cache[self._label_cache_key(artist, title, isrc)] = enriched
            temp = self.label_cache_file.with_suffix(".tmp")
            temp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.label_cache_file)

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.token:
            raise DiscogsAgentError("DISCOGS_TOKEN non configurato")
        query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value})
        url = f"{DISCOGS_API}{path}" + (f"?{query}" if query else "")
        cache_path = self._cache_path(url)
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        with self._lock:
            wait = 1.0 - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
        request = urllib.request.Request(url, headers={"Authorization": f"Discogs token={self.token}", "User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(2)
            elif exc.code in (401, 403):
                logger.error("discogs request failed path=%s status=%s: DISCOGS_TOKEN invalido o rifiutato", path, exc.code)
            else:
                logger.warning("discogs request failed path=%s status=%s", path, exc.code)
            raise DiscogsAgentError(f"Discogs HTTP {exc.code}") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            logger.warning("discogs request unreachable path=%s error=%s", path, exc)
            raise DiscogsAgentError("Discogs non raggiungibile") from exc
        except Exception as exc:
            # Mirrors media_core._oembed's fix for the same bug class: a stalled
            # connection can raise a bare TimeoutError (not URLError), and other
            # http.client exceptions aren't OSError subclasses either, so urllib
            # doesn't wrap them - they'd otherwise escape _get() uncaught.
            logger.warning("discogs request fallita in modo inatteso path=%s error=%r", path, str(exc)[:200])
            raise DiscogsAgentError("Discogs request fallita in modo inatteso") from exc
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload

    @staticmethod
    def _release_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        if not payload or not payload.get("id"):
            return None
        labels = [item.get("name") for item in payload.get("labels") or [] if item.get("name")]
        artists = [item.get("name") for item in payload.get("artists") or [] if item.get("name")]
        styles = sorted(set((payload.get("styles") or []) + (payload.get("genres") or [])))
        images = payload.get("images") or []
        cover = next((image.get("uri") for image in images if image.get("type") == "primary" and image.get("uri")), None)
        if not cover and images:
            cover = images[0].get("uri")
        return {
            "label": labels[0] if labels else None,
            "labels": labels,
            "catalog_no": next((item.get("catno") for item in payload.get("labels") or [] if item.get("catno")), None),
            "year": payload.get("year"),
            "country": payload.get("country"),
            "styles": styles,
            "artists": artists,
            "cover_url": cover,
            "discogs_url": payload.get("uri") or f"https://www.discogs.com/release/{payload['id']}",
            "release_id": payload.get("id"),
        }

    def _record_brain(self, result: dict[str, Any]) -> None:
        if not result or not result.get("label"):
            return
        try:
            current = json.loads(self.brain_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            current = {"labels": [], "artists": [], "releases": [], "relations": []}
        label = result["label"]
        current["labels"] = sorted(set(current.get("labels", []) + [label]))
        current["artists"] = sorted(set(current.get("artists", []) + result.get("artists", [])))
        release = {key: result.get(key) for key in ("release_id", "label", "catalog_no", "year", "country", "styles", "discogs_url")}
        current["releases"] = [item for item in current.get("releases", []) if item.get("release_id") != release["release_id"]] + [release]
        current["relations"] = [item for item in current.get("relations", []) if item.get("release_id") != release["release_id"]]
        current["relations"] += [{"release_id": release["release_id"], "label": label, "artist": artist, "relation": "releases_on", "source": "discogs"} for artist in result.get("artists", [])]
        temp = self.brain_file.with_suffix(".tmp")
        temp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.brain_file)

    def enrich(self, artist: str, title: str, isrc: str | None = None, catalog_no: str | None = None, barcode: str | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            logger.warning("discogs enrich skipped artist=%r title=%r: DISCOGS_TOKEN assente", artist, title)
            return None
        if not artist.strip() or not title.strip():
            return None
        params = {"artist": artist, "title": title, "type": "release", "per_page": "5"}
        if catalog_no:
            params["catno"] = catalog_no
        if barcode:
            params["barcode"] = barcode
        try:
            search = self._get("/database/search", params)
            result = next((item for item in search.get("results") or [] if item.get("id")), None)
            if not result:
                self._misses += 1
                logger.info("discogs enrich miss artist=%r title=%r hits=%s misses=%s", artist, title, self._hits, self._misses)
                return None
            payload = self._get(f"/releases/{result['id']}")
            enriched = self._release_payload(payload)
            if enriched:
                enriched["isrc"] = isrc
                self._record_brain(enriched)
                self._store_label(artist, title, isrc, enriched)
                self._hits += 1
                logger.info("discogs enrich hit artist=%r title=%r label=%r hits=%s misses=%s", artist, title, enriched.get("label"), self._hits, self._misses)
            return enriched
        except DiscogsAgentError as exc:
            logger.warning("discogs enrich failed artist=%r title=%r error=%s", artist, title, exc)
            return None
