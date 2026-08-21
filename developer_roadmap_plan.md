# Drops — Developer Roadmap Plan (Backend Reference)

Questo documento definisce la struttura strategica e le specifiche per l'implementazione della nuova **Pagina Developer** (`DeveloperRoadmap.tsx`) all'interno dell'applicazione Drops.

L'obiettivo è creare una pagina di pianificazione interattiva per lo sviluppo del progetto, dotata di un menu laterale (sidebar) a schede per navigare tra le diverse macro sezioni e di visualizzazioni ad accordion/collassabili per i dettagli tecnici.

---

## Struttura dell'Interfaccia Utente (UI) della Pagina Developer

1. **Sidebar Laterale Sinistra:**
   * Consente di cambiare il focus tra le 8 macro sezioni elencate di seguito.
   * La scheda selezionata deve risaltare visivamente.

2. **Area Contenuto Principale:**
   * **Titolo della Sezione:** Nome della macro area corrente.
   * **Introduzione/Focus:** Breve spiegazione dell'ambito tecnologico e dell'obiettivo strategico della sezione.
   * **Elenco dei Task:** Ciascun task deve avere un titolo chiaro incentrato sul beneficio concreto per l'utente.
   * **Sub-Task Collassabili:** Sotto ogni task, un elenco di sotto-attività che dettagliano le decisioni da prendere, le raccomandazioni e le azioni pratiche da svolgere, contrassegnate dai tag di competenza:
     * `[front]` (Astro/React/CSS)
     * `[back]` (FastAPI/Python/Docker/Supabase/R2)
     * `[desktop]` (Tauri v2/Rust/OS)

3. **Archivio Attività Completate:**
   * Una sezione o scheda speciale (Sezione 8) concepita a comparsa o collassata di default per tenere traccia delle milestone già consolidate senza ingombrare la visualizzazione attiva.

---

## Dettaglio delle Macro Sezioni, Task e Sub-Task

### Sezione 1: Downloader Singolo & Tracce Esclusive (YT/SC)
* **Introduzione:** Gestione dell'acquisizione di singole tracce audio da sorgenti esterne, con ottimizzazioni per la scena underground e materiali esclusivi.
1. **Task 1.1: Download Diretto da YouTube per tracce esclusive**
   * *Miglioramento Utente:* Scarica all'istante l'audio da video YouTube che ospitano tracce, remix o bootleg rari non pubblicati altrove, senza dover ricorrere a convertitori web esterni instabili o pieni di pubblicità.
   * *a) Integrazione libreria di estrazione audio (yt-dlp)* `[back]`
     * **Scelte & Raccomandazioni:** Usare la libreria `yt-dlp` installata direttamente sul backend FastAPI per estrarre l'audio in formato MP3 a 320kbps (Raccomandato per compatibilità ottimale con hardware DJ).
     * **Azione:** Sviluppare l'endpoint `/api/download/youtube-direct` con gestione asincrona delle code sul server.
   * *b) Interfaccia di input e feedback di progresso* `[front]`
     * **Scelte & Raccomandazioni:** Utilizzare un indicatore di caricamento globale con notifica di esito (Raccomandato per semplicità di sviluppo iniziale) o sviluppare un sistema di monitoraggio progressivo in tempo reale con Server-Sent Events (SSE).
     * **Azione:** Creare il modulo di input URL nella dashboard e gestire gli stati della richiesta.
2. **Task 1.2: Download Singolo Multi-Sorgente con selezione della qualità**
   * *Miglioramento Utente:* Salva sul tuo dispositivo o nel cloud qualsiasi singola traccia da YouTube o SoundCloud scegliendo la qualità desiderata e organizzandola in cartelle.
   * *a) Opzioni di conversione e formati (MP3 vs FLAC/HQ)* `[back]`
     * **Scelte & Raccomandazioni:** Limitare di default il download a MP3 ad alta qualità (320kbps) per ottimizzare lo storage (Raccomandato), o abilitare il download in formato lossless FLAC/WAV per SoundCloud HQ.
     * **Azione:** Configurare i parametri post-processor FFmpeg sul backend in base alla sorgente e alla scelta dell'utente.

---

