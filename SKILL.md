---
name: weekly-fatigue-literature-update
description: Import weekly fatigue-literature batches from Google Drive into a local Windows Zotero library, archive them, and generate a Chinese-summary WeChat Official Account draft for manual review. Use when the user needs to run the weekly Zotero literature update, sync/deduplicate/classify fatigue papers, attach PDFs, verify imports, schedule the Zotero Weekly Literature Sync task, generate a WeChat公众号 draft, or troubleshoot this workflow.
---

# Weekly Fatigue Literature Update

## Overview

Run the local half of the weekly fatigue-literature pipeline: take cloud-produced
weekly batches from a Google Drive folder, import them into a local Windows Zotero
installation, deduplicate against the whole library, assign the six fixed
fatigue-research collections, attach available PDFs, verify every write, and archive
the processed batch. Do not redo the upstream cloud search or citation-expansion work.

An optional second half turns an archived batch into a Chinese WeChat公众号 draft
for manual review. The generator writes local HTML/Markdown/payload files and, only
when explicitly requested with --upload, saves a draft through the Official Account
draft/add API. It never publishes.

## When to run

- Run scripts/sync_zotero.py with --dry-run first to preview a Zotero batch.
- Run the real Zotero sync only after confirming Zotero Desktop is running and the
  Google Drive Desktop client has materialized the inbox.
- Use the PowerShell entry points for the Windows logon scheduled task.
- Generate a WeChat draft from an archived batch after the Zotero sync has verified.

## Prerequisites

- Zotero Desktop running locally at http://127.0.0.1:23119 for the Zotero sync.
- Google Drive Desktop with the inbox/archive paths available locally.
- Python 3 with only the standard library. Do not install third-party packages.
- A working config.json copied from assets/config.template.json and filled in.
- For WeChat draft upload: a real wechat_mp_config.json copied from
  assets/wechat_mp_config.template.json. Local draft generation needs no WeChat keys.

## Setup

1. Copy assets/config.template.json to a stable working directory as config.json.
2. Replace python_exe, powershell_exe, zotero_exe, google_drive_inbox, and
   google_drive_archive with the real local paths.
3. Keep collection_map unchanged unless the Zotero collection keys were actually
   recreated. main_category is the only classification authority.
4. For WeChat, copy assets/wechat_mp_config.template.json to a secret location as
   wechat_mp_config.json and fill in appid, secret, and optional cover/media values.

## Workflow

1. Discover the authoritative batch from the inbox using only
   Zotero_本周入库清单_YYYYMMDD.json. Ignore RIS/CSV files for classification.
2. Preflight with:
   python scripts/sync_zotero.py --config <path/to/config.json> --dry-run
3. Run the real sync:
   python scripts/sync_zotero.py --config <path/to/config.json>
4. After the run, read Zotero back and confirm each item key, DOI, target collection,
   and PDF child. Only report success after this write-back verification.
5. Optional WeChat draft:
   python scripts/generate_wechat_draft.py <archived_batch.json> --summaries <summaries.json>

## WeChat公众号 draft

- Use scripts/generate_wechat_draft.py to generate a draft from an archived batch.
- Each article requires a Chinese summary and comment. Supply them through the
  optional --summaries JSON keyed by cumulative_id. Without that file, the script
  emits classification-based fallback text and marks it for manual completion.
- Default mode is local-only. Pass --upload to save a draft; never call publish APIs.
- Personal/unverified accounts may receive 48001, so the local HTML fallback is always
  written and can be pasted into mp.weixin.qq.com.
- Keep the real WeChat config secret and out of Git.

## Scripts

- scripts/sync_zotero.py: stdlib-only Python importer and verifier.
- scripts/check_zotero.ps1: checks for a pending batch, starts Zotero if needed,
  then runs sync_zotero.py. Pass -ConfigPath explicitly.
- scripts/login_trigger.ps1: scheduled-task entry point; silently exits when there
  is no pending batch.
- scripts/generate_wechat_draft.py: stdlib-only WeChat draft generator for manual review.

## Hard rules

- Use the Zotero Local API read-only; write only through the local Connector endpoints
  (saveItems, updateSession, saveAttachment). Never edit zotero.sqlite.
- Never delete or merge existing items, attachments, or collections.
- Deduplicate against the whole Zotero library. Normalize DOI first; fall back to
  normalized title only when DOI is missing. Mark ambiguous cases NEEDS_REVIEW.
- Preserve requires_access and unavailable PDF records; never bypass access controls.
- Keep the run idempotent: processed_batches.json is a cache, but the live Zotero
  library is the source of truth.
- Do not rewrite the verified core logic unless there is a real new bug.
- For WeChat, never call freepublish/submit or other publish endpoints. Chinese
  summaries/comments are generated guidance and must be manually reviewed.

## References

- references/workflow_context.md: full workflow contract, dedup rules, PDF rules,
  Windows task configuration, safety red lines, and known historical bugs.
- references/wechat_draft_api.md: WeChat token, draft/add, field limits, media ID,
  IP whitelist, 48001 handling, and manual paste fallback.
