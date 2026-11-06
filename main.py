"""
ADE -> ICS proxy  (Google Cloud Function / Cloud Run)
=====================================================

Polytech Paris-Saclay moved its timetable to a NEW ADE server in 2026:

    OLD  ade-planning.polytech.universite-paris-saclay.fr   (ADE 6.12, projectId 19 = 2025-2026 only)
    NEW  planning.polytech.universite-paris-saclay.fr       (ADE 6.17, projectId 2  = 2026-2027)

The old `anonymous_cal.jsp` URL kept returning an EMPTY calendar because the
2026-2027 timetable simply does not exist on the old server. The fix is to point
at the new host with the right projectId.

Good news: the new server's native iCal export works ANONYMOUSLY in a single
request and already carries clean start/end times (UTC), room and group info.
So the old, slow RSS + eventInfo.jsp + per-event Type fan-out is gone. We now:

  1. Fetch the native ICS in ONE request (a firstDate/lastDate window is REQUIRED,
     otherwise ADE returns an empty calendar).
  2. Recover the OFFICIAL ADE activity Type (CM / TD / C-TD / TP / Contrôle /
     Projet Encadré / ...) by replaying the /direct GWT-RPC planning app — see
     ade_gwt.py. Each ICS UID embeds the ADE eventId, and each GWT "square"
     carries the same id + its type, so matching is exact. Results are cached
     in-memory for 6 h; on any failure we serve the feed without types (with a
     name-based exam heuristic as fallback) rather than failing.
  3. Re-emit a Google-Calendar-friendly feed: "Course (TYPE)", and exams
     (Contrôle / Épreuve / Examen / Partiel / Soutenance / Certification ...)
     flagged 🚨 with a 30-minute alarm.

Resource ids: the full group tree (516 groups across PEIP / FISE / FISA /
Externes) was harvested from the ADE tree into catalog.json — ids were
preserved in the copy to the new server. Individual students are deliberately
NOT stored anywhere in this project; any other resource id can still be passed
by hand.

(There used to be a /search name -> id lookup. It relied on the legacy host's
anonymous tree, which died in late 2026; the new host only offers search
through a login or the reverse-engineered GWT app, which returns ids without
names. It was removed rather than kept half-working.)

Routes:
    /            the ICS feed (subscribe to this; `resources` is required —
                 a bare visit redirects to /pick)
    /pick        HTML picker: every Polytech group (from catalog.json) and a
                 subscription-URL builder

Query parameters (feed):
    resources   REQUIRED. Resource id, or comma-separated ids — the UNION of
                all their events, deduplicated (e.g. resources=1406,1196)
    not         Case-insensitive regex; events whose SUMMARY / DESCRIPTION /
                LOCATION matches are dropped. Use it to unselect what a
                resource brings in, e.g. not=APP5%20GrA (alias: exclude)
    name        Calendar display name                  (default: "ADE Polytech")
    projectId   ADE project id                         (default: 2  = 2026-2027)
    firstDate   YYYY-MM-DD window start                (default: academic-year start)
    lastDate    YYYY-MM-DD window end                  (default: next July 31)
    host        ADE host override                      (default: new server)

Deploy:
    gcloud run deploy ade-to-ics --source . --function proxy_ade \\
        --base-image python314 --region europe-west9 --allow-unauthenticated
"""

import datetime as _dt
import json
import logging
import os
import re
import time
import uuid

import functions_framework
import requests
from flask import Response
from requests.adapters import HTTPAdapter

from ade_tls import ca_bundle

try:
    import ade_gwt
except Exception:  # pragma: no cover - keep the feed alive even if the module breaks
    ade_gwt = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ade_ics_proxy")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_HOST = "planning.polytech.universite-paris-saclay.fr"
DEFAULT_PROJECT_ID = "2"     # 2026-2027 on the new server. Bump this each new year.