### Sezione 2: Gestione Playlist & Download Multiplo
* **Introduzione:** Interfaccia e logica di rete per gestire set completi e intere playlist di etichette senza sovraccaricare le API esterne.
1. **Task 2.1: Anteprima, filtri e selezione brani da Playlist/Set prima del download**
   * *Miglioramento Utente:* Vedi la lista completa delle tracce di una playlist (YT) o di un set (SC) prima di scaricarla, potendo selezionare solo i brani che ti interessano davvero ed escludendo automaticamente quelli che hai già in libreria.
   * *a) Limite massimo elementi risolti in simultanea* `[back]`
     * **Scelte & Raccomandazioni:** Impostare un limite massimo di 100 tracce a playlist (Raccomandato) per evitare blocchi IP temporanei ed esaurimento di memoria sul server.
     * **Azione:** Configurare il controllo del limite ed eventualmente la paginazione all'interno dell'endpoint `/playlist/resolve`.
   * *b) Selezione tramite checkbox e filtro duplicati* `[front]`
     * **Scelte & Raccomandazioni:** Riconoscere automaticamente i file già presenti in locale o in cloud e deselezionarli per default (Raccomandato), lasciando all'utente la facoltà di forzare una sovrascrittura.
     * **Azione:** Creare la tabella dei brani trovati con checkbox dinamici e badge "Già presente".

---

### Sezione 3: Cloud Storage & Libreria Utente
* **Introduzione:** Spostamento del baricentro della libreria utente dal disco locale a una soluzione in cloud sicura, con streaming privato e organizzazione automatica.
1. **Task 3.1: Salvataggio e organizzazione della propria musica in Cloud**
   * *a) Configurazione dello Storage Personale (Cloudflare R2)* `[back]`
     * **Scelte & Raccomandazioni:** Utilizzare Cloudflare R2 (Raccomandato per i costi di traffico in uscita a $0) creando una cartella per ciascun utente sincronizzata con le credenziali Supabase.
     * **Azione:** Sviluppare la logica di upload asincrono su R2 al termine del download e salvare il percorso su database Supabase.
   * *b) Ricerca testuale e filtri nel catalogo cloud* `[front]`
     * **Scelte & Raccomandazioni:** Implementare una ricerca rapida client-side sui metadati (Raccomandato per reattività) o delegare le query di ricerca interamente a Supabase.
     * **Azione:** Disegnare la tabella di visualizzazione della libreria cloud con filtri di genere e barra di ricerca.
2. **Task 3.2: Riproduttore Musicale Cloud e Streaming Privato**
   * *Miglioramento Utente:* Ascolta in streaming la musica salvata nel tuo archivio cloud privato direttamente dal browser, ovunque ti trovi e su qualsiasi dispositivo.
   * *a) Generazione di link sicuri per lo streaming (Presigned URLs)* `[back]`
     * **Scelte & Raccomandazioni:** Generare URL firmati a scadenza (es. validità di 1 ora, Raccomandato) per proteggere i file audio da accessi esterni non autorizzati.
     * **Azione:** Sviluppare l'endpoint `/api/stream/{track_id}` che rilascia il link temporaneo.
   * *b) Riproduttore audio persistente e coda d'ascolto* `[front]`
     * **Scelte & Raccomandazioni:** Implementare un player audio fisso a fondo pagina (Raccomandato) che rimanga attivo navigando nell'app, con supporto a una coda di riproduzione dinamica.
     * **Azione:** Integrare lo stato del player nel contesto globale di React/Astro.

---

### Sezione 4: Integrazione Streaming & Sync Preferiti
* **Introduzione:** Connessione alle piattaforme esterne per importare le selezioni e i metadati accumulati dall'utente.
1. **Task 4.1: Sincronizzazione automatica e download dei preferiti di Spotify**
   * *Miglioramento Utente:* Collega il tuo account Spotify e importa in Drops i tuoi brani "Mi Piace" per verificare quali sono scaricabili o già in tuo possesso ed estrarne i metadati.
   * *a) Algoritmo di matching tra metadati Spotify e sorgenti audio* `[back]`
     * **Scelte & Raccomandazioni:** Eseguire una ricerca su YouTube cercando combinazioni di Titolo + Artista + "Audio" per trovare la corrispondenza migliore (Raccomandato) ed escludere i video non musicali.
     * **Azione:** Creare il motore di matching automatico e l'endpoint di associazione tracce.
   * *b) Interfaccia di gestione dei preferiti importati* `[front]`
     * **Scelte & Raccomandazioni:** Mostrare i brani importati evidenziando lo stato (es. "Scaricabile", "Già scaricato", "Incertezza sulla fonte") con pulsante singolo e multiplo di download (Raccomandato).
     * **Azione:** Sviluppare la dashboard Spotify-to-Cloud con griglia interattiva dei brani.
