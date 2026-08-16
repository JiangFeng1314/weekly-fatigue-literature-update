# Weekly Fatigue Literature Update

A Codex skill that runs the local half of the weekly fatigue-literature pipeline:
import cloud-produced weekly batches from Google Drive into a local Windows Zotero
library, deduplicate against the whole library, assign the six fixed fatigue-research
collections, attach available PDFs, verify every write, and archive the processed batch.

## What it does

- Discovers the authoritative batch control file `Zotero_本周入库清单_YYYYMMDD.json`
- Deduplicates by normalized DOI first, then normalized title fallback
- Writes new items and PDF children through the local Zotero Connector
- Classifies items into six fixed collections
- Verifies item key, DOI, target collection, and PDF child after writing
- Archives successful batches and writes structured logs

## Install

Copy this folder to the Codex skills directory:

```powershell
Copy-Item -LiteralPath 'weekly-fatigue-literature-update' -Destination "$env:USERPROFILE\.codex\skills" -Recurse -Force
```

Restart Codex or open a new session so the skill is discovered.

## Prerequisites

- Zotero Desktop running at `http://127.0.0.1:23119`
- Google Drive Desktop with the inbox/archive folders available locally
- Python 3 with only the standard library
- A real `config.json` derived from `assets/config.template.json`

## Setup

```powershell
Copy-Item 'assets\config.template.json' 'D:\zotero-weekly-config\config.json'
```

Edit the copied `config.json` and set real values for `python_exe`, `powershell_exe`,
`zotero_exe`, `google_drive_inbox`, and `google_drive_archive`.

## Usage

```powershell
# read-only preview
python scripts\sync_zotero.py --config D:\zotero-weekly-config\config.json --dry-run

# real sync
python scripts\sync_zotero.py --config D:\zotero-weekly-config\config.json

# manual Windows entry point: check for a batch, start Zotero, then sync
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check_zotero.ps1 -ConfigPath D:\zotero-weekly-config\config.json
```

## Safety rules

- Read via Zotero Local API; write only through the local Connector.
- Never modify `zotero.sqlite` or delete/merge existing user data.
- Never bypass institutional access, paywalls, or captchas.
- Dry-run is read-only.

## License

MIT