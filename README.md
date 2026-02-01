# ADE to ICS Proxy

Google Cloud Function that converts ADE Campus calendar to ICS format for Google Calendar subscription.

## Quick Deploy

```bash
gcloud run deploy ade-to-ics \
  --source . \
  --function proxy_ade \
  --base-image python314 \
  --region europe-west9 \
  --allow-unauthenticated
```

## Usage

```
GET https://your-cloud-run-url/?resources=3018&englishResources=1479
```

| Parameter          | Default | Description                                  |
| ------------------ | ------- | -------------------------------------------- |
| `resources`        | `3018`  | Main group resource ID                       |
| `englishResources` | `1479`  | English group resource ID (for Anglais swap) |
| `projectId`        | `19`    | ADE project ID                               |

Subscribe to the URL in Google Calendar: **Settings → Add calendar → From URL**

## Local Development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
# Open http://127.0.0.1:8080/
```

---

# Research Documentation

## The Problem

ADE Campus provides a calendar interface but:

- No direct ICS export with event types (CM, TD, TP, Contrôle...)
- The built-in ICS only has basic info, no "Type" field
- Need to combine two groups: main schedule (3018) minus wrong Anglais + correct Anglais from 1479

## Approaches Tried

### ❌ Approach 1: Direct ICS Download + Hex Decoding

**Idea:** Download the official `.ics` file and decode event IDs from the UID field.

```
UID: ADE60506f6c797465636850617269735361636c6179...3433393733
     ^^^                                           ^^^^^^^^
     "ADE" prefix                                  hex-encoded eventId
```

**URLs tested:**

```
/jsp/custom/modules/plannings/anonymous_cal.jsp?resources=3018&projectId=19&calType=ical
```

**Result:** Works for ~89% of events. Some UIDs don't decode to valid event IDs.

**Problem:** Missing Type field. Had to call `eventInfo.jsp` for each event → slow (sequential).

### ❌ Approach 2: imagemap.jsp Scraping

**Idea:** Scrape `imagemap.jsp` which returns HTML with embedded event IDs.

```
/jsp/custom/modules/plannings/imagemap.jsp?width=1018&height=590
```

**Why it failed:**

- Requires JavaScript execution (frame-based architecture)
- All pages detect iframe context: `if (top.document.location == document.location) top.document.location = "index.jsp";`
- Resource selection only saved via JS navigation: `document.location = "tree.jsp?selectId=3018"`
- HTTP requests alone cannot trigger this state

**Tested 8 strategies:**

1. Basic ICS + imagemap → 0 events
2. With interface.jsp → 0 events
3. Full frame setup (10 pages) → 0 events
4. With tree.jsp selection → 0 events
5. POST to tree.jsp → 0 events
6. Multiple tree calls → 0 events
7. With Referer headers → 0 events
8. Alternative endpoints → 0 events

**Error always returned:**

```
"To display a planning, you have to:
- Select one or multiple resources on the resources tree
- Select a date on the piano"
```

### ❌ Approach 3: pianoWeeks.jsp / bounds.jsp

**Idea:** Navigate weeks using piano control.

```
/jsp/custom/modules/plannings/bounds.jsp?week=3&reset=false
/jsp/custom/modules/plannings/pianoWeeks.jsp
```

**Result:** Same problem - requires JS frame context.

### ❌ Approach 4: ADE-Scheduler GitHub Repo

**Reference:** [github.com/SnaKyEyeS/ADE-Scheduler](https://github.com/SnaKyEyeS/ADE-Scheduler)

**Finding:** Uses official OAuth API (`/API/projects/...`), not web scraping. Requires credentials - not available for anonymous access.

### ✅ Approach 5: RSS Feed (Final Solution)

**Discovery:** ADE exposes RSS feeds!

```
/jsp/rss?projectId=19&resources=3018&nbDays=999
```

**Advantages:**

- Returns all events with GUID (= eventId)
- No JavaScript required
- Fast (single HTTP request)

**The GUID field is the eventId:**

```xml
<item>
  <guid>43973</guid>
  <title>Mathématiques</title>
  <description>...</description>
</item>
```

**Complete flow:**

1. Init session via `anonymous_cal.jsp` (sets cookies)
2. Fetch RSS for main group → filter out "Anglais"
3. Fetch RSS for English group → keep only "Anglais"
4. Combine feeds, dedupe by GUID
5. Parallel fetch `eventInfo.jsp?eventId={guid}` for Type
6. Build ICS

## Key URLs Reference

| Endpoint                                                                               | Purpose                          |
| -------------------------------------------------------------------------------------- | -------------------------------- |
| `/jsp/`                                                                                | Base URL, sets initial cookies   |
| `/jsp/standard/gui/interface.jsp`                                                      | Frame interface (JS-dependent)   |
| `/jsp/custom/modules/plannings/anonymous_cal.jsp?resources=X&projectId=Y&calType=ical` | Session init + ICS download      |
| `/jsp/rss?projectId=X&resources=Y&nbDays=999`                                          | **RSS feed (the solution)**      |
| `/jsp/custom/modules/plannings/eventInfo.jsp?eventId=X`                                | Event details with Type field    |
| `/jsp/custom/modules/plannings/imagemap.jsp`                                           | Calendar view (requires JS)      |
| `/jsp/custom/modules/plannings/bounds.jsp`                                             | Week navigation (requires JS)    |
| `/jsp/custom/modules/plannings/tree.jsp?selectId=X`                                    | Resource selection (requires JS) |

## Event Type Extraction

From `eventInfo.jsp` response:

```html
<label>Type : CM</label>
```

Regex: `<label>Type\s*:\s*([^<]+)</label>`

Types found: `CM`, `TD`, `TP`, `C-TD`, `Projet Encadré`, `Contrôle`, `Présentation`, `Evènement`

## Architecture Lesson

ADE uses a **frame-based JavaScript architecture**:

```
┌─────────────────────────────────────────┐
│ interface.jsp (parent frame)            │
│  ┌─────────────────────────────────────┐│
│  │ tree.jsp (left)  │ imagemap (right) ││
│  │                  │                  ││
│  │ JS: selectId=X   │ Reads state      ││
│  │ → saves to       │ from server      ││
│  │   server session │                  ││
│  └──────────────────┴──────────────────┘│
└─────────────────────────────────────────┘
```

**Why HTTP alone fails:**

- Frame pages detect direct access → redirect to index.jsp
- State only saved during JS navigation
- No API for direct state manipulation

**Why RSS works:**

- Standalone endpoint, no frame context
- Cookies from `anonymous_cal.jsp` sufficient
- Returns complete data

## Performance

| Metric           | Value                               |
| ---------------- | ----------------------------------- |
| Total time       | ~2 seconds                          |
| RSS fetch        | ~0.3s (parallel for both resources) |
| Type fetching    | ~1.5s (20 parallel workers)         |
| Events processed | 95                                  |
| Types found      | 95/95 (100%)                        |

## Files

```
ade2ics/
├── main.py           # Google Cloud Function
├── requirements.txt  # Dependencies
└── README.md         # This file
```
