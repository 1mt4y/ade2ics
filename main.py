"""
ADE RSS to ICS - Google Cloud Function
=======================================

This function:
1. Fetches RSS feeds from ADE (resources=3018 and resources=1479)
2. Filters out "Anglais" from 3018, keeps only "Anglais" from 1479
3. Combines feeds, calls eventInfo.jsp to get "Type" field
4. Returns a proper ICS calendar

Query Parameters:
- resources: Main resource ID (default: 3018)
- englishResources: English group resource ID (default: 1479)
- projectId: ADE project ID (default: 19)

Deployment:
    gcloud run deploy ade-to-ics --source . --function proxy_ade \\
        --base-image python314 --region europe-west9 --allow-unauthenticated
"""

import functions_framework
import requests
import re
import concurrent.futures
import time
import logging
import xml.etree.ElementTree as ET
import uuid
from html import unescape
from collections import Counter
from flask import Response
from requests.adapters import HTTPAdapter
from datetime import datetime
import pytz

# Configure logging with more detail for Cloud Run
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ade_rss_proxy")

# Constants
MAX_RETRIES = 5
RETRY_DELAY = 0.5  # seconds, will double each retry (0.5, 1, 2, 4, 8)
REQUEST_TIMEOUT = 10  # seconds for RSS requests
EVENT_INFO_TIMEOUT = 5  # seconds for eventInfo.jsp requests
MAX_WORKERS = 20  # parallel workers for type fetching

BASE_URL = "https://ade-planning.polytech.universite-paris-saclay.fr/jsp"
PLANNING_BASE = f"{BASE_URL}/custom/modules/plannings"

# Valid resource ID pattern (numeric only)
VALID_RESOURCE_PATTERN = re.compile(r"^\d+$")

# Compile regex patterns for parsing
TYPE_PATTERN = re.compile(r"<label>Type\s*:\s*([^<]+)</label>")
RESOURCES_PATTERN = re.compile(r"<b>Ressources</b>(.*)|<b>Resources</b>(.*)", re.DOTALL)
DATE_TIME_PATTERN = re.compile(
    r"<p>(\d{2}/\d{2}/\d{4})\s+(\d+h\d+)\s*-\s*(\d+h\d+)</p>"
)
ROOM_PATTERN = re.compile(r"\d{3}\s*-\s*[A-Za-z0-9]+")
TIME_PATTERN = re.compile(r"(\d+)h(\d+)")


def validate_resources(value: str, param_name: str) -> tuple[bool, str]:
    """Validate resource ID parameter."""
    if not value:
        return False, f"{param_name} cannot be empty"
    if not VALID_RESOURCE_PATTERN.match(value):
        return False, f"{param_name} must be numeric (got: {value})"
    if len(value) > 10:
        return False, f"{param_name} too long (max 10 digits)"
    return True, ""


def create_session():
    """Create optimized requests session with connection pooling."""
    session = requests.Session()

    adapter = HTTPAdapter(
        pool_connections=MAX_WORKERS,
        pool_maxsize=MAX_WORKERS,
        max_retries=2,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            "Connection": "keep-alive",
        }
    )

    return session


def init_ade_session(session, resources: str, project_id: str, request_id: str):
    """Initialize ADE session by visiting required pages with retry logic."""
    urls = [
        f"{BASE_URL}/",
        f"{BASE_URL}/standard/gui/interface.jsp",
        f"{PLANNING_BASE}/anonymous_cal.jsp?resources={resources}&projectId={project_id}&calType=ical",
    ]

    for url in urls:
        for attempt in range(MAX_RETRIES):
            try:
                resp = session.get(url, timeout=REQUEST_TIMEOUT)
                if resp.status_code >= 400:
                    logger.warning(
                        f"[{request_id}] Session init got HTTP {resp.status_code} for {url}"
                    )
                break
            except requests.Timeout as e:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_DELAY * (2**attempt)
                    logger.warning(
                        f"[{request_id}] Session init timeout ({url}), retry {attempt + 1}/{MAX_RETRIES} in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        f"[{request_id}] Session init failed after {MAX_RETRIES} attempts (timeout): {url}"
                    )
            except requests.RequestException as e:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_DELAY * (2**attempt)
                    logger.warning(
                        f"[{request_id}] Session init error ({type(e).__name__}), retry {attempt + 1}/{MAX_RETRIES}"
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        f"[{request_id}] Session init failed after {MAX_RETRIES} attempts: {e}"
                    )

    logger.info(f"[{request_id}] Session initialized for resources={resources}")


