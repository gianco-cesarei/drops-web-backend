import logging
import os
import socket
import subprocess
import time

import uvicorn

# uvicorn only configures its own "uvicorn*" loggers; without this, the app's
# "drops.*" loggers inherit the root logger's default WARNING level and INFO
# diagnostics (session/discogs status) never reach Render's log viewer.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("drops.run_web")

# Path baked into the Docker image (see Dockerfile) when the bgutil-ytdlp-pot-provider
# HTTP server is bundled. Absent in local/dev runs - that's fine, this whole step
# is best-effort.
BGUTIL_SERVER_DIR = "/opt/bgutil-server"
BGUTIL_SERVER_ENTRY = f"{BGUTIL_SERVER_DIR}/build/main.js"
BGUTIL_PORT = 4416
# Bounded cold-start gate: API waits briefly for provider, then starts anyway.
BGUTIL_READY_TIMEOUT_SECONDS = 15.0


def _log_bgutil_server_dir_contents() -> None:
    for path in (BGUTIL_SERVER_DIR, f"{BGUTIL_SERVER_DIR}/build"):
        try:
            logger.info("bgutil pot provider: contenuto di %s: %r", path, os.listdir(path))
        except OSError as exc:
            logger.info("bgutil pot provider: impossibile leggere %s (%s)", path, exc)


def _wait_bgutil_readiness(process: subprocess.Popen) -> bool:
    """Wait until sidecar accepts connections, or report bounded failure."""
    deadline = time.monotonic() + BGUTIL_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            logger.warning(
                "bgutil pot provider: processo terminato (exit=%s) - richieste PO token disabilitate",
                exit_code,
            )
            return False
        try:
            with socket.create_connection(("127.0.0.1", BGUTIL_PORT), timeout=0.5):
                pass
        except OSError:
            time.sleep(0.5)
            continue
        logger.info("bgutil pot provider: HTTP server pronto su 127.0.0.1:%s, PO token abilitato", BGUTIL_PORT)
        return True
    logger.warning(
        "bgutil pot provider: server non pronto entro %ss - le richieste PO token falliranno finche' non lo sara'",
        BGUTIL_READY_TIMEOUT_SECONDS,
    )
    return False


def start_bgutil_pot_provider() -> bool:
    """Best-effort: start the bundled bgutil-ytdlp-pot-provider HTTP server so
    yt-dlp can request a free PO token against YouTube's bot-check.

    Waits up to BGUTIL_READY_TIMEOUT_SECONDS, but never fails app startup.
    """
    if not os.path.isfile(BGUTIL_SERVER_ENTRY):
        logger.info("bgutil pot provider: %s non trovato nell'immagine, il sidecar non parte", BGUTIL_SERVER_ENTRY)
        _log_bgutil_server_dir_contents()
        return False
    try:
        # cwd lets server resolve node_modules. DEVNULL prevents an unread
        # PIPE buffer from blocking long-lived sidecar process.
        process = subprocess.Popen(
            ["node", BGUTIL_SERVER_ENTRY], cwd=BGUTIL_SERVER_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, text=True,
        )
    except OSError as exc:
        logger.warning("bgutil pot provider: avvio fallito - %s: %s, il sidecar non parte", type(exc).__name__, exc)
        return False
    # Cold-start gate: avoid serving first YouTube request before PO provider.
    # Bounded wait; API still starts when provider unavailable.
    ready = _wait_bgutil_readiness(process)
    if not ready:
        os.environ.pop("DROPS_YTDLP_BGUTIL_HTTP_BASE_URL", None)
    return ready


if __name__ == "__main__":
    start_bgutil_pot_provider()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "web_app:create_app",
        factory=True,
        host="0.0.0.0",
        port=port,
        workers=1,
    )
