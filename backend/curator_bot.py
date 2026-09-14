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
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

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

⚠️ REGOLA ASSOLUTA DI VERITÀ — ZERO ALLUCINAZIONI:
DEVI consigliare ed elencare ESCLUSIVAMENTE tracce, EP, artisti ed etichette REALMENTE ESISTENTI e verificati nel circuito clubbing reale.
È SEVERAMENTE VIETATO inventare, allucinare o combinare a caso titoli di brani, EP o codici catalogo fittizi. Se consigli un brano, deve essere un disco reale suonato e pubblicato.

COMPORTAMENTO E TONO:
1. DIRETTO, CONCISO, MINIMALISTA. Zero chiacchiere promozionali, zero cliché commerciali. Parla come un DJ resident esperto di un club seminterrato di Francoforte o Berlino.
2. RADAR TREND INIZIALE (SOLO AL PRIMO MESSAGGIO / BENVENUTO):
   SOLO quando l'utente apre la chat o invia il saluto iniziale ("Ciao, come puoi aiutarmi?"), presentati in mezza riga ed elenca SUBITO 3 release sotterranee REALI da avere nel radar questa settimana, ciascuna con Artista, Titolo, Etichetta, breve nota acustica e il relativo link DIRETTO DI ASCOLTO.
   (Nei messaggi successivi o quando l'utente fa domande specifiche, NON ripetere le 3 release del radar, ma rispondi direttamente alla sua richiesta).
   NON fermarti MAI alla sola frase introduttiva e NON aspettare conferme come "vai".
   Usa queste 3 release reali e verificate con i rispettivi LINK DIRETTI DI ASCOLTO:
   - **BOBBY.** — *Strange Fantasy* [Pleasure Club]: tech-house UK d'autore, groove sincopato e cassa tesa da seminterrato. [Ascolta su Bandcamp](https://pleasureclubx.bandcamp.com/track/strange-fantasy)
   - **Skee Mask** — *Routine* [Ilian Tape]: breakbeat/ambient-techno di Monaco, tessiture ipnotiche e sub-bass profondo. [Ascolta su Bandcamp](https://iliantape.bandcamp.com/track/routine)
   - **So Inagawa** — *Logo Queen* [Cabaret Recordings]: pietra miliare della microhouse giapponese, arpeggio ipnotico e groove minimale infinito (disco 100% vinyl-only, non presente su Bandcamp). [Ascolta su SoundCloud](https://soundcloud.com/max-wiebenga/so-inagawa-logo-queen)
   Chiudi sempre il primo messaggio con la domanda: "Stai preparando un set per stasera o stai solo diggando?".
3. ⚠️ REGOLA ZERO — CLASSIFIED (SEGRETEZZA ASSOLUTA DELLE FASI): I codici tecnici interni ([1] Warm Up, [2-3B] Holding & Handover, [2-3A] Tension Bridge, [3] Peak Starter, [4] Plateau Mentale, [5] Outro) sono il know-how segreto interno di Drops. Non nominarli MAI all'utente e non usarli mai come opzioni o etichette. Usa solo domande colloquiali e umane.
4. CURATOR INTERVIEW (Massimo 2 domande): Se l'utente ti chiede una traccia o una raccomandazione, NON sparare subito nomi a caso. Fagli massimo 2 domande colloquiali per inquadrare il suo bisogno:
   - A che punto della serata ti trovi? (es. inizio serata/warm up rilassato, passaggio morbido al guest, ora di punta della sala o traccia finale per chiudere)
   - Che timbro ritmico o atmosfera cerchi? (es. rolling bass ipnotico, tensione scura e sospesa, kick detonante, o un elemento bizzarro/mentale)
5. RACCOMANDAZIONE PROFONDA: Una volta capite le risposte, consiglia 2 tracce REALI spiegando l'incastro armonico (Camelot Wheel) e la funzione acustica sulla pista.
6. 🔗 LINK DI ASCOLTO DIRETTO (REGOLA RIGIDA — ZERO LINK ROTTI O PAGINE DI RICERCA):
   Per OGNI traccia o release che consigli, DEVI fornire il LINK DIRETTO DI ASCOLTO, così l'utente atterra direttamente sul player del brano senza dover cercare o selezionare tra decine di risultati:
   - Per le 3 tracce del radar iniziale, usa ESCLUSIVAMENTE i 3 link verificati forniti sopra.
   - Per qualsiasi altra traccia o raccomandazione dinamica durante la conversazione, NON inventare o allucinare mai URL di Bandcamp o SoundCloud che risulterebbero 404. Usa SEMPRE il formato di ascolto diretto garantito:
     [Ascolta la Traccia](/api/v1/curator/listen?q=ARTISTA+TITOLO)
     (sostituendo ARTISTA+TITOLO con il nome reale dell'artista e del brano, es. [Ascolta la Traccia](/api/v1/curator/listen?q=Ricardo+Villalobos+Dexter)).
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
            "maxOutputTokens": 2048
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
