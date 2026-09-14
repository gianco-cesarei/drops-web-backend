# DROPS CURATOR BOT — HANDOVER & SPECIFICATION BRIEFING
**Da**: DropAgent (Agente di Digging & Archiviazione Musicale)  
**A**: Agente Principale del Progetto Drops (Senior Full-Stack & UI)  
**Priorità**: ALTA / ARCHITETTURA PRODOTTO & DESIGN SYSTEM

---

## 1. Contesto & Lavoro Svolto da DropAgent
In qualità di agente delegato per la musica e l'ecosistema Drops, abbiamo completato la prima fase di ideazione e prototipazione del **Drops Curator [AI]**:
- **Backend**: Implementato `backend/curator_bot.py` e route `POST /api/v1/curator/chat` in `backend/web_app.py`, con prompt calibrato sulla cultura underground (`backend/underground_club_brain.md`).
- **Frontend**: Creato `src/components/CuratorDrawer.tsx` montato in `PublicLayout.astro` e `App.tsx`.
- **Modello Attuale**: Google Gemini (`gemini-3.6-flash`) via API key su variabile d'ambiente Render `GEMINI_API_KEY`.

---

## 2. Il Problema Tecnico Riscontrato (Blocchi / Timeout)
L'attuale implementazione sincrona su Render (istanza Free Tier di backend) presenta limiti strutturali:
1. **Latenza di Rete & Timeout**: Le chiamate HTTP sincrone verso Gemini impiegano 8-12 secondi. L'istanza Render free blocca il thread o rischia di andare in timeout rispetto al browser / Cloudflare Worker proxy.
2. **Troncamento Token**: Impostare `maxOutputTokens` statico rischia di tagliare le risposte a metà.

---

## 3. Direttive Architetturali per Drops Senior

### ⚠️ REGOLA ZERO — SEGRETEZZA ASSOLUTA DELLE FASI (CLASSIFIED / PROPRIETARY)
> **LE FASI DEL SET SONO IL KNOW-HOW SEGRETO INTERNO DI DROPS.**  
> - **MAI** mostrare nell'interfaccia pubblica pulsanti, chip o etichette con i codici interni (`[1] Warm Up`, `[2-3B] Holding & Handover`, `[2-3A] Tension Bridge`, `[3] Peak Starter`, `[4] Plateau Mentale`, `[5] Outro`).  
> - Quei codici appartengono solo alla nostra tassonomia interna per l'analisi e la chiavetta CDJ.  
> - Il Curatore AI può usarli internamente nei suoi ragionamenti o porre domande fluide e colloquiali (es. "A che punto della serata suoni? Inizio, cambio con il guest, ora di punta o chiusura?"), ma **NON deve mai spiattellarli come menu tecnici o bottoni all'utente pubblico**.

### 🛠️ OTTIMIZZAZIONI TECNICHE DA IMPLEMENTARE:
1. **Streaming SSE (Server-Sent Events) o Edge AI**:
   - Invece di una chiamata HTTP sincrona `POST` che attende tutto il testo prima di rispondere, implementare lo streaming token-by-token (SSE su FastAPI `StreamingResponse` o WebSocket).
   - *Alternativa d'élite raccomandata*: Spostare l'inferenza direttamente nel Cloudflare Worker di frontend usando **Cloudflare Workers AI** (modelli Llama 3 / Mistral già disponibili a edge senza latenza verso Render).
2. **Sessione Rigorosamente Stateless / Zero-Memory**:
   - Mantenere la regola aurea: nessuna memoria della chat su database o server. Quando l'utente ricarica la pagina o chiude il browser tab, la sessione svanisce.
3. **Radar di Benvenuto**:
   - All'apertura del drawer, il bot deve sempre accogliere l'utente fornendo subito 3 release underground calde/sold-out (es. Telum, Pleasure Club, Bosconi Records, Cabaret, Time Passages).
4. **Intervista Selettiva (Massimo 2 domande)**:
   - Se l'utente chiede consigli, non sparare tracce a caso ma porre 2 domande colloquiali per inquadrare slot temporale e risposta acustica, per poi consigliare 2 tracce dettagliando incastro armonico (Camelot) e impatto sonoro.

---

## 4. Identità Visiva Ufficiale: *The Vinyl Head* & Sticker Pack

L'identità del Curatore è stata definita e approvata:
- **Nome / Concetto**: **The Vinyl Head** (Mascotte non-umanoide, gender-fluid, cera underground).
- **Anatomia**: Testa a vinile 12" nero lucido con microsolchi, occhi a LED ambrati caldi ed espressivi, hoodie streetwear oversize nero, cuffia vintage da DJ.
- **Riferimento di Stile & Lore**: Vedi `VINYLHEAD_CHARACTER_BIBLE.md` nella root del repository.

### 🎨 Asset Disponibili in `public/stickers/`:
1. `public/stickers/vinylhead_eureka.png`:
   - PNG trasparente fustellato (die-cut outline bianca).
   - Mostra The Vinyl Head che solleva trionfante un vinile dorato (*"Ho trovato la perla!"*).

### 📋 Roadmap della Collezione 10 Sticker per la Chat:
| # | Codice Sticker | Posa & Significato | Trigger nella Chat |
|---|---|---|---|
| **01** | `STK_EUREKA` | Tiene in alto il vinile con riflesso dorato | Quando consiglia o estrae una traccia perfetta. |
| **02** | `STK_HUSH` | Dito alla bocca *"Shhh... secret white label ID"* | Tracce rare, unreleased, test-pressings. |
| **03** | `STK_DIGGING` | Crate digging, vinili che scorrono nei box | Inizio ricerca o spiegazione di etichette underground. |
| **04** | `STK_CUE_BOOTH` | Cuffia all'orecchio, mano sul mixer | Analisi tecnica di transizione o mix in consolle. |
| **05** | `STK_CAMELOT` | Medita sulla ruota armonica Camelot | Quando spiega incastri di tonalità (+1, -1, relative). |
| **06** | `STK_APPROVED` | Thumbs up / Bomba da club approvata | Valutazione positiva di una traccia chiesta dall'utente. |
| **07** | `STK_AFTERHOURS` | Espresso fumante su flight case alle 6 AM | Chiusura set, outro o decompression session. |
| **08** | `STK_AVATAR_BADGE` | Icona circolare minimal Vinyl Head | Avatar dell'header del `CuratorDrawer`. |
| **09** | `STK_NEEDLE_DROP` | Testina che poggia sui solchi | Partenza traccia o transizione a climax. |
| **10** | `STK_GROOVING` | Head-nodding a tempo con occhi socchiusi | Risposte su groove ipnotici o minimal funk. |

### 🚀 Compito per Drops Senior:
1. Montare `vinylhead_eureka.png` (o avatar circolare) nell'header di `CuratorDrawer.tsx`.
2. Mostrare lo sticker a fine messaggio quando il bot consiglia una perla.
3. Completare/integrare gli altri sticker del pack via via che vengono rilasciati.