REQUEST_TIMEOUT = 20         # seconds
MAX_RETRIES = 4
RETRY_BACKOFF = 0.5          # 0.5, 1, 2, 4 ...

CAL_NAME = "ADE Polytech"

VALID_RESOURCES = re.compile(r"^\d+(,\d+)*$")   # "3018" or "3018,1044"
VALID_ID = re.compile(r"^\d+$")
VALID_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The full Polytech group tree (516 groups + selectable folders), harvested
# from the ADE tree into catalog.json — ids survived the 2026 server migration.
# Entries: {"id": int, "name": str, "kind": "group"|"folder", "path": str}.
# Individual students are deliberately NOT in this file (privacy).
def _load_catalog() -> list[dict]:
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        logger.warning("catalog.json not loadable (%s); /pick will be empty", exc)
        return []


CATALOG = _load_catalog()

# Name-based exam markers — FALLBACK ONLY, used when the official ADE type is
# unavailable. Word-ish boundaries keep precision high (e.g. "contr Qualité" is
# NOT flagged, only a real "Contrôle" / "Controle" is).
EXAM_RE = re.compile(
    r"(contr[oô]le|\bexam\b|examen|[ée]preuve|partiel|soutenance|"
    r"rattrapage|\btoeic\b|certification|\bqcm\b|[ée]valuation)",
    re.IGNORECASE,
)

# Official ADE types that count as graded / must-not-miss sessions.
EXAM_TYPE_RE = re.compile(
    r"(contr[oô]le|exam|[ée]preuve|partiel|rattrap|soutenance|certification)",
    re.IGNORECASE,
)

