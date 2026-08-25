"""
ade_gwt.py — fetch official ADE event *types* (CM / TD / C-TD / Contrôle / ...)
from the ADE 6.17 "Web Direct Planning" GWT-RPC backend.

Why this exists
---------------
The new ADE server (planning.polytech.universite-paris-saclay.fr) exposes its
anonymous iCal export, but NOT the activity Type field: `jsp/rss` is empty and
`eventInfo.jsp` redirects to login for anonymous users. The only anonymous
surface that still carries the Type is the /direct GWT planning app, which the
intranet links to with an encrypted `?data=` token (read-only user `visuweb`).

This module replays that app's GWT-RPC calls (whole year in 5 requests, <1 s):

    GET  /direct/?data=<token>,1                sets JSESSIONID
    POST method1login   (DirectPlanningServiceProxy)     logs in via the token
    POST method13loadProject                             opens projectId
    POST method6getLegends  (…PlanningServiceProxy)      SETS THE SERVER-SIDE
                                                         SELECTION: getTimetable
                                                         serves the resources +
                                                         weeks of the *last*
                                                         getLegends call (and
                                                         NPEs if none was made).
                                                         We select every piano
                                                         week (0..52) at once.
    POST method10getTimetable                            ONE call returns every
                                                         "SquareEvent" in the
                                                         selection; its own week
                                                         list only picks the
                                                         display grid, so we pass
                                                         [0].

Each SquareEvent embeds the ADE *eventId* (the same id found in the iCal UID,
`ADE` + hex("`<project>-<eventId>-<session>-<rep>")) and its display lines:

    [name, type, teacher..., room..., group...]

so we map eventId -> type with zero geometry involved.

Known limits (checked 2026-08-25):
  * keep the timetable request's own week list to a SINGLE week — multi-week
    requests switch the response to an aggregate "Cumul" encoding this parser
    does not read;
  * the GWT permutation / serialization-policy hashes below are baked into the
    deployed ADE build. An ADE upgrade will change them and every call will
    fail cleanly — the caller must treat an empty result as "no types today"
    (the calendar still works, just without official types). To re-capture:
    open the /direct APP5 link with browser devtools and copy the values from
    any *ServiceProxy request (X-GWT-Permutation header + 2nd field of the body).
"""

from __future__ import annotations

import logging
import os
import re
import time

import requests

logger = logging.getLogger("ade_gwt")

# --- Constants captured from the deployed ADE 6.17 build (2026-08-25) --------
PERMUTATION = os.environ.get("ADE_GWT_PERMUTATION", "F0B064A5524657E44B6F389432E4A88F")
POLICY_DIRECT = os.environ.get("ADE_GWT_POLICY_DIRECT", "E07660201B4B36F3E98F8913F3D3DB01")
POLICY_PLANNING = os.environ.get("ADE_GWT_POLICY_PLANNING", "5B85F4B5FF901AAC381326C3E06AC751")
DISPLAY_CONFIG = 40  # "Affichage_semaine": cells render NAME + TYPE_ACTIVITY + resources

# Opaque client id (a GWT-encoded long the JS client generates once per page
# load; the server treats it as a client correlation id — any value works).
CLIENT_ID = "aA5kE6x"

# The intranet "APP5" direct-access token (encrypted login for read-only user
# `visuweb`; from https://intranet.polytech.universite-paris-saclay.fr/planning.html).
# The trailing ",1" is part of the URL parameter and of the login payload.
DEFAULT_DATA_TOKEN = os.environ.get(
    "ADE_DIRECT_DATA",
    "95e000515dff41bf090dab728096f2e56a0b179abf87e00afbd4d1456078eea5ee89f496aa25ad65"
    "e554f8ae63b9ffad4bed70619bd37c737b049faa2be2e8d1c579ed9a4cdb41f702f705740da3d2e2"
    "87643d51fd9e6d518032f83f5d0ba15559e7b879f62e7da35f60d0ef5412004e7ec16061e9f4bd51"
    "04a5dcb1e2021b7618fa684c37b1bee2c0716937c1e8a308,1",
)

_SVC_DIRECT = "com.adesoft.gwt.directplan.client.rpc.DirectPlanningServiceProxy"
_SVC_PLANNING = "com.adesoft.gwt.directplan.client.rpc.DirectPlanningPlanningServiceProxy"

_IDENTIFIER_RE = re.compile(r"[0-9a-f]{32}w\d+")
_SQUARE_DELIM = re.compile(r"4,0,1,(\d+),3,1,1,2,")
_TEXT_REF = re.compile(r"(?:^|,)0,(\d+),6(?=,|$)")
_ROOMISH = re.compile(r"^\d{3}\s*-\s")

