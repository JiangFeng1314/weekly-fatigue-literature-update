# Weekly Fatigue Literature Update - Workflow Reference

## Contents

- [Scope](#scope)
- [Inputs](#inputs)
- [Zotero collection map](#zotero-collection-map)
- [Deduplication rules](#deduplication-rules)
- [Write model and limits](#write-model-and-limits)
- [PDF rules](#pdf-rules)
- [Idempotency](#idempotency)
- [Logging and verification](#logging-and-verification)
- [Windows login automation](#windows-login-automation)
- [Safety red lines](#safety-red-lines)
- [Local layout](#local-layout)
- [Run commands](#run-commands)
- [Known historical bugs](#known-historical-bugs)

## Scope

- Keep the cloud workflow unchanged: retrieval, citation expansion, ResearchRabbit,
  deduplication recommendations, cumulative Excel ledger, Gmail weekly report, file
  library, and Zotero bridge-file generation.
- Local responsibility: Google Drive synced artifacts -> Windows -> Zotero
  import/classification/PDF association -> write-back verification -> archive.
- Do not simplify by redoing the cloud retrieval process.

## Inputs

Expected Google Drive layout, referenced from `config.google_drive_inbox`:

```text
Zotero周报同步
├─ 待入库
└─ 已处理
```

Typical `待入库` files:

```text
Zotero_本周入库_YYYYMMDD.ris
Zotero_本周入库清单_YYYYMMDD.json   # authoritative control file
Zotero_本周入库清单_YYYYMMDD.csv
Zotero_PDF待获取_YYYYMMDD.csv
```

If `待入库` is missing, report `GOOGLE_DRIVE_NOT_SYNCED`; never fabricate paths.

## Zotero collection map

Root: `00-博士论文综述文献` (`MNEMAYZC`).

| main_category | Collection name | collection key | connector target id |
|---|---|---|---|
| 1 | 1-物理约束机器学习疲劳寿命预测 | SH545K65 | C78 |
| 2 | 2-疲劳损伤演化与寿命预测模型 | FY9H2XUP | C79 |
| 3 | 3-轨道车辆构架/部件疲劳、载荷谱与实测服役数据 | JSVTV99R | C80 |
| 4 | 4-加速度/状态监测驱动的间接疲劳预测 | DXHKVS53 | C81 |
| 5 | 5-多轴疲劳、焊接接头、断裂与裂纹扩展 | SZW8FRJ3 | C82 |
| 6 | 6-多保真、迁移学习、不确定性与小样本疲劳建模 | HBBM7GV4 | C83 |

- `main_category` is the only classification authority.
- `target_collection` must exactly match the mapped name.
- One new item goes to exactly one primary collection. Secondary categories are tags
  or log fields only.
- Missing/invalid/conflicting category -> `CLASSIFICATION_ERROR`; do not guess or
  create collections.

## Deduplication rules

- Search the entire Zotero Library, not only the target collection.
- Normalize DOI: lowercase, trim, strip `https://doi.org/`, `http://doi.org/`,
  `http(s)://dx.doi.org/`, `doi:`, and `DOI:` prefixes.
- Same DOI -> `EXISTING`; never create again.
- Title fallback: Unicode normalize, lowercase, trim, collapse whitespace, normalize
  common punctuation.
- Use title comparison only when DOI is missing or the original record has no DOI.
- Do not use risky fuzzy matching.
- Multiple candidates or ambiguity -> `NEEDS_REVIEW`; do not auto-merge.

## Write model and limits

- Never modify `zotero.sqlite` directly and never use SQLite table operations.
- Never delete existing items, attachments, or collections.
- Local API (`/api/...`) is read-only. Write through Connector:
  - `POST /connector/saveItems` creates new items.
  - `POST /connector/updateSession` moves same-session items to the target collection.
  - `POST /connector/saveAttachment` attaches a PDF child to a same-session item.
- Zotero 9.0.6 has no BBT debug bridge. The official Connector has no endpoint to add
  an existing item to a collection or attach a PDF to an existing item.
- Therefore:
  - New item + PDF child attachment: supported by the script.
  - Existing item already in the right collection: `EXISTING`.
  - Existing item not in the right collection: `NEEDS_REVIEW`; never fake
    `COLLECTION_ADDED`.
  - Existing item missing a PDF: `NEEDS_REVIEW`.
- Fully automating existing items requires a Zotero Web API key (`zotero_api_key` and
  `zotero_user_id`) or BBT. Do not bypass institutional auth, paywalls, or captchas.

## PDF rules

- Read `pdf_access_status` and `pdf_url`.
- `available`: download, then verify non-HTML login page, size greater than zero, and
  `%PDF-` header or PDF Content-Type. Attach as a child only after verification.
- `requires_access`: do not bypass permissions. Record `PDF_REQUIRES_ACCESS` and keep
  the item.
- `unavailable`: record `PDF_UNAVAILABLE` and keep the item.
- Before upload, check the parent for an existing PDF by URL, filename, or content ->
  `PDF_ALREADY_EXISTS`; do not re-upload.

## Idempotency

- `processed_batches.json` accelerates judgment, but live Zotero state is the final
  source of truth.
- If state files are lost, DOI/title dedup must still prevent duplicate items and PDFs.
- Re-running the same `YYYYMMDD` batch must be stable.

## Logging and verification

- Write `logs/YYYYMMDD.log` and `logs/YYYYMMDD.json` per batch.
- Each record includes `batch_date`, `source_week`, `cumulative_id`, `title`, `doi`,
  `main_category`, `target_collection`, `target_collection_key`, `zotero_item_key`,
  `action`, `pdf_status`, `error`, and `timestamp`.
- After writing, re-read Zotero and confirm item key, DOI, target collection, and PDF
  child one by one.

## Windows login automation

- Task Scheduler name: `Zotero Weekly Literature Sync`.
- Trigger: at log on, current user.
- Entry: `scripts/login_trigger.ps1` -> `scripts/check_zotero.ps1` ->
  `scripts/sync_zotero.py`.
- Exit quietly when there is no pending batch; remind and attempt to start Zotero only
  when a batch is pending and Zotero is not running.
- Poll `127.0.0.1:23119` every 3-5 seconds for `poll_timeout_seconds` (default 120).
  On timeout record `ZOTERO_START_TIMEOUT`.

## Safety red lines

- Do not write passwords, cookies, API keys, or tokens to ordinary logs.
- Do not bypass access controls.
- Do not delete or merge existing user data.
- Dry-run is read-only: no item creation, no collection changes, no long-term PDF
  downloads, no Drive file moves.

## Local layout

```text
<skill>/scripts/sync_zotero.py
<skill>/scripts/check_zotero.ps1
<skill>/scripts/login_trigger.ps1
<working-dir>/config.json          # copied from assets/config.template.json
<working-dir>/logs/
<working-dir>/state/
```

## Run commands

```powershell
# dry-run
python <skill>/scripts/sync_zotero.py --config <working-dir>/config.json --dry-run

# real sync
python <skill>/scripts/sync_zotero.py --config <working-dir>/config.json

# manual check/start Zotero, then sync
powershell -NoProfile -ExecutionPolicy Bypass -File <skill>/scripts/check_zotero.ps1 -ConfigPath <working-dir>/config.json
```

## Known historical bugs

1. DriveFS enumeration order made RIS files the classification source by mistake.
   Fixed by using only `Zotero_本周入库清单_YYYYMMDD.json` as the control file; keep
   the regression test.
2. `requires_access` was misclassified as `missing_pdf` / `NEEDS_REVIEW`. Fixed to
   `EXISTING` plus `PDF_REQUIRES_ACCESS`; keep regression tests for `requires_access`,
   `unavailable`, and existing-PDF cases.

Maintenance rule: except for real new bugs, do not refactor the verified core logic.