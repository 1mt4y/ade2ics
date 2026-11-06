# Polytech ADE → Google Calendar

Turns the [Polytech Paris-Saclay ADE timetable](https://planning.polytech.universite-paris-saclay.fr)
into a clean ICS feed you can subscribe to from Google Calendar (or any calendar
app) — with the **official course types** (CM / TD / TP / C-TD / Contrôle /
Projet Encadré …) on every event and **exams flagged 🚨** with a 30-minute alarm.

Works for **every promo**: PEIP, FISE (ET3/ET4/ET5), FISA (APP3/APP4/APP5), FC,
plus any other ADE resource id (room, teacher…) you pass by hand.

## For students — 2 minutes

1. Open the picker: **`https://ade-to-ics-988522548799.europe-west9.run.app/pick`**
2. Tick your groups — use the filter box (`APP5`, `ET3 info`, `PEIP2 anglais`…).
3. Copy the generated URL → Google Calendar → **Autres agendas ＋ → À partir de
   l'URL**.

Why exams matter here: the school schedules contrôles under the plain course
name ("Sécurité", "Réseaux 2"…) — only ADE's Type field distinguishes a
Contrôle from a lecture, and most graded sessions are invisible to name
matching. This proxy recovers that field.

## API

| Route | Purpose |
|---|---|
| `/` | the ICS feed (`resources` required; a bare visit redirects to `/pick`) |
| `/pick` | picker UI — every group from `catalog.json` + subscription-URL builder |

| Query param (feed) | Default | Notes |
|---|---|---|
| `resources` | **required** | Resource id or comma list (`1406,1196`) — the **union** of their events, deduplicated |
| `not` (alias `exclude`) | — | Case-insensitive **regex**; drops events whose summary / description / location matches (e.g. `not=APP5%20GrA` to unselect a group someone attached by mistake) |
| `name` | `ADE Polytech` | Calendar display name |
| `projectId` | `2` | **2026-2027.** Bump each school year (see below) |
| `firstDate` / `lastDate` | academic year | `YYYY-MM-DD` |
| `host` | new server | override if ADE moves again |

## Privacy

- **No student names are stored** in this repo or service. `catalog.json`
  contains groups and folders only.
- The service holds no accounts, no cookies, no logs beyond Cloud Run's
  standard request logs.
- The only credential involved is the school's **public** read-only direct-link
  token (from the intranet planning page) used to read course types; it grants
  nothing beyond what any student's browser already gets.

## Self-hosting

```bash
gcloud run deploy ade-to-ics \
  --source . \
  --function proxy_ade \
  --base-image python314 \
  --region europe-west9 \
  --allow-unauthenticated
```

Local run: `pip install -r requirements.txt && python main.py 1406,1196` →
writes `ADECal.ics` and prints the exams it flagged.

---

# How it works (and why it's built this way)

## The 2026 server migration

| | Host | Anonymous projects |
|---|---|---|
| ❌ old | `ade-planning.polytech.universite-paris-saclay.fr` (ADE 6.12) | `projectId=19` → 2025-2026 only |
| ✅ new | `planning.polytech.universite-paris-saclay.fr` (ADE 6.17) | `projectId=1` → 2025-2026, **`projectId=2` → 2026-2027** |

Any old subscription URL pointing at the old host returns an **empty** calendar
for 2026-2027. Resource ids were preserved in the migration.

## Broken TLS chain (since 2026-10-12)

The new host's renewed certificate (HARICA "GEANT TLS ECC 1") is served
**without its intermediate**. Browsers repair this silently, so ADE links keep
opening in Chrome, but Python `requests` fails with `unable to get local issuer
certificate` and the feed answers 503 "could not reach ADE". `ade_tls.py`
verifies against certifi's roots + the missing intermediate shipped in
`certs/`. If the school switches CA again: take the new intermediate from the
leaf's "CA Issuers" AIA URL (`openssl x509 -noout -ext authorityInfoAccess`)
and drop it into `certs/` as a PEM-encoded `*.crt` (not `*.pem`: `.gcloudignore`
excludes those, so it would silently not deploy).

## Name search removed (2026-11)

There used to be a `/search?q=` name → id lookup (and a "calendrier personnel"
box in the picker). It read the old host's anonymous `tree.jsp`; that host is
gone (TCP accepts, HTTP never answers), and the new host's `tree.jsp`
redirects to login. The GWT app's `searchResource` does work anonymously (see
the protocol notes below), but it returns **ids without names**, and getting
names needs `getChildren` with hand-built GXT config objects. Rather than ship
a half-working, upgrade-fragile search, the feature was removed. Group ids in
`catalog.json` are unaffected, and any id can still go in "Autres ressources".