2. **Task 4.2: Importazione automatica dei "Likes" da SoundCloud e YouTube**
   * *Miglioramento Utente:* Tieni traccia dei brani a cui metti "Like" su SoundCloud e YouTube e importali in blocco per aggiungerli alla tua coda di download.
   * *a) Connessione e recupero dei preferiti senza API proprietarie* `[back]`
     * **Scelte & Raccomandazioni:** Per SoundCloud, chiedere semplicemente all'utente l'URL del proprio profilo pubblico e scansionare i like pubblici (Raccomandato per facilità d'uso) o, in alternativa, far incollare un file JSON esportato.
     * **Azione:** Configurare l'estrattore di metadati per i Mi Piace di SoundCloud e YouTube (via OAuth limitato).

---

### Sezione 5: Corso DJ & Producing Online (Learning Hub)
* **Introduzione:** Portale didattico privato volto a formare l'utente nell'arte del DJing e della produzione con strumenti interattivi.
1. **Task 5.1: Portale di Formazione per DJ e Produttori (Lezioni e Dispense)**
   * *Miglioramento Utente:* Impara le tecniche di mixaggio e produzione musicale in un'area riservata organizzata in moduli didattici, video-lezioni, schede tecniche e test di autovalutazione.
   * *a) Struttura dell'area didattica e hosting dei video delle lezioni* `[front]` `[back]`
     * **Scelte & Raccomandazioni:** Integrare un lettore video per lezioni ospitate su Vimeo/YouTube non in elenco (Raccomandato per ridurre i costi di hosting e banda) invece di caricare video grezzi su R2.
     * **Azione:** Progettare la griglia delle lezioni organizzate per moduli e salvare i progressi dell'utente nel database.
2. **Task 5.2: Mappa Interattiva delle Scuole di Musica e Cabine DJ**
   * *Miglioramento Utente:* Trova accademie musicali fisiche, sale prove, studi di registrazione e cabine DJ a noleggio vicino a te visualizzandoli su una mappa geografica interattiva.
   * *a) Integrazione della mappa geografica dei punti d'interesse* `[front]`
     * **Scelte & Raccomandazioni:** Usare Leaflet con OpenStreetMap (Raccomandato perché open-source e gratuito al 100%) invece di Mapbox/Google Maps che richiedono chiavi API e piani a pagamento.
     * **Azione:** Creare il componente Mappa con marker interattivi che mostrano prezzi, attrezzatura e contatti delle scuole o studi.
3. **Task 5.3: Crate Didattici di Tracce e Player di Beatmatching**
   * *Miglioramento Utente:* Esercitati sul beatmatching (mettere a tempo) direttamente dal browser usando un player speciale con regolatore di velocità (Pitch Control) su pacchetti di tracce selezionate per difficoltà.
   * *a) Regolatore di velocità audio (Pitch Control) via Web Audio API* `[front]`
     * **Scelte & Raccomandazioni:** Fornire un Pitch Slider (da -8% a +8%) con l'opzione "Key Lock" per mantenere o variare l'intonazione originale a seconda dell'esercizio (Raccomandato per simulare i giradischi reali).
     * **Azione:** Sviluppare la logica Web Audio API per regolare il tempo di riproduzione.
   * *b) Selezione e catalogazione dei pacchetti didattici (Crate)* `[back]`
     * **Scelte & Raccomandazioni:** Suddividere le tracce didattiche in base alla difficoltà di beatmatching (es. Crate Facile: intro con cassa pulita; Crate Difficile: intro sincopate o corte).
     * **Azione:** Popolare il database con i brani seed consigliati per l'apprendimento.

---

