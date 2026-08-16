# Weekly Fatigue Literature Update

A Codex skill that runs the local half of the weekly fatigue-literature pipeline:
import cloud-produced weekly batches from Google Drive into a local Windows Zotero
library, deduplicate against the whole library, assign the six fixed fatigue-research
collections, attach available PDFs, verify every write, and archive the processed batch.

It can also turn an archived batch into a Chinese-summary WeChat公众号 draft for manual
review. Draft generation is local-first and publish-free.

## What it does

- Discovers the authoritative batch control file Zotero_本周入库清单_YYYYMMDD.json
- Deduplicates by normalized DOI first, then normalized title fallback
- Writes new items and PDF children through the local Zotero Connector
- Classifies items into six fixed collections
- Verifies item key, DOI, target collection, and PDF child after writing
- Archives successful batches and writes structured logs
- Generates a WeChat draft with per-paper Chinese summary and comment
- Optionally saves the WeChat draft through draft/add when --upload is passed

## Install

Copy this folder to the Codex skills directory:

    Copy-Item -LiteralPath 'weekly-fatigue-literature-update' -Destination "$env:USERPROFILE\.codex\skills" -Recurse -Force

Restart Codex or open a new session so the skill is discovered.

## Prerequisites

- Zotero Desktop running at http://127.0.0.1:23119 for Zotero sync
- Google Drive Desktop with the inbox/archive folders available locally
- Python 3 with only the standard library
- A real config.json derived from assets/config.template.json
- Optional WeChat config derived from assets/wechat_mp_config.template.json

## Setup

    Copy-Item 'assets\config.template.json' 'D:\zotero-weekly-config\config.json'

Edit the copied config.json and set real values for python_exe, powershell_exe,
zotero_exe, google_drive_inbox, and google_drive_archive.

For WeChat draft upload, also copy the WeChat template somewhere private:

    Copy-Item 'assets\wechat_mp_config.template.json' 'D:\zotero-weekly-config\wechat_mp_config.json'

Fill in appid and secret. Do not commit that file.

## Usage

    # read-only Zotero preview
    python scripts\sync_zotero.py --config D:\zotero-weekly-config\config.json --dry-run

    # real Zotero sync
    python scripts\sync_zotero.py --config D:\zotero-weekly-config\config.json

    # manual Windows entry point: check for a batch, start Zotero, then sync
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check_zotero.ps1 -ConfigPath D:\zotero-weekly-config\config.json

    # local WeChat draft review files, no keys required
    python scripts\generate_wechat_draft.py G:\path\to\Zotero_本周入库清单_20260814.json --summaries examples\summaries_20260814.json

    # save a WeChat draft; user manually publishes later
    python scripts\generate_wechat_draft.py G:\path\to\Zotero_本周入库清单_20260814.json --config D:\zotero-weekly-config\wechat_mp_config.json --summaries examples\summaries_20260814.json --upload

## WeChat draft notes

- The script never calls publish endpoints such as freepublish/submit.
- Personal/unverified accounts may receive 48001; the local HTML file is the fallback.
- Per-paper Chinese summaries and comments should be provided as a summaries JSON
  keyed by cumulative_id. The example file is examples/summaries_20260814.json.
- See references/wechat_draft_api.md for API limits, cover media, IP whitelist,
  and manual copy-paste workflow.

## Safety rules

- Read via Zotero Local API; write only through the local Connector.
- Never modify zotero.sqlite or delete/merge existing user data.
- Never bypass institutional access, paywalls, or captchas.
- Dry-run is read-only.
- Never commit WeChat AppSecret, access_token, or real config files.
- Treat generated Chinese summaries/comments as review guidance, not paper abstracts.

## License

MIT
