# AGENTS.md — drops-web-backend

Regole di casa lette da ogni agente (Claude, Codex, Cursor, Gemini) che apre questo repo.
Repo: **drops-web-backend** (github.com/gianco-cesarei/drops-web-backend) — API FastAPI (Python)
deployata su Render, dietro il Worker Cloudflare del frontend.

Ecosistema Drops = 3 repo: `drops-web-backend` (questo), `drops-web-frontend` (Astro/Worker),
`drops-desktop` (Tauri v2). Non mischiarli: una sessione = un repo.

---

## 1. Gate di implementazione (obbligatorio)
Nessuna feature entra su `main` senza questi 5 passi:
1. **Ricognizione** — subagent Explore (read-only): capire file e pattern esistenti, riusare ciò che c'è.
2. **Design** — subagent Plan (o plan mode): approccio approvato prima di scrivere codice.
3. **Implementazione** — su branch `feat/*` o `fix/*`. Mai commit diretti su `main`.
4. **Verifica** — review + test: `pytest`, e prova del container (`Dockerfile`) prima del deploy Render.
5. **Rilascio** — merge + tag versione + voce CHANGELOG.

## 2. Mappa agenti / skill → lavoro
- Ricognizione/scope incerto → **Explore** · Design/architettura → **Plan**
- Implementazione isolata → **general-purpose**; multi-file → agente principale su branch
- Domande Claude Code/SDK/API → **claude-code-guide**
- Deliverable → skill **docx/pdf/pptx/xlsx** · grafici → **dataviz**

## 3. Versionamento
Linea semver propria del backend (`v0.x` → `v1.0.0` al lancio). `feat`→minor, `fix`→patch,
breaking→major. Ogni release = tag + CHANGELOG. Tenere allineate le API col frontend.

## 4. Stack & ambiente
- FastAPI (Python) in `backend/`, containerizzato (`Dockerfile`), deploy su Render.
- Sta dietro al Worker Cloudflare: leggere IP reale da `CF-Connecting-IP`/`X-Forwarded-For`,
  `redirect_uri` dinamico da `Host`/`X-Forwarded-Host`.
- Account/dati = **Supabase piano FREE** (auth + metadati). File musica su **Cloudflare R2**
  (egress $0). Mai file audio su Supabase.
- Segreti mai nel repo: solo env/secret store. `cookies.txt`, `.env`, chiavi → gitignore.
  (Nota storica: c'era un `cookies.txt` con sessioni reali nel vecchio monorepo — verificare che
  non sia rientrato qui e invalidare quelle sessioni.)

## 5. Brand
Drops = musica elettronica curata di nicchia. Generi accettati/esclusi e tono: vedi AGENTS.md del
frontend (fonte brand). Il backend non genera contenuti editoriali ma deve rispettare gli stessi
confini quando serve dati/metadati.