### Sezione 6: Grafo Discovery & Motore Suggest
* **Introduzione:** Il motore di curatela e raccomandazione orientato alla musica elettronica underground (scena Minimal, Deep House, Techno).
1. **Task 6.1: Motore "Drops Suggest" & Grafo di Etichette, Party e Artisti**
   * *Miglioramento Utente:* Esplora le connessioni tra le etichette discografiche di nicchia, i party underground e i DJ per scoprire nuove tracce affini al tuo gusto attraverso consigli intelligenti.
   * *a) Visualizzazione del grafo relazionale e navigazione* `[front]`
     * **Scelte & Raccomandazioni:** Utilizzare una libreria interattiva leggera come `ForceGraph2D` o `vis.js` (Raccomandato perché gestisce bene il dragging e lo zoom dei nodi in React).
     * **Azione:** Sviluppare la vista del grafo relazionale nel tab "Brain".
   * *b) Algoritmo di raccomandazione* `[back]`
     * **Scelte & Raccomandazioni:** Pesare le raccomandazioni combinando la vicinanza geografica dell'utente ai party, l'affinità dello stile delle etichette discografiche e il momentum temporale (Raccomandato).
     * **Azione:** Sviluppare le query SQL su Supabase/Postgres per estrarre le raccomandazioni affini.
2. **Task 6.2: Guide Editoriali pratiche per il settore musicale**
   * *Miglioramento Utente:* Leggi guide super-sintetiche e aggiornate su come pubblicare la tua musica, stampare in vinile nel 2026, e gestire i codici ISRC/UPC per proteggere i tuoi diritti.
   * *a) Gestione dei contenuti editoriali (CMS leggero vs file statici)* `[front]` `[back]`
     * **Scelte & Raccomandazioni:** Salvare gli articoli come file Markdown statici in Astro (Content Collections) per caricarli all'istante ed evitare query complesse a database (Raccomandato per massimizzare la velocità SEO).
     * **Azione:** Configurare i layout Astro per il rendering dei blocchi di testo delle guide.

---

### Sezione 7: Analisi Audio & Preparazione DJ
* **Introduzione:** Utility locali e cloud per arricchire i metadati e strutturare i file audio per la riproduzione su hardware professionale.
1. **Task 7.1: Analizzatore di BPM automatico nel Cloud**
   * *Miglioramento Utente:* Scopri all'istante il BPM esatto delle tracce scaricate grazie a un'analisi automatica che avviene sul server, senza dover installare motori di analisi pesanti sul tuo computer.
   * *a) Algoritmo e posizionamento del calcolo del BPM* `[back]`
     * **Scelte & Raccomandazioni:** Eseguire l'analisi del BPM sul backend FastAPI in modo asincrono usando librerie Python (NumPy + FFmpeg) per la massima precisione (Raccomandato), salvando il risultato su database.
     * **Azione:** Configurare la coda di analisi in background e i relativi endpoint.
2. **Task 7.2: Editor Tag ID3 ed Esportazione per DJ Hardware (Rekordbox)**
   * *Miglioramento Utente:* Correggi i tag ID3 dei tuoi brani (artista, titolo, copertina, BPM) ed esporta le tracce ordinate in cartelle su una chiavetta USB pronta all'uso per i lettori CDJ professionali.
   * *a) Scrittura fisica dei metadati sui file esportati* `[desktop]` `[front]`
     * **Scelte & Raccomandazioni:** Utilizzare i comandi nativi Rust di Tauri per scrivere i tag ID3 direttamente sui file scaricati prima di copiarli sul dispositivo USB dell'utente (Raccomandato per compatibilità Rekordbox).
     * **Azione:** Implementare la chiamata Rust per l'aggiornamento dei metadati e il gestore dei percorsi delle cartelle.

---

### Sezione 8: Archivio Attività Completate (Completati)
* **Introduzione:** Registro delle funzionalità storiche e strutturali già testate e in produzione.
1. **Task 8.1: Raccolta storica degli obiettivi completati**
   * *Miglioramento Utente:* Tieni traccia dell'affidabilità generale dell'app verificando le milestone superate e i bug fix applicati nel tempo.
   * *a) Spostamento visivo dei vecchi obiettivi in questa sezione* `[front]`
     * **Azione:** Raccogliere in un contenitore a comparsa (accordion) le attività storiche già completate:
       * Split dei 3 monorepo e pulizia del codice morto.
       * Risoluzione dello schermo nero sul mini-player (blocco delle dimensioni dell'artwork a 56x56).
       * Gestione rate limit di Discogs (coda pacizzata con sleep di 300ms e thread pool securizzata).
       * Estrazione IP reale del client Cloudflare per superare il blocco di rate limit globale.
       * Spotify OAuth iniziale con reindirizzamenti dinamici.
