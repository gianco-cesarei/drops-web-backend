from __future__ import annotations

from types import SimpleNamespace

import run_web


def test_failed_provider_readiness_disables_dead_http_backend(monkeypatch):
    monkeypatch.setattr(run_web.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(run_web.subprocess, "Popen", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(run_web, "_wait_bgutil_readiness", lambda _process: False)
    monkeypatch.setenv("DROPS_YTDLP_BGUTIL_HTTP_BASE_URL", "http://127.0.0.1:4416")

    assert run_web.start_bgutil_pot_provider() is False
    assert "DROPS_YTDLP_BGUTIL_HTTP_BASE_URL" not in run_web.os.environ


def test_ready_provider_keeps_http_backend(monkeypatch):
    monkeypatch.setattr(run_web.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(run_web.subprocess, "Popen", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(run_web, "_wait_bgutil_readiness", lambda _process: True)
    monkeypatch.setenv("DROPS_YTDLP_BGUTIL_HTTP_BASE_URL", "http://127.0.0.1:4416")

    assert run_web.start_bgutil_pot_provider() is True
    assert run_web.os.environ["DROPS_YTDLP_BGUTIL_HTTP_BASE_URL"] == "http://127.0.0.1:4416"
