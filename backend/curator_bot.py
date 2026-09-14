"""
Underground Club Curator Bot for Drops Web App.
Powered by Google Gemini (Free Tier), configured with Drops Underground Sound Brain.
Zero-memory session management: completely stateless on server.
"""

import os
import json
import ssl
import urllib.request
import urllib.error
from typing import List, Dict, Any

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_NAME = "gemini-3.6-flash"

SSL_CTX = ssl._create_unverified_context()

from pathlib import Path

# Load Brain system instructions
DEFAULT_LOCAL_PATH = Path("/Users/gianco/Documents/Claude/Projects/DropAgent/UNDERGROUND_CLUB_BRAIN.md")
CONTAINER_PATH = Path(__file__).parent / "underground_club_brain.md"
BRAIN_PATH = Path(os.environ.get("DROPS_BRAIN_PATH", CONTAINER_PATH if CONTAINER_PATH.exists() else DEFAULT_LOCAL_PATH))

try:
    with open(BRAIN_PATH, "r", encoding="utf-8") as f:
        BRAIN_MANIFESTO = f.read()
except Exception:
    if CONTAINER_PATH.exists():
        with open(CONTAINER_PATH, "r", encoding="utf-8") as f:
            BRAIN_MANIFESTO = f.read()
    else:
        BRAIN_MANIFESTO = "Sei il curatore underground di Drops. Aiuti a scegliere tracce di clubbing di nicchia."

SYSTEM_INSTRUCTION = f"""
Sei 'Drops Curator', l'intelligenza artificiale e mentore musicale underground di Drops.
Il tuo compito è guidare digger e DJ nella selezione di musica elettronica di nicchia e di altissimo livello artistico.

CANONE E CONOSCENZA FONDAMENTALE:
{BRAIN_MANIFESTO}

COMPORTAMENTO E TONO:
1. DIRETTO, CONCISO, MINIMALISTA. Zero chiacchiere promozionali, zero cliché commerciali. Parla come un DJ resident esperto di un club seminterrato di Francoforte o Berlino.
2. RADAR TREND INIZIALE: Se l'utente saluta o inizia la sessione, accoglilo brevemente e mostra subito 3 release calde/sold-out da tenere d'occhio (es. Bosconi Bosco058/059, Telum, Pleasure Club).
3. CURATOR INTERVIEW: Se l'utente ti chiede una traccia o una raccomandazione, NON sparare subito nomi a caso. Fagli 2 o massimo 3 domande a scelta multipla chiuse per inquadrare il suo bisogno:
   - Quale slot/fase del set stai preparando? (es. [1] Warm Up, [2-3B] Holding, [2-3A] Tension Bridge, [3] Peak Starter, [4] Plateau, [5] Outro)
   - Che timbro ritmico cerchi? (es. Rolling bass continuo, Acid/Tensione, Drum tool percussivo, Vocal ipnotico/bizzarro)
4. RACCOMANDAZIONE PROFONDA: Una volta capite le risposte, consiglia 2 tracce spiegando l'incastro armonico (Camelot Wheel) e la funzione acustica sulla pista.
"""

def chat_with_curator(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Sends chat history to Gemini with injected Underground Brain instructions.
    Stateless: takes full history array and returns assistant response.
    """
    contents = []
    for msg in messages:
        role = "user" if msg.get("role") == "user" else "model"
        contents.append({
            "role": role,
            "parts": [{"text": msg.get("content", "")}]
        })

    payload = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_INSTRUCTION}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 800
        }
    }

    api_key = os.environ.get("GEMINI_API_KEY", GEMINI_API_KEY)
    if not api_key:
        return {"success": False, "reply": "Curator non disponibile: GEMINI_API_KEY non configurata sul server."}

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={api_key}"
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                return {"success": True, "reply": text}
            return {"success": False, "reply": "Nessuna risposta dal curatore."}
    except Exception as e:
        return {"success": False, "reply": f"Errore di connessione al curatore: {str(e)}"}

if __name__ == "__main__":
    print("Testing curator bot...")
    res = chat_with_curator([{"role": "user", "content": "Ciao, cosa mi consigli per stasera?"}])
    print("Risposta:\n", res["reply"])
