"""
ade_tls.py — CA bundle that tolerates the ADE server's incomplete TLS chain.

Since its certificate renewal on 2026-10-12, planning.polytech.universite-paris-saclay.fr
serves ONLY its leaf certificate (issued by HARICA "GEANT TLS ECC 1") and omits the
intermediate. Browsers silently repair this (AIA fetching / cached intermediates),
so the /direct link still opens fine in Chrome — but Python's `requests` does not,
and every call failed with "certificate verify failed: unable to get local issuer
certificate" (the feed answered 503 "could not reach ADE").

Fix: verify against certifi's roots PLUS the missing intermediate, shipped in
certs/. The root (HARICA TLS ECC Root CA 2021) stays the trust anchor, so this
does not weaken verification; if the school fixes its chain, nothing changes.
If the school changes CA again, grab the new intermediate from the leaf's
"CA Issuers" AIA URL and drop it into certs/ as a PEM-encoded *.crt
(NOT *.pem: .gcloudignore excludes *.pem, so it would never be deployed).
"""

from __future__ import annotations

import functools
import glob
import logging
import os
import tempfile

logger = logging.getLogger("ade_tls")

_CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")


@functools.lru_cache(maxsize=1)
def ca_bundle() -> str | bool:
    """Path to certifi's bundle + every PEM-encoded *.crt in certs/ (built once per process).

    Falls back to True (requests' default verification) if anything goes wrong.
    """
    try:
        import certifi

        extras = sorted(glob.glob(os.path.join(_CERT_DIR, "*.crt")))
        if not extras:
            logger.warning("no certs/*.crt deployed; ADE's incomplete TLS chain will fail")
            return True
        with open(certifi.where(), encoding="utf-8") as fh:
            parts = [fh.read()]
        for path in extras:
            with open(path, encoding="utf-8") as fh:
                parts.append(fh.read())
        fd, bundle = tempfile.mkstemp(prefix="ade2ics-ca-", suffix=".pem")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(parts))
        return bundle
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not build CA bundle (%s); using default verification", exc)
        return True
