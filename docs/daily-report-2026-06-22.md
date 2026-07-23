# Daily Work Report — Academic Trainer Profile

**Project:** Academic-TrainerProfile  
**Branch:** `experiments/database`  
**Repository:** [LPA-AI-Projects/Academic-TrainerProfile](https://github.com/LPA-AI-Projects/Academic-TrainerProfile)  
**Report date:** 22 June 2026  
**Production URL:** `https://academic-trainerprofile-production.up.railway.app`

---

## Executive Summary

Integrated Bitrix24 as the chat trigger for trainer profile generation and refinement. The backend reads task comments, pulls course outlines from Google Drive and trainer CVs from Zoho CRM, generates PDF profiles via the LLM pipeline, and posts results back into the Bitrix task chat. Several production bugs (BBCode URL parsing, Zoho module mapping) were fixed during testing, and chat reply formatting plus infinite-loop prevention were added before pushing to GitHub.

---

## Work Completed

### 1. Bitrix24 outbound webhook integration (20 Jun)

**Commit:** `5b20af3` — *Add Bitrix24 outbound webhooks for trainer profile generate and refine*

| Component | Description |
|-----------|-------------|
| **Endpoint** | `POST /api/v1/bitrix/trainer-profile/generate` |
| **Event** | `ONTASKCOMMENTADD` (Task comment added) |
| **Auth** | `BITRIX_APPLICATION_TOKEN` from Bitrix outbound webhook |
| **New files** | `bitrix_service.py`, `bitrix_outbound.py` |
| **Updated** | `profile_service.py`, `google_drive_service.py`, `main.py`, `config.py`, `schemas.py` |

**Architecture:**

- **Bitrix** — chat trigger only (not a source of trainer data)
- **Zoho CRM** — trainer CVs and record metadata
- **Google Drive** — course outline documents
- **Backend** — orchestration, LLM generation, PDF export, Drive upload

---

### 2. Single webhook with comment-based routing (22 Jun)

**Commit:** `fbcfc74` — *Use single Bitrix outbound webhook with comment-based generate/refine routing*

One outbound webhook URL handles both generate and refine. The backend classifies each task comment:

| Action | Comment format |
|--------|----------------|
| **Generate** | `trainer_profile` + `outline:` + Google Drive URL + `trainers:` + Zoho URL(s) |
| **Refine** | `unique_code: TR2001` + `refine:` + instruction (minimum 10 characters) |
| **Ignore** | Anything else (e.g. plain `Refine:` from the course-outline bot) |

---

### 3. BBCode URL parsing fix (22 Jun)

**Commit:** `91944ea` — *Fix Bitrix BBCode URL parsing for Zoho links and Drive outlines*

**Problem:** Bitrix task comments wrap URLs in BBCode (`[URL]...[/URL]`). A trailing `[` was left on Zoho record IDs and Drive URLs, causing:

- Zoho API 404 errors (e.g. `7026232000010532226%5B`)
- Corrupted Google Drive outline URLs

**Fix:** BBCode-aware URL parsing in `bitrix_service.py`.

---

### 4. Zoho module name mapping (22 Jun)

**Commit:** `937d406` — *Map Zoho CustomModule tab URLs to configured trainer API module name*

**Problem:** Zoho CRM URLs use the tab name `CustomModule1`, but the API module name is `Trainers`, resulting in `INVALID_MODULE` errors.

**Fix:** Ignore `CustomModuleN` in URLs; resolve records using `ZOHO_TRAINER_MODULE_API_NAME=Trainers` plus the record ID from the URL.

---

### 5. Bitrix task chat reply after generation (22 Jun)

**Commit:** `c921566` — *Post trainer profile Drive links back to Bitrix task chat after generate/refine*

**Problem:** Generated PDF and Drive links were only returned in the HTTP response to Bitrix. Bitrix does not automatically post webhook responses into the task chat.

**Solution:**

- After successful generate or refine, post a reply via `task.commentitem.add` (fallback: `im.message.add`)
- Config flag: `BITRIX_REPLY_TO_CHAT=true` (default)

**Reply format posted to Bitrix chat:**

```
Trainer profile generated successfully.

name: Jane Doe
trainer_id: TR321
Google Drive: https://drive.google.com/...

name: dsf
trainer_id: TR342
Google Drive: https://drive.google.com/...
```

**Infinite-loop prevention:**

Posting a task comment triggers `ONTASKCOMMENTADD` again. Bot replies are detected by `is_trainer_profile_bot_reply()` (header `Trainer profile … successfully.` + `trainer_id:` line) and ignored before any processing.

---

### 6. Git push (22 Jun)

All changes pushed to:

- **Branch:** `experiments/database`
- **Latest commit:** `c921566`

---

## Bitrix24 Configuration

| Field | Value |
|-------|-------|
| Handler URL | `https://academic-trainerprofile-production.up.railway.app/api/v1/bitrix/trainer-profile/generate` |
| Event | `ONTASKCOMMENTADD` |
| Application token | Same as `BITRIX_APPLICATION_TOKEN` env var |

**Inbound webhook scopes:** `im`, `disk`, `tasks`, `user` (no `crm` scope required).

---

## Railway Environment Variables

| Variable | Purpose |
|----------|---------|
| `BITRIX_APPLICATION_TOKEN` | Validates outbound webhook requests from Bitrix |
| `BITRIX_REST_WEBHOOK_URL` | Inbound REST — read comments and post replies |
| `BITRIX_REPLY_TO_CHAT` | `true` — post Drive links back to task chat |
| `ZOHO_TRAINER_MODULE_API_NAME` | `Trainers` |
| `ZOHO_TRAINER_CV_FIELD_API_NAME` | `Trainer_CV` |
| `ANTHROPIC_*`, `DATABASE_URL`, `GOOGLE_*`, `ZOHO_*`, `PUBLIC_BASE_URL`, `API_SECRET_KEY` | Existing app configuration |

---

## Issues Encountered

| Issue | Root cause | Status |
|-------|------------|--------|
| Zoho 404 with `%5B` on record ID | BBCode `[` appended to URL | **Fixed** |
| Zoho `INVALID_MODULE` for CustomModule1 | Tab name ≠ API module name | **Fixed** |
| Corrupted outline Drive URL | Same BBCode issue | **Fixed** |
| PDF link not returned to Bitrix chat | Feature not implemented | **Fixed** |
| Task 79292 empty comment / `DIALOG_ID_EMPTY` | Chat ID resolution fallback failed | **Open** — ignored gracefully |
| Application token exposed in chat | Shared during debugging | **Action:** rotate token |

---

## Successful Test Flow (reference)

**Task 79682:** webhook received → comment fetched from `chat715104` → routed as `generate` → Drive outline downloaded → Zoho CVs fetched → profiles generated → reply posted to task chat.

---

## Files Changed (cumulative)

| File | Changes |
|------|---------|
| `backend/app/services/bitrix_service.py` | REST client, message parser, Zoho URL parse, BBCode fix |
| `backend/app/services/bitrix_outbound.py` | Outbound webhook, routing, chat reply, loop prevention |
| `backend/app/services/profile_service.py` | `generate_from_bitrix_chat()` flow |
| `backend/app/services/google_drive_service.py` | Outline download from Drive |
| `backend/app/main.py` | Webhook routes, reply wiring |
| `backend/app/config.py` | Bitrix settings including `BITRIX_REPLY_TO_CHAT` |
| `backend/app/schemas.py` | Bitrix-related request/response schemas |
| `backend/.env.example` | Bitrix env var documentation |

---

## Next Steps (optional)

1. Fix comment fetch fallback for tasks where `chat_id` cannot be resolved (task 79292 pattern).
2. Rotate `BITRIX_APPLICATION_TOKEN` and any other credentials shared during debugging.
3. Verify Railway deployment picks up `c921566` and test end-to-end in production.
4. Add failure replies to Bitrix chat (optional — currently only success is posted).

---

## Commit Log (20–22 Jun 2026)

| Commit | Date | Message |
|--------|------|---------|
| `5b20af3` | 20 Jun | Add Bitrix24 outbound webhooks for trainer profile generate and refine |
| `fbcfc74` | 22 Jun | Use single Bitrix outbound webhook with comment-based generate/refine routing |
| `91944ea` | 22 Jun | Fix Bitrix BBCode URL parsing for Zoho links and Drive outlines |
| `937d406` | 22 Jun | Map Zoho CustomModule tab URLs to configured trainer API module name |
| `c921566` | 22 Jun | Post trainer profile Drive links back to Bitrix task chat after generate/refine |

---

*Generated from project git history and development session notes.*