## Pipeline

1. **One anonymous request** to the new server's native iCal export
   (`anonymous_cal.jsp` — a `firstDate`/`lastDate` window is **required** or
   ADE returns an empty file). Gives times (UTC), rooms, teachers, groups —
   but *not* the activity Type.
2. **Official Types via GWT-RPC replay** (`ade_gwt.py`, 5 requests, <0.5 s for
   a whole year): the legacy RSS + `eventInfo.jsp` route is dead on the new
   server (`jsp/rss` empty, `eventInfo.jsp` → login), but the `/direct`
   "Web Direct Planning" GWT app works anonymously through the intranet's
   `?data=` token (read-only user `visuweb`). We replay
   `login → loadProject → getLegends → getTimetable`; every returned
   "SquareEvent" embeds the same eventId the iCal UID encodes
   (`ADE` + hex("`<project>-<eventId>-<session>-<rep>")) plus display lines
   `[name, type, teacher…, room…, group…]` — an exact eventId → Type join.
   Types are cached in-memory 6 h; on failure the feed degrades to a
   name-based exam heuristic instead of breaking.
3. Re-emit a Google-friendly `VCALENDAR`: `Course (TYPE)` summaries, `Type:`
   in the description, `🚨` + `VALARM` on graded types (Contrôle / Épreuve /
   Examen / Partiel / Soutenance / Certification / Rattrapage).

## GWT-RPC protocol notes (hard-won, keep for posterity)

* `getLegends` is not just a read — it **sets the server-side selection**
  (resources + weeks). `getTimetable` serves whatever the last `getLegends`
  selected and NPEs if none was made. Select all piano weeks (0–52) in one
  `getLegends`, then a single `getTimetable` returns the entire year.
* Keep `getTimetable`'s own week list to ONE week — multi-week requests switch
  the response to an aggregate "Cumul" encoding the parser doesn't read.
* The permutation + serialization-policy hashes in `ade_gwt.py` belong to the
  deployed ADE build (6.17 r796, captured 2026-08-25). **An ADE upgrade will
  change them**; every call then fails cleanly and the feed continues without
  types. Re-capture them from browser devtools on any intranet direct link
  (X-GWT-Permutation header + 2nd `|`-field of any ServiceProxy POST body), or
  override via env vars `ADE_GWT_PERMUTATION`, `ADE_GWT_POLICY_DIRECT`,
  `ADE_GWT_POLICY_PLANNING`. The `?data=` token: `ADE_DIRECT_DATA`.
* Resource search (not used, recorded in case search is ever revived):
  `DirectPlanningServiceProxy.method12searchResource(J clientId, String q)`
  where `q` is ADE's own text format, not GWT's:
  `[1]{"StringField""NAME""""<text>""false""true""true""true""2147483647""false"[0]"CONTAINS""false""false""0"`.
  It answers with the matching tree paths as nested
  `{"<id>""<isFolder>""0""-1""-1""-1""0""false"[0]""[<n children>]…` nodes:
  ids only, no names (e.g. `APP5 INFO` → `-100 → -5 → 858 → 871 → 872 → 1259 →
  [1406, 1260, 1130, 1125, 1124]`).
  `method4getChildren` (which would give names) NPEs unless its
  `TreeResourceConfig` carries fully built GXT sub-objects.

## catalog.json

The full "Liste des groupes" tree (516 groups + 86 selectable folders across
PEIP / FISE / FISA / Externes / Groupes Enseignants), harvested from the legacy
host's anonymous tree (`tree.jsp` category5 walk — see git history for the
script). That legacy host is now dead, so a re-harvest would have to go
through the new host (login, or the GWT app). Ids are stable across the
migration, so the current file stays valid until the school restructures groups.

## Finding next year's `projectId`

The current year is the **highest** anonymous project id on the new host
(2025-26 = 1, 2026-27 = 2, so 2027-28 will be 3). Bump `DEFAULT_PROJECT_ID`
in `main.py`, re-harvest `catalog.json` if group ids changed, redeploy.

## Files

```
main.py            # Cloud Function: feed + /pick
ade_gwt.py         # GWT-RPC client for official ADE activity Types
ade_tls.py         # CA bundle = certifi + certs/*.crt (ADE omits its intermediate)
certs/             # missing intermediate cert(s) for the ADE host
catalog.json       # harvested group tree (no individuals)
requirements.txt   # functions-framework, requests
```
