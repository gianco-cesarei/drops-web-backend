# DROPS CURATOR BOT — HANDOVER & SPECIFICATION BRIEFING
**Da**: DropAgent (Agente di Digging & Archiviazione Musicale)  
**A**: Agente Principale del Progetto Drops (Backend & Frontend)  
**Priorità**: ALTA / ARCHITETTURA PRODOTTO

---

## 1. Contesto & Lavoro Svolto da DropAgent
In qualità di agente delegato per la musica e l'ecosistema Drops, abbiamo integrato il primo prototipo funzionale del **Drops Curator [AI]**:
- **Backend**: Implementato `backend/curator_bot.py` e route `POST /api/v1/curator/chat` in `backend/web_app.py`, con prompt basato su `backend/underground_club_brain.md`.
- **Frontend**: Creato `src/components/CuratorDrawer.tsx` montato in `PublicLayout.astro` e `App.tsx`.
- **Modello Attuale**: Google Gemini (`gemini-3.6-flash`) via API key su variabile d'ambiente Render `GEMINI_API_KEY`.

---

## 2. Il Problema Tecnico Riscontrato (Blocchi / Timeout)
L'attuale implementazione sincrona su Render (istanza Free Tier di backend) presenta limiti strutturali:
1. **Latenza di Rete & Timeout**: Le chiamate HTTP sincrone verso Gemini impiegano 8-12 secondi. L'istanza Render free blocca il thread o rischia di andare in timeout rispetto al browser / Cloudflare Worker proxy.
2. **Troncamento Token**: Impostare `maxOutputTokens` statico rischia di tagliare le risposte a metà.

---

## 3. Direttive Architetturali per il Progetto Principale Drops

### ⚠️ REGOLA ZERO — SEGRETEZZA ASSOLUTA DELLE FASI (CLASSIFIED / PROPRIETARY)
> **LE FASI DEL SET SONO IL KNOW-HOW SEGRETO INTERNO DI DROPS.**  
> - **MAI** mostrare nell'interfaccia pubblica pulsanti, chip o etichette con i codici interni (`[1] Warm Up`, `[2-3B] Holding & Handover`, `[2-3A] Tension Bridge`, `[3] Peak Starter`, `[4] Plateau Mentale`, `[5] Outro`).  
> - Quei codici appartengono solo alla nostra tassonomia interna per l'analisi e la chiavetta CDJ.  
> - Il Curatore AI può usarli internamente nei suoi ragionamenti o porre domande fluide e colloquiali (es. "A che punto della serata suoni? Inizio, cambio con il guest, ora di punta o chiusura?"), ma **NON deve mai spiattellarli come menu tecnici o bottoni all'utente pubblico**.

### 🛠️ OTTIMIZZAZIONI TECNICHE DA IMPLEMENTARE NEL PROGETTO PRINCIPALE:
1. **Streaming SSE (Server-Sent Events) o Edge AI**:
   - Invece di una chiamata HTTP sincrona `POST` che attende tutto il testo prima di rispondere, implementare lo streaming token-by-token (SSE su FastAPI `StreamingResponse` o WebSocket).
   - *Alternativa d'élite*: Spostare l'inferenza direttamente nel Cloudflare Worker di frontend usando **Cloudflare Workers AI** (modelli Llama 3 / Mistral già disponibili a edge senza latenza verso Render).
2. **Sessione Rigorosamente Stateless / Zero-Memory**:
   - Mantenere la regola aurea: nessuna memoria della chat su database o server. Quando l'utente ricarica la pagina o chiude il browser tab, la sessione svanisce.
3. **Radar di Benvenuto**:
   - All'apertura del drawer, il bot deve sempre accogliere l'utente fornendo subito 3 release underground calde/sold-out (es. Telum, Pleasure Club, Bosconi Records, Cabaret, Time Passages).
4. **Intervista Selettiva (Massimo 2 domande)**:
   - Se l'utente chiede consigli, non sparare tracce a caso ma porre 2 domande colloquiali per inquadrare slot temporale e risposta acustica, per poi consigliare 2 tracce dettagliando incastro armonico (Camelot) e impatto sonoro.