RPC_TIMEOUT = 15   # seconds per HTTP call
N_DAYS = 7         # Mon..Sun — Saturday events exist (rarely)
ALL_WEEKS = list(range(0, 53))  # out-of-range weeks are harmless in getLegends


class AdeGwtError(RuntimeError):
    """Raised when the GWT replay cannot proceed (login refused, bad payload...)."""


# ---------------------------------------------------------------------------
# GWT-RPC response helpers
# ---------------------------------------------------------------------------
def _parse_string_table(payload: str) -> tuple[list[str], str]:
    """Split a //OK[...] payload into (string table, leading int stream)."""
    i = payload.rindex("],")
    j = payload.rindex("[", 0, i)
    raw = payload[j + 1 : i]
    out: list[str] = []
    buf: list[str] = []
    k, in_str = 0, False
    while k < len(raw):
        c = raw[k]
        if not in_str:
            if c == '"':
                in_str = True
                buf = []
            k += 1
            continue
        if c == "\\":
            n = raw[k + 1]
            if n == "x":
                buf.append(chr(int(raw[k + 2 : k + 4], 16)))
                k += 4
            elif n == "u":
                buf.append(chr(int(raw[k + 2 : k + 6], 16)))
                k += 6
            elif n == "n":
                buf.append("\n")
                k += 2
            elif n == "t":
                buf.append("\t")
                k += 2
            else:
                buf.append(n)
                k += 2
            continue
        if c == '"':
            in_str = False
            out.append("".join(buf))
            k += 1
            continue
        buf.append(c)
        k += 1
    return out, payload[:j]


def _parse_squares(payload: str) -> list[tuple[int, list[str]]]:
    """Extract (eventId, display lines) pairs from a getTimetable //OK payload.

    The int stream is read backwards by the GWT client; in raw array order each
    square *ends* with `4,0,1,<eventId>,3,1,1,2` and its display lines appear as
    `0,<stringRef>,6` groups in reverse display order.
    """
    strings, ints = _parse_string_table(payload)
    body = ints[len("//OK[") :]
    parts = _SQUARE_DELIM.split(body)
    squares: list[tuple[int, list[str]]] = []
    for i in range(1, len(parts), 2):
        event_id = int(parts[i])
        chunk = parts[i - 1]
        refs = [int(m) for m in _TEXT_REF.findall(chunk)]
        lines = []
        for r in reversed(refs):  # reverse -> display order
            if 0 < r <= len(strings):
                s = strings[r - 1]
                if s.startswith(("java.", "com.", "[")):  # defensive: never a display line
                    continue
                lines.append(s)
        squares.append((event_id, lines))
    return squares


def _type_of(lines: list[str]) -> str:
    """Display order is [name, type, teacher..., room..., group...]; the type
    slot is always emitted (empty string when the activity has no type)."""
    if len(lines) < 2:
        return ""
    t = lines[1].strip()
    if not t or len(t) > 40 or _ROOMISH.match(t):
        return ""
    return t