def parse_rss_xml(xml_content: str, request_id: str) -> list[dict]:
    """Parse RSS XML content and extract event information."""
    if not xml_content or len(xml_content) < 100:
        logger.warning(
            f"[{request_id}] RSS content too short ({len(xml_content)} bytes)"
        )
        return []

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        logger.error(f"[{request_id}] XML parse error: {e}")
        # Log first 500 chars for debugging
        logger.debug(f"[{request_id}] XML content preview: {xml_content[:500]}")
        return []

    events = []
    parse_errors = []

    items = root.findall(".//item")
    if not items:
        logger.warning(f"[{request_id}] No <item> elements found in RSS")
        return []

    for item in items:
        try:
            guid_elem = item.find("guid")
            title_elem = item.find("title")
            desc_elem = item.find("description")

            if guid_elem is None or guid_elem.text is None:
                parse_errors.append("Missing GUID")
                continue
            if title_elem is None or title_elem.text is None:
                parse_errors.append(f"Missing title for GUID {guid_elem.text}")
                continue

            guid = guid_elem.text.strip()
            title = title_elem.text.strip()

            description = ""
            if desc_elem is not None:
                description = desc_elem.text or "".join(desc_elem.itertext())
            description = unescape(unescape(description or ""))

            # Parse date/time: "<p>30/03/2026 10h30 - 12h30</p>"
            date_time_match = DATE_TIME_PATTERN.search(description)

            if not date_time_match:
                parse_errors.append(f"No date/time for '{title}' (GUID: {guid})")
                continue

            date_str = date_time_match.group(1)
            start_time = date_time_match.group(2)
            end_time = date_time_match.group(3)

            day, month, year = date_str.split("/")
            start_match = TIME_PATTERN.match(start_time)
            end_match = TIME_PATTERN.match(end_time)

            if not start_match or not end_match:
                parse_errors.append(f"Invalid time format for '{title}'")
                continue

            start_h, start_m = start_match.groups()
            end_h, end_m = end_match.groups()

            start_dt = datetime(
                int(year), int(month), int(day), int(start_h), int(start_m)
            )
            end_dt = datetime(int(year), int(month), int(day), int(end_h), int(end_m))

            # Extract resources
            resources = []
            location = ""
            professor = ""

            resources_match = RESOURCES_PATTERN.search(description)
            if resources_match:
                resources_section = resources_match.group(1) or resources_match.group(2)
                resources_section = re.sub(r"^\s*<br\s*/?>", "", resources_section)
                resources_section = re.sub(
                    r"</p>.*", "", resources_section, flags=re.DOTALL
                )
                resources_section = resources_section.replace("]]>", "").strip()
                raw_resources = [
                    r.strip()
                    for r in re.split(r"<br\s*/?>", resources_section)
                    if r.strip()
                ]

                for r in raw_resources:
                    resources.append(r)
                    # Room pattern: "620 - B016"
                    if ROOM_PATTERN.search(r):
                        location = r
                    # Professor heuristic: no digits, not group labels, has "Firstname Lastname" pattern
                    if not professor:
                        upper_r = r.upper()
                        excluded_keywords = {
                            "APP",
                            "ET",
                            "FC",
                            "INFO",
                            "PROMO",
                            "GROUPE",
                            "GROUP",
                        }
                        if (
                            not re.search(r"\d", r)
                            and not any(kw in upper_r for kw in excluded_keywords)
                            and re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ].+\s+[A-Za-zÀ-ÖØ-öø-ÿ]", r)
                        ):
                            professor = r

            # Fallback for location
            if not location and resources:
                location = resources[-1]
            if not location:
                loc_match = ROOM_PATTERN.search(description)
                if loc_match:
                    location = loc_match.group(0).strip()

            events.append(
                {
                    "guid": guid,
                    "title": title,
                    "start": start_dt,
                    "end": end_dt,
                    "resources": resources,
                    "location": location,
                    "professor": professor,
                    "type": None,
                }
            )
        except ValueError as e:
            parse_errors.append(f"Value error parsing event: {e}")
            continue
        except Exception as e:
            parse_errors.append(f"Unexpected error: {type(e).__name__}: {e}")
            continue

    # Log parsing summary
    if parse_errors:
        logger.warning(
            f"[{request_id}] RSS parsing: {len(events)} ok, {len(parse_errors)} errors"
        )
        # Log first few errors
        for err in parse_errors[:5]:
            logger.debug(f"[{request_id}] Parse error: {err}")
        if len(parse_errors) > 5:
            logger.debug(f"[{request_id}] ... and {len(parse_errors) - 5} more errors")

    return events


def fetch_rss_events(session, resources: int, request_id: str) -> list[dict]:
    """Fetch RSS feed for a resource and parse events."""
    rss_url = f"{BASE_URL}/rss?projectId=19&resources={resources}&nbDays=999"

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(rss_url, timeout=REQUEST_TIMEOUT)

            if resp.status_code != 200:
                logger.warning(
                    f"[{request_id}] RSS {resources} returned HTTP {resp.status_code}"
                )
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (2**attempt))
                    continue
                return []

            resp.encoding = "utf-8"
            content_length = len(resp.text)

            if content_length < 100:
                logger.warning(
                    f"[{request_id}] RSS {resources} suspiciously short ({content_length} bytes)"
                )

            events = parse_rss_xml(resp.text, request_id)
            logger.debug(
                f"[{request_id}] RSS {resources}: {content_length} bytes → {len(events)} events"
            )
            return events

        except requests.Timeout:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY * (2**attempt)
                logger.warning(
                    f"[{request_id}] RSS {resources} timeout, retry {attempt + 1}/{MAX_RETRIES} in {wait}s"
                )
                time.sleep(wait)
            else:
                logger.error(
                    f"[{request_id}] RSS {resources} failed after {MAX_RETRIES} attempts (timeout)"
                )
                return []
        except requests.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY * (2**attempt)
                logger.warning(
                    f"[{request_id}] RSS {resources} error ({type(e).__name__}), retry {attempt + 1}/{MAX_RETRIES}"
                )
                time.sleep(wait)
            else:
                logger.error(
                    f"[{request_id}] RSS {resources} failed after {MAX_RETRIES} attempts: {e}"
                )
                return []

    return []


def fetch_event_type(event_id: str, session, request_id: str) -> tuple[str, str]:
    """Fetch event type from eventInfo.jsp with retry logic."""
    url = f"{PLANNING_BASE}/eventInfo.jsp?eventId={event_id}"

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=EVENT_INFO_TIMEOUT)

            if resp.status_code != 200:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (2**attempt))
                    continue
                logger.debug(
                    f"[{request_id}] eventInfo {event_id}: HTTP {resp.status_code}"
                )
                return event_id, ""

            html = resp.text

            # Check for session redirect (ADE kicks us back to login)
            if "top.document.location='index.jsp'" in html or len(html) < 500:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (2**attempt))
                    continue
                logger.debug(
                    f"[{request_id}] eventInfo {event_id}: session expired or invalid response"
                )
                return event_id, ""

            type_match = TYPE_PATTERN.search(html)
            if type_match:
                return event_id, type_match.group(1).strip()

            # Type not found in HTML (might be a valid event without type)
            return event_id, ""

        except requests.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (2**attempt))
            else:
                logger.debug(f"[{request_id}] eventInfo {event_id}: timeout")
                return event_id, ""
        except requests.RequestException:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (2**attempt))
            else:
                return event_id, ""

    return event_id, ""


def build_ics_calendar(events: list[dict]) -> str:
    """Build ICS calendar string from events."""
    tz = pytz.timezone("Europe/Paris")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ADE RSS to ICS Proxy//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:ADE Planning",
    ]

    for ev in events:
        # Localize and convert to UTC
        start_dt = tz.localize(ev["start"])
        end_dt = tz.localize(ev["end"])
        start_utc = start_dt.astimezone(pytz.UTC)
        end_utc = end_dt.astimezone(pytz.UTC)

        # Format timestamps
        dtstart = start_utc.strftime("%Y%m%dT%H%M%SZ")
        dtend = end_utc.strftime("%Y%m%dT%H%M%SZ")

        # Build summary
        summary = ev["title"]
        event_type = (ev.get("type") or "").strip()
        event_type_lower = event_type.lower()

        if event_type_lower in {"contrôle", "controle"}:
            summary = f"🚨 [CONTROLE] {summary}"
        elif event_type:
            summary = f"{summary} ({event_type})"

        # Escape special chars for ICS
        summary = summary.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")

        # Build description
        desc_parts = [f"Event ID: {ev['guid']}"]
        if ev.get("type"):
            desc_parts.append(f"Type: {ev['type']}")
        if ev.get("professor"):
            desc_parts.append(f"Professor: {ev['professor']}")
        if ev.get("resources"):
            desc_parts.append(f"Resources: {', '.join(ev['resources'])}")
        description = "\\n".join(desc_parts)

        # Location
        location = ev.get("location", "")
        if location:
            location = (
                location.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
            )

        lines.append("BEGIN:VEVENT")
        lines.append(
            f"UID:{ev['guid']}@ade-planning.polytech.universite-paris-saclay.fr"
        )
        lines.append(f"DTSTART:{dtstart}")
        lines.append(f"DTEND:{dtend}")
        lines.append(f"SUMMARY:{summary}")
        lines.append(f"DESCRIPTION:{description}")
        if location:
            lines.append(f"LOCATION:{location}")
        lines.append("STATUS:CONFIRMED")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    return "\r\n".join(lines)


