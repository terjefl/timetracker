# Timetracker

<img src="app/static/logo.svg" width="72" align="right" alt="">

Enkel, selvhostet timeregistrering og fakturering for private oppdrag.

- **Kunderegister** med navn, telefon, e-post, adresse og notater
- **Timepriser per kunde** — én pristype (brukes automatisk, velges ikke) eller flere, f.eks. «Standard» og «Helg» (velges ved registrering)
- **Timeregistrering** med dato, antall timer (`1,5`, `1:30` eller `90m`) og beskrivelse
- **Oversikt** med timer per kunde, ufakturert beløp, utestående og timer per måned
- **Rapport** over alle timer i en periode, summert per pristype — utskrift/PDF og CSV
- **Faktura** av ufakturerte timer: kladd → sendt → betalt. Timer som ligger på en faktura kommer ikke med i neste rapport over ufakturerte timer, og timer på sendte fakturaer er låst

Timepris og typenavn kopieres inn på hver registrering, så prisendringer påvirker ikke historikk eller sendte fakturaer.

## Teknikk

FastAPI + Jinja2 + SQLite (én fil i `/data`). Ingen innlogging i appen — den er ment å ligge bak Cloudflare Access eller tilsvarende.

## Kjøre

```bash
docker compose -f compose.example.yml up -d
# http://localhost:8500
```

Lokalt for utvikling:

```bash
uv venv && uv pip install -r requirements-dev.txt
TIMETRACKER_DB=data/dev.db .venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest
```

## Backup

All data ligger i `timetracker.db` i datakatalogen. SQLite kjører i WAL-modus; ta konsistent kopi med
`sqlite3 timetracker.db ".backup backup.db"` (eller stopp containeren og kopier fila).

## Image

GitHub Actions kjører testene og publiserer `ghcr.io/terjefl/timetracker:latest` ved push til `main`.

Ikoner genereres fra SVG-ene med `uv run --with resvg-py --with pillow scripts/make_icons.py`.