# In-memory cache of {eventId: official type} per (host, projectId, resources).
# Cloud Run keeps instances warm, so this typically survives between Google
# Calendar refreshes. Stale entries are reused when ADE's GWT backend fails.
TYPES_TTL = 6 * 3600
_types_cache: dict = {"key": None, "at": 0.0, "types": {}}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _session() -> requests.Session:
    s = requests.Session()
    s.verify = ca_bundle()  # ADE omits its TLS intermediate — see ade_tls.py
    adapter = HTTPAdapter(max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(
        {
            "User-Agent": "ade2ics/2.0 (+https://github.com/) calendar-sync",
            "Accept": "text/calendar, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        }
    )
    return s


def fetch_native_ics(
    session: requests.Session,
    host: str,
    resources: str,
    project_id: str,
    first_date: str,
    last_date: str,
    request_id: str,
) -> str:
    """Fetch ADE's native anonymous iCal export in a single request."""
    url = (
        f"https://{host}/jsp/custom/modules/plannings/anonymous_cal.jsp"
        f"?resources={resources}&projectId={project_id}&calType=ical"
        f"&firstDate={first_date}&lastDate={last_date}"
    )
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                return resp.text
            last_err = f"HTTP {resp.status_code}"
            logger.warning("[%s] ADE returned %s (attempt %d)", request_id, last_err, attempt + 1)
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            logger.warning("[%s] ADE request error %s (attempt %d)", request_id, last_err, attempt + 1)
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise RuntimeError(last_err or "unknown error")


# ---------------------------------------------------------------------------
# ICS parsing / re-emission
# ---------------------------------------------------------------------------
def _unfold(ics: str) -> list[str]:
    """Un-fold RFC 5545 continuation lines (CRLF + space/tab)."""
    raw = ics.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in raw:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_events(ics: str) -> list[dict]:
    """Split a VCALENDAR into a list of {property: value} event dicts."""
    events: list[dict] = []
    current: dict | None = None
    for line in _unfold(ics):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, val = line.split(":", 1)
            key = key.split(";", 1)[0]  # drop any params, e.g. DTSTART;TZID=
            current[key] = val
    return events


def event_id_of(uid: str) -> int | None:
    """ADE UIDs are `ADE` + hex("`<projectName>-<eventId>-<session>-<rep>")."""
    if not uid or not uid.startswith("ADE"):
        return None
    try:
        decoded = bytes.fromhex(uid[3:]).decode("latin-1")
    except ValueError:
        return None
    parts = decoded.split("-")
    if len(parts) >= 3 and parts[-3].isdigit():
        return int(parts[-3])
    return None


def get_official_types(
    host: str, project_id: str, resources: str, events: list[dict], request_id: str
) -> dict[int, str]:
    """Official ADE types via the GWT backend, cached 6 h, stale on error."""
    if ade_gwt is None:
        return {}
    key = (host, project_id, resources)
    now = time.time()
    if _types_cache["key"] == key and now - _types_cache["at"] < TYPES_TTL:
        return _types_cache["types"]

    try:
        types = ade_gwt.fetch_types(
            host,
            int(project_id),
            [int(r) for r in resources.split(",")],
        )
        _types_cache.update({"key": key, "at": now, "types": types})
        logger.info("[%s] GWT types: %d activities", request_id, len(types))
        return types
    except Exception as exc:  # noqa: BLE001 - types are best-effort
        logger.warning("[%s] GWT type fetch failed (%s); %s", request_id, exc,
                       "serving stale types" if _types_cache["key"] == key else "no types")
        return _types_cache["types"] if _types_cache["key"] == key else {}


def _fold(line: str) -> str:
    """Fold a line to <=75 octets per RFC 5545 (byte-aware)."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    chunks, start = [], 0
    limit = 75
    while start < len(data):
        end = min(start + limit, len(data))
        # don't split a multi-byte char
        while end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(data[start:end])
        start = end
        limit = 74  # continuation lines start with a leading space
    return "\r\n ".join(c.decode("utf-8") for c in chunks)


def _esc(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def is_exam(summary: str) -> bool:
    return bool(EXAM_RE.search(summary or ""))


def build_calendar(
    events: list[dict],
    types: dict[int, str] | None = None,
    cal_name: str = CAL_NAME,
) -> tuple[str, int]:
    """Re-emit events as a clean, Google-friendly VCALENDAR. Returns (ics, exam_count)."""
    types = types or {}
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ade2ics//Polytech ADE proxy//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{cal_name}",
        "X-WR-TIMEZONE:Europe/Paris",
        "X-PUBLISHED-TTL:PT6H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]
    exam_count = 0
    for ev in events:
        dtstart = ev.get("DTSTART")
        dtend = ev.get("DTEND")
        summary = ev.get("SUMMARY", "").strip()
        if not dtstart or not summary:
            continue

        uid = ev.get("UID") or f"{abs(hash(dtstart + summary))}@ade2ics"

        # Official ADE type when we have it; name-based heuristic otherwise.
        ev_type = types.get(event_id_of(uid) or -1, "")
        if ev_type:
            exam = bool(EXAM_TYPE_RE.search(ev_type))
            summary = f"{summary} ({ev_type})"
        else:
            exam = is_exam(summary)
        if exam:
            exam_count += 1
            summary = f"🚨 {summary}"

        out = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now}", f"DTSTART:{dtstart}"]
        if dtend:
            out.append(f"DTEND:{dtend}")
        out.append(f"SUMMARY:{_esc(summary)}")
        if ev.get("LOCATION"):
            out.append(f"LOCATION:{_esc(ev['LOCATION'])}")
        if ev.get("DESCRIPTION") or ev_type:
            # ADE's description already uses literal "\n"; keep it, just re-escape commas/semicolons safely
            desc = ev.get("DESCRIPTION", "").replace("\\n", "\n")
            if ev_type:
                desc = f"Type: {ev_type}\n{desc}"
            out.append(f"DESCRIPTION:{_esc(desc)}")
        if exam:
            out.append("CATEGORIES:EXAM")
            # 30-minute heads-up for graded sessions
            out += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                "TRIGGER:-PT30M",
                f"DESCRIPTION:{_esc(summary)}",
                "END:VALARM",
            ]
        out.append("STATUS:CONFIRMED")
        out.append("END:VEVENT")
        lines.extend(out)

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n", exam_count


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def default_window(today: _dt.date | None = None) -> tuple[str, str]:
    """Academic-year window: Aug 1 of the current school year -> Jul 31 of the next."""
    today = today or _dt.date.today()
    start_year = today.year if today.month >= 8 else today.year - 1
    return f"{start_year}-08-01", f"{start_year + 1}-07-31"


# ---------------------------------------------------------------------------
# /pick — subscription URL builder
# ---------------------------------------------------------------------------
def _picker_html() -> str:
    catalog_json = json.dumps(CATALOG, ensure_ascii=False)
    return """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polytech ADE → Google Calendar</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:780px;margin:2rem auto;padding:0 1rem;color:#222}
 h1{font-size:1.4rem} h2{font-size:1rem;margin:1.2rem 0 .3rem;color:#555}
 label{display:block;padding:.12rem 0 .12rem 1rem;cursor:pointer}
 code{background:#f4f4f4;padding:.1rem .3rem;border-radius:4px}
 .id{color:#999;font-size:.8em} .sub{color:#888;font-size:.8em;margin:.4rem 0 .1rem .5rem}
 input[type=text]{width:100%;padding:.4rem;margin:.2rem 0;box-sizing:border-box}
 textarea{width:100%;height:4.5rem;font-family:monospace;font-size:.85em;box-sizing:border-box}
 button{padding:.4rem .9rem;margin:.3rem .3rem .3rem 0;cursor:pointer}
 .hint{color:#777;font-size:.85em}
 details{border:1px solid #e5e5e5;border-radius:8px;padding:.3rem .6rem;margin:.35rem 0}
 summary{cursor:pointer;font-weight:600}
</style></head><body>
<h1>📅 Polytech ADE → Google Calendar</h1>
<p class="hint">Coche tes groupes → copie l'URL →
 Google Calendar → <b>Autres agendas ＋ → À partir de l'URL</b>.
 Les cours arrivent typés (CM/TD/TP…) et les contrôles flagués 🚨.</p>
<h2>Filtrer les groupes</h2>
<input type="text" id="filter" placeholder="ex: APP5, ET3 info, PEIP2 anglais…">
<div id="cat"></div>
<h2>Autres ressources (ids séparés par des virgules)</h2>
<input type="text" id="extra" placeholder="ex: 2464">
<h2>Exclure des événements (regex, optionnel)</h2>
<input type="text" id="not" placeholder="ex: APP5 GrA — retire les cours d'un groupe en trop">
<h2>Nom de l'agenda</h2>
<input type="text" id="name" placeholder="ADE Polytech">
<h2>Ton URL d'abonnement</h2>
<textarea id="url" readonly></textarea>
<button id="copy">Copier</button>
<a id="test" target="_blank"><button>Tester le flux</button></a>
<script>
const CATALOG = __CATALOG__;
const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const cat = document.getElementById('cat');
const secs = new Map();
for (const e of CATALOG) {
  const top = e.path ? e.path.split(' / ')[0] : e.name;
  if (!secs.has(top)) secs.set(top, []);
  secs.get(top).push(e);
}
for (const [top, items] of secs) {
  const d = document.createElement('details');
  const n = items.filter(i => i.kind === 'group').length;
  d.innerHTML = `<summary>${esc(top)} <span class="id">${n} groupes</span></summary>`;
  let lastPath = null;
  for (const it of items) {
    if (it.path && it.path !== lastPath) {
      const h = document.createElement('div'); h.className = 'sub';
      h.textContent = it.path; h.dataset.q = it.path.toLowerCase();
      d.appendChild(h); lastPath = it.path;
    }
    const l = document.createElement('label');
    l.dataset.q = (it.name + ' ' + it.path).toLowerCase();
    l.innerHTML = `<input type="checkbox" value="${it.id}"> ` +
      (it.kind === 'folder' ? '📁 ' : '') + esc(it.name) +
      (it.kind === 'folder' ? ' <span class="id">(tout le dossier)</span>' : '') +
      ` <span class="id">#${it.id}</span>`;
    d.appendChild(l);
  }
  cat.appendChild(d);
}
document.getElementById('filter').addEventListener('input', ev => {
  const q = ev.target.value.trim().toLowerCase();
  for (const d of cat.querySelectorAll('details')) {
    let any = false;
    for (const el of d.querySelectorAll('label,.sub')) {
      const hit = !q || el.dataset.q.includes(q);
      el.style.display = hit ? '' : 'none';
      if (hit && el.tagName === 'LABEL') any = true;
    }
    d.style.display = (!q || any) ? '' : 'none';
    d.open = q ? any : false;
  }
});
function build() {
  const ids = [...cat.querySelectorAll('input[type=checkbox]:checked')].map(c => c.value);
  const extra = document.getElementById('extra').value.split(',').map(s => s.trim()).filter(s => /^\\d+$/.test(s));
  const all = [...new Set([...ids, ...extra])];
  const p = new URLSearchParams();
  if (all.length) p.set('resources', all.join(','));
  const nt = document.getElementById('not').value.trim(); if (nt) p.set('not', nt);
  const nm = document.getElementById('name').value.trim(); if (nm) p.set('name', nm);
  const url = location.origin + '/?' + p.toString();
  document.getElementById('url').value = all.length ? url : '(sélectionne au moins un groupe)';
  document.getElementById('test').href = all.length ? url : '#';
}
cat.addEventListener('change', build);
for (const id of ['extra','not','name']) document.getElementById(id).addEventListener('input', build);
document.getElementById('copy').onclick = () =>
  navigator.clipboard.writeText(document.getElementById('url').value);
build();
</script></body></html>""".replace("__CATALOG__", catalog_json)


# ---------------------------------------------------------------------------
# Cloud Function entry point
# ---------------------------------------------------------------------------
@functions_framework.http
def proxy_ade(request):
    request_id = uuid.uuid4().hex[:8]
    start = time.time()

    # --- auxiliary routes -------------------------------------------------
    if request.path.rstrip("/") == "/pick":
        return Response(_picker_html(), content_type="text/html; charset=utf-8")

    # No resource selection -> send humans to the picker.
    if not (request.args.get("resources") or "").strip():
        return Response("", status=302, headers={"Location": "/pick"})

    host = (request.args.get("host") or DEFAULT_HOST).strip()
    resources = request.args.get("resources").strip()
    project_id = (request.args.get("projectId") or DEFAULT_PROJECT_ID).strip()
    d_first, d_last = default_window()
    first_date = (request.args.get("firstDate") or d_first).strip()
    last_date = (request.args.get("lastDate") or d_last).strip()
    not_pattern = (request.args.get("not") or request.args.get("exclude") or "").strip()
    cal_name = re.sub(r"[\x00-\x1f]", "", (request.args.get("name") or CAL_NAME))[:60]

    # Validation
    problems = []
    if not VALID_RESOURCES.match(resources):
        problems.append("resources must be numeric (comma-separated allowed)")
    if not VALID_ID.match(project_id):
        problems.append("projectId must be numeric")
    if not VALID_DATE.match(first_date):
        problems.append("firstDate must be YYYY-MM-DD")
    if not VALID_DATE.match(last_date):
        problems.append("lastDate must be YYYY-MM-DD")
    if not re.match(r"^[A-Za-z0-9.\-]+$", host):
        problems.append("invalid host")
    not_re = None
    if not_pattern:
        if len(not_pattern) > 200:
            problems.append("'not' too long (max 200 chars)")
        else:
            try:
                not_re = re.compile(not_pattern, re.IGNORECASE)
            except re.error as exc:
                problems.append(f"'not' is not a valid regex: {exc}")
    if problems:
        return Response("Bad Request: " + "; ".join(problems), status=400, content_type="text/plain")

    logger.info(
        "[%s] START host=%s resources=%s projectId=%s window=%s..%s",
        request_id, host, resources, project_id, first_date, last_date,
    )

    session = _session()
    try:
        raw = fetch_native_ics(session, host, resources, project_id, first_date, last_date, request_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] ADE fetch failed: %s", request_id, exc)
        return Response(
            "Error: could not reach ADE. It may be temporarily unavailable.",
            status=503,
            content_type="text/plain",
        )

    events = parse_events(raw)

    # Deduplicate by UID (defensive — ADE already unions server-side).
    seen_uids: set[str] = set()
    deduped = []
    for ev in events:
        uid = ev.get("UID")
        if uid and uid in seen_uids:
            continue
        if uid:
            seen_uids.add(uid)
        deduped.append(ev)
    events = deduped

    # Apply the `not` exclusion filter (summary + description + location).
    if not_re:
        before = len(events)
        events = [
            ev for ev in events
            if not not_re.search(
                f"{ev.get('SUMMARY', '')}\n{ev.get('DESCRIPTION', '')}\n{ev.get('LOCATION', '')}"
            )
        ]
        logger.info(
            "[%s] 'not=%s' filtered %d/%d events",
            request_id, not_pattern, before - len(events), before,
        )

    if not events:
        logger.error(
            "[%s] 0 events (raw %d bytes). Wrong projectId/host, or the date "
            "window has no events?", request_id, len(raw),
        )
        return Response(
            "Error: ADE returned an empty calendar. Check projectId "
            f"(currently {project_id}; 2026-2027 = 2 on {DEFAULT_HOST}) and that "
            "firstDate/lastDate cover the school year.",
            status=502,
            content_type="text/plain",
        )

    types = get_official_types(host, project_id, resources, events, request_id)
    ics, exam_count = build_calendar(events, types, cal_name=cal_name)
    logger.info(
        "[%s] DONE %d events (%d exams, %d typed activities) in %.2fs, %d bytes",
        request_id, len(events), exam_count, len(types), time.time() - start, len(ics),
    )

    return Response(
        ics,
        status=200,
        content_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="ADECal.ics"',
            "Cache-Control": "public, max-age=3600",
        },
    )


# ---------------------------------------------------------------------------
# Local dev:  python main.py  -> writes ADECal.ics and prints a summary
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python main.py <resources> [projectId]   e.g. python main.py 1406,1196")
        sys.exit(1)
    res = sys.argv[1]
    pid = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PROJECT_ID
    f, l = default_window()
    sess = _session()
    raw = fetch_native_ics(sess, DEFAULT_HOST, res, pid, f, l, "local")
    evs = parse_events(raw)
    types = get_official_types(DEFAULT_HOST, pid, res, evs, "local")
    ics, exams = build_calendar(evs, types)
    with open("ADECal.ics", "w", encoding="utf-8", newline="") as fh:
        fh.write(ics)
    print(f"host={DEFAULT_HOST} resources={res} projectId={pid} window={f}..{l}")
    print(f"{len(evs)} events, {len(types)} typed activities, {exams} exams -> ADECal.ics ({len(ics)} bytes)")
    typed = sum(1 for ev in evs if types.get(event_id_of(ev.get('UID', '')) or -1))
    print(f"events with official type: {typed}/{len(evs)}")
    for ev in evs:
        eid = event_id_of(ev.get("UID", "")) or -1
        t = types.get(eid, "")
        s = ev.get("SUMMARY", "")
        if (t and EXAM_TYPE_RE.search(t)) or (not t and is_exam(s)):
            print(f"  EXAM: {ev.get('DTSTART','')} {s} [{t or 'name-based'}]")