@functions_framework.http
def proxy_ade(request):
    """
    Google Cloud Function entry point.

    Query Parameters:
        - resources: Main resource ID (default: 3018)
        - englishResources: English group resource ID (default: 1479)
        - projectId: ADE project ID (default: 19)

    Returns:
        ICS calendar file with Content-Type: text/calendar
    """
    start_time = time.time()
    request_id = str(uuid.uuid4())[:8]  # Short unique ID for log correlation

    # Parse and validate parameters
    main_resources = request.args.get("resources", "3018").strip()
    english_resources = request.args.get("englishResources", "1479").strip()
    project_id = request.args.get("projectId", "19").strip()

    # Validate inputs
    for param_name, param_value in [
        ("resources", main_resources),
        ("englishResources", english_resources),
        ("projectId", project_id),
    ]:
        valid, error = validate_resources(param_value, param_name)
        if not valid:
            logger.warning(f"[{request_id}] Invalid parameter: {error}")
            return Response(
                f"Bad Request: {error}",
                status=400,
                content_type="text/plain",
            )

    logger.info(
        f"[{request_id}] START - resources={main_resources}, english={english_resources}, projectId={project_id}"
    )

    # Create session
    session = create_session()

    try:
        # Phase 1: Initialize session and fetch main RSS
        phase1_start = time.time()
        init_ade_session(session, main_resources, project_id, request_id)
        events_main = fetch_rss_events(session, int(main_resources), request_id)

        if not events_main:
            logger.error(
                f"[{request_id}] No events from main RSS (resources={main_resources})"
            )
            return Response(
                "Error: Failed to fetch calendar data. ADE may be unavailable.",
                status=503,
                content_type="text/plain",
            )

        # Phase 1b: Check Anglais events for "Contrôle" type BEFORE removing
        anglais_events = [
            ev for ev in events_main if ev.get("title", "").strip() == "Anglais"
        ]
        non_anglais_events = [
            ev for ev in events_main if ev.get("title", "").strip() != "Anglais"
        ]
        
        # Fetch types for Anglais events to check for exams
        anglais_to_keep = []
        anglais_to_remove = []
        
        if anglais_events:
            logger.info(
                f"[{request_id}] Checking {len(anglais_events)} Anglais events for Contrôle type..."
            )
            
            # Fetch types for all Anglais events in parallel
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(fetch_event_type, ev["guid"], session, request_id): ev
                    for ev in anglais_events
                }
                
                for future in concurrent.futures.as_completed(futures):
                    ev = futures[future]
                    try:
                        _, ev_type = future.result()
                        ev["type"] = ev_type
                        
                        # Keep Anglais if it's a Contrôle (exam)
                        if ev_type.lower() in {"contrôle", "controle"}:
                            anglais_to_keep.append(ev)
                            logger.info(
                                f"[{request_id}] KEEPING Anglais Contrôle: {ev['guid']} on {ev['start']}"
                            )
                        else:
                            anglais_to_remove.append(ev)
                    except Exception as e:
                        logger.warning(
                            f"[{request_id}] Error checking Anglais {ev['guid']}: {e}"
                        )
                        anglais_to_remove.append(ev)  # Remove if we can't check
        
        # Build filtered main events: non-Anglais + Anglais Contrôles only
        filtered_main = non_anglais_events + anglais_to_keep
        
        logger.info(
            f"[{request_id}] RSS {main_resources}: {len(events_main)} events, "
            f"kept {len(anglais_to_keep)} Anglais Contrôle, "
            f"removed {len(anglais_to_remove)} other Anglais → {len(filtered_main)} remaining"
        )
        phase1_duration = round(time.time() - phase1_start, 2)

        # Phase 2: Initialize session and fetch English RSS
        phase2_start = time.time()
        # init_ade_session(session, english_resources, project_id, request_id)
        events_english = fetch_rss_events(session, int(english_resources), request_id)

        # Keep only non-Contrôle Anglais from English RSS (to avoid duplicating exams)
        filtered_english = []
        for ev in events_english:
            if ev.get("title", "").strip() == "Anglais":
                filtered_english.append(ev)
        
        logger.info(
            f"[{request_id}] RSS {english_resources}: {len(events_english)} events, "
            f"kept {len(filtered_english)} Anglais only"
        )
        phase2_duration = round(time.time() - phase2_start, 2)

        # Phase 3: Combine and deduplicate
        combined = {}
        duplicates = 0
        for ev in filtered_main + filtered_english:
            guid = ev.get("guid")
            if guid:
                if guid in combined:
                    duplicates += 1
                else:
                    combined[guid] = ev

        events = list(combined.values())
        if duplicates > 0:
            logger.info(f"[{request_id}] Deduplicated {duplicates} events")
        logger.info(f"[{request_id}] Combined total: {len(events)} unique events")

        # Phase 4: Fetch types in parallel (skip events that already have types)
        phase4_start = time.time()
        type_map = {}
        
        # Events that need type fetching (exclude already-typed Anglais Contrôles)
        events_needing_types = [ev for ev in events if not ev.get("type")]
        events_with_types = [ev for ev in events if ev.get("type")]
        
        if events_with_types:
            logger.info(
                f"[{request_id}] Skipping type fetch for {len(events_with_types)} already-typed events"
            )
            for ev in events_with_types:
                type_map[ev["guid"]] = ev["type"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(fetch_event_type, ev["guid"], session, request_id): ev[
                    "guid"
                ]
                for ev in events_needing_types
            }

            for future in concurrent.futures.as_completed(futures):
                try:
                    eid, e_type = future.result()
                    type_map[eid] = e_type
                except Exception as e:
                    eid = futures[future]
                    logger.warning(f"[{request_id}] Type fetch error for {eid}: {e}")
                    type_map[eid] = ""

        # Apply types and collect statistics
        types_found = 0
        missing_types = []
        type_counts = Counter()

        for ev in events:
            ev_type = type_map.get(ev["guid"], "")
            ev["type"] = ev_type
            if ev_type:
                types_found += 1
                type_counts[ev_type] += 1
            else:
                missing_types.append(f"{ev['guid']}: {ev['title']}")

        phase4_duration = round(time.time() - phase4_start, 2)

        # Log type statistics
        logger.info(f"[{request_id}] Types found: {types_found}/{len(events)}")

        if type_counts:
            # Log type distribution
            type_dist = ", ".join(f"{t}:{c}" for t, c in type_counts.most_common(10))
            logger.info(f"[{request_id}] Type distribution: {type_dist}")

        if missing_types:
            logger.warning(
                f"[{request_id}] Missing types ({len(missing_types)}): "
                f"{missing_types[:5]}{'...' if len(missing_types) > 5 else ''}"
            )

        # Phase 5: Build ICS
        ics_content = build_ics_calendar(events)
        ics_size = len(ics_content)

        # Final timing summary
        total_duration = round(time.time() - start_time, 2)
        logger.info(
            f"[{request_id}] COMPLETE in {total_duration}s "
            f"(init+main:{phase1_duration}s, english:{phase2_duration}s, types:{phase4_duration}s) "
            f"- {len(events)} events, {ics_size} bytes"
        )

        # Return with proper headers
        return Response(
            ics_content,
            mimetype="text/calendar",
            headers={
                "Content-Disposition": "inline; filename=ade-planning.ics",
                "Cache-Control": "public, max-age=3600",  # Cache for 1 hour
                "X-Request-Id": request_id,
                "X-Events-Count": str(len(events)),
            },
        )

    except Exception as e:
        logger.exception(f"[{request_id}] Unhandled error: {e}")
        return Response(
            f"Internal Server Error. Request ID: {request_id}",
            status=500,
            content_type="text/plain",
        )

    finally:
        session.close()


# For local testing
if __name__ == "__main__":
    from flask import Flask, request as flask_request

    app = Flask(__name__)

    @app.route("/")
    def index():
        return proxy_ade(flask_request)

    @app.route("/health")
    def health():
        """Health check endpoint for load balancers."""
        return Response("OK", status=200, content_type="text/plain")

    print("=" * 60)
    print("ADE RSS to ICS Proxy - Local Development Server")
    print("=" * 60)
    print("Endpoints:")
    print("  GET /              - Main calendar endpoint")
    print("  GET /health        - Health check")
    print("")
    print("Parameters:")
    print("  ?resources=3018    - Main resource ID")
    print("  ?englishResources=1479 - English group resource ID")
    print("  ?projectId=19      - ADE project ID")
    print("=" * 60)

    app.run(debug=True, port=8080)
