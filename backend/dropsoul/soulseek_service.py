"""
DropSoul Service for Drops Web Backend.
Coordinates Soulseek P2P searches, candidate scoring, and quality verification integration.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from .quality_verifier import AudioQualityVerifier, QualityReport, QualityVerdict

logger = logging.getLogger("dropsoul.service")


class DropSoulService:
    def __init__(
        self,
        slskd_url: Optional[str] = None,
        slskd_api_key: Optional[str] = None,
        verifier: Optional[AudioQualityVerifier] = None,
    ):
        self.slskd_url = slskd_url or os.environ.get("SLSKD_URL", "http://localhost:5030")
        self.slskd_api_key = slskd_api_key or os.environ.get("SLSKD_API_KEY", "")
        self.verifier = verifier or AudioQualityVerifier()

    def analyze_audio_file(self, file_path: str) -> QualityReport:
        """Runs the FFT spectral analyzer on any downloaded or local audio file."""
        return self.verifier.analyze(file_path)

    def is_slskd_connected(self) -> bool:
        """Checks if local or container slskd node is reachable."""
        import urllib.request
        try:
            req = urllib.request.Request(f"{self.slskd_url.rstrip('/')}/api/v0/application")
            if self.slskd_api_key:
                req.add_header("X-API-Key", self.slskd_api_key)
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False