# ---------------------------------------------------------------------------
# GWT-RPC client
# ---------------------------------------------------------------------------
class DirectPlanningClient:
    def __init__(self, host: str, data_token: str = DEFAULT_DATA_TOKEN):
        self.host = host.rstrip("/")
        self.base = f"https://{self.host}/direct/gwtdirectplanning/"
        self.data_token = data_token
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 ade2ics"})
        self._rpc_headers = {
            "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
            "X-GWT-Permutation": PERMUTATION,
            "X-GWT-Module-Base": self.base,
        }

    def _rpc(self, service: str, body: str) -> str:
        resp = self.session.post(
            self.base + service.rsplit(".", 1)[-1],
            data=body.encode("utf-8"),
            headers=self._rpc_headers,
            timeout=RPC_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text

    def login(self) -> None:
        self.session.get(
            f"https://{self.host}/direct/?data={self.data_token}", timeout=RPC_TIMEOUT
        )
        body = (
            f"7|0|12|{self.base}|{POLICY_DIRECT}|{_SVC_DIRECT}|method1login|J|"
            "com.adesoft.gwt.core.client.rpc.data.LoginRequest/3705388826|"
            "java.lang.String/2004016611|Z|"
            "com.adesoft.gwt.directplan.client.rpc.data.DirectLoginRequest/635437471|"
            f"{self.data_token}||fr|"
            f"1|2|3|4|4|5|6|7|8|{CLIENT_ID}|9|0|10|0|0|0|11|11|-1|0|0|12|0|"
        )
        res = self._rpc(_SVC_DIRECT, body)
        if not res.startswith("//OK") or not _IDENTIFIER_RE.search(res):
            raise AdeGwtError(f"GWT login refused: {res[:200]}")

    def load_project(self, project_id: int) -> None:
        body = (
            f"7|0|7|{self.base}|{POLICY_DIRECT}|{_SVC_DIRECT}|method13loadProject|J|I|Z|"
            f"1|2|3|4|3|5|6|7|{CLIENT_ID}|{project_id}|1|"
        )
        res = self._rpc(_SVC_DIRECT, body)
        if not res.startswith("//OK"):
            raise AdeGwtError(f"loadProject({project_id}) failed: {res[:200]}")

    @staticmethod
    def _selection(int_ref: int, resources: list[int], weeks: list[int], tail: str) -> str:
        days = "|".join(f"{int_ref}|{d}" for d in range(N_DAYS))
        res = "".join(f"{int_ref}|{r}|" for r in resources)
        wks = "".join(f"{int_ref}|{w}|" for w in weeks)
        return (
            f"{days}|{DISPLAY_CONFIG}|{tail}|0|0|"
            f"{int_ref - 1}|{len(resources)}|{res}"
            f"{int_ref - 1}|{len(weeks)}|{wks}"
        )

    def get_legends(self, resources: list[int], weeks: list[int]) -> str:
        """Also SETS the session's selection — what getTimetable will serve."""
        # Integer type ref is 10 in this call's string table (ArrayList is 9).
        sel = self._selection(10, resources, weeks, "11|12")
        body = (
            f"7|0|14|{self.base}|{POLICY_PLANNING}|{_SVC_PLANNING}|method6getLegends|J|"
            "com.adesoft.gwt.core.client.rpc.data.planning.PlanningSelection/927838067|"
            "com.extjs.gxt.ui.client.data.SortInfo/1143517771|java.util.List|"
            "java.util.ArrayList/4159755760|java.lang.Integer/3438268394|Cumul|"
            "Déplacement|com.extjs.gxt.ui.client.Style$SortDir/3873584144|NAME|"
            f"1|2|3|4|4|5|6|7|8|{CLIENT_ID}|6|9|{N_DAYS}|{sel}7|13|1|14|9|0|"
        )
        res = self._rpc(_SVC_PLANNING, body)
        if not res.startswith("//OK"):
            raise AdeGwtError(f"getLegends failed: {res[:200]}")
        return res

    def get_timetable(self, resources: list[int], week: int = 0) -> str:
        """Serves the current getLegends selection; `week` only sets the grid."""
        # Integer type ref is 11 in this call's string table (ArrayList is 10).
        sel = self._selection(11, resources, [week], "12|13")
        body = (
            f"7|0|13|{self.base}|{POLICY_PLANNING}|{_SVC_PLANNING}|method10getTimetable|J|"
            "com.adesoft.gwt.core.client.rpc.data.planning.PlanningSelection/927838067|I|Z|"
            "java.util.List|java.util.ArrayList/4159755760|java.lang.Integer/3438268394|"
            "Cumul|Déplacement|"
            f"1|2|3|4|7|5|6|7|7|8|9|9|{CLIENT_ID}|6|10|{N_DAYS}|{sel}1536|829|1|10|0|10|0|"
        )
        return self._rpc(_SVC_PLANNING, body)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_types(
    host: str,
    project_id: int,
    resources: list[int],
    data_token: str = DEFAULT_DATA_TOKEN,
) -> dict[int, str]:
    """Return {ade_event_id: official_type} for the given resources.

    Whole year in 5 HTTP round-trips (<1 s): login, loadProject, getLegends
    selecting every piano week, one getTimetable. Raises AdeGwtError when the
    replay cannot proceed (caller should degrade gracefully).
    """
    started = time.monotonic()
    client = DirectPlanningClient(host, data_token)
    client.login()
    client.load_project(project_id)
    client.get_legends(resources, ALL_WEEKS)  # selects resources + all weeks
    payload = client.get_timetable(resources)
    if not payload.startswith("//OK"):
        raise AdeGwtError(f"getTimetable failed: {payload[:200]}")

    types: dict[int, str] = {}
    for event_id, lines in _parse_squares(payload):
        t = _type_of(lines)
        if t and not types.get(event_id):
            types[event_id] = t
        types.setdefault(event_id, t)
    types = {k: v for k, v in types.items() if v}
    logger.info(
        "fetched %d typed activities in %.2fs", len(types), time.monotonic() - started
    )
    return types
