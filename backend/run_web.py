import logging
import os
import socket
import subprocess
import threading
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
# Generous: this only gates a log line, never app startup (see the
# background thread in start_bgutil_pot_provider) - node cold-starting under
# load on a shared Render instance can genuinely take longer than the 10s
# this used to be, which made "server pronto" never fire even when the
# server came up moments later.
BGUTIL_READY_TIMEOUT_SECONDS = 60.0


def _log_bgutil_server_dir_contents() -> None:
    for path in (BGUTIL_SERVER_DIR, f"{BGUTIL_SERVER_DIR}/build"):
        try:
            logger.info("bgutil pot provider: contenuto di %s: %r", path, os.listdir(path))
        except OSError as exc:
            logger.info("bgutil pot provider: impossibile leggere %s (%s)", path, exc)


def _log_bgutil_readiness(process: subprocess.Popen) -> None:
    """Runs in a background thread: logs when the sidecar actually starts
    accepting connections, or why it didn't. Informational only -
    DROPS_YTDLP_BGUTIL_HTTP_BASE_URL is set statically in the Dockerfile
    regardless of what happens here, so yt-dlp always points at
    127.0.0.1:4416; if the server never comes up, the bgutil yt-dlp plugin's
    own HTTP call there just fails at request time and it degrades without a
    PO token for that request, exactly as it always did when no provider was
    configured. This thread only makes that outcome visible in the logs
    instead of silent.
    """
    deadline = time.monotonic() + BGUTIL_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            output = process.stdout.read() if process.stdout else ""
            logger.warning(
                "bgutil pot provider: processo terminato (exit=%s) - le richieste PO token falliranno finche' non riparte. Output:\n%s",
                exit_code, output.strip() or "(nessun output)",
            )
            return
        try:
            with socket.create_connection(("127.0.0.1", BGUTIL_PORT), timeout=0.5):
                pass
        except OSError:
            time.sleep(0.5)
            continue
        logger.info("bgutil pot provider: HTTP server pronto su 127.0.0.1:%s, PO token abilitato", BGUTIL_PORT)
        return
    logger.warning(
        "bgutil pot provider: server non pronto entro %ss - le richieste PO token falliranno finche' non lo sara'",
        BGUTIL_READY_TIMEOUT_SECONDS,
    )


def start_bgutil_pot_provider() -> None:
    """Best-effort: start the bundled bgutil-ytdlp-pot-provider HTTP server so
    yt-dlp can request a free PO token against YouTube's bot-check.

    Never blocks or fails app startup: DROPS_YTDLP_BGUTIL_HTTP_BASE_URL is
    set statically in the Dockerfile (not here), so media_core.py always
    wires yt-dlp to the sidecar regardless of whether it's actually up yet -
    readiness is only checked in a background thread, purely to log it.
    """
    if not os.path.isfile(BGUTIL_SERVER_ENTRY):
        logger.info("bgutil pot provider: %s non trovato nell'immagine, il sidecar non parte", BGUTIL_SERVER_ENTRY)
        _log_bgutil_server_dir_contents()
        return
    try:
        # cwd=BGUTIL_SERVER_DIR: the server resolves its own node_modules
        # relative to where it's run from. stdout+stderr captured (not
        # inherited) so a crash's actual output can be logged instead of
        # silently vanishing.
        process = subprocess.Popen(
            ["node", BGUTIL_SERVER_ENTRY], cwd=BGUTIL_SERVER_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except OSError as exc:
        logger.warning("bgutil pot provider: avvio fallito - %s: %s, il sidecar non parte", type(exc).__name__, exc)
        return
    threading.Thread(target=_log_bgutil_readiness, args=(process,), daemon=True).start()


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
