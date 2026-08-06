"""Detecting and working around TLS interception.

Consumer antivirus "web shields" and corporate proxies both terminate outbound HTTPS
and re-sign it with their own root. That root is installed in the OS trust store, so
browsers are happy -- but Python verifies against `certifi`, which contains only
public CAs, so every provider call dies with CERTIFICATE_VERIFY_FAILED. The failure
looks exactly like a bad API key, which is what makes it worth detecting explicitly.

The fix is a bundle that merges both stores. It is deliberately *not* `verify=False`:
turning verification off would hide a real man-in-the-middle just as effectively as it
hides this benign one.

Worth being clear-eyed about what this does and doesn't buy: where interception is
active, the intercepting software already sees every request in cleartext, API keys
included. Merging the bundle does not weaken that -- it stops pretending verification
is happening and lets the traffic through.
"""

from __future__ import annotations

import base64
import ssl
from dataclasses import dataclass
from pathlib import Path

import certifi

from .config import CA_BUNDLE

# Probing a host we never authenticate to keeps this free of side effects.
PROBE_HOSTS = ("generativelanguage.googleapis.com", "api.openai.com")

_SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"


@dataclass(frozen=True)
class Interception:
    host: str
    intercepted: bool
    issuer: str
    certifi_ok: bool
    os_store_ok: bool


def _issuer_of(host: str, context: ssl.SSLContext, timeout: float = 8.0) -> str | None:
    import socket

    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                issuer = dict(x[0] for x in cert.get("issuer", ()))
                return issuer.get("organizationName") or issuer.get("commonName") or "?"
    except (OSError, ssl.SSLError):
        return None


def detect(host: str = PROBE_HOSTS[0]) -> Interception:
    """Compare what certifi accepts against what the OS store accepts."""
    strict = ssl.create_default_context(cafile=certifi.where())
    certifi_ok = _issuer_of(host, strict) is not None

    relaxed = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    relaxed.load_default_certs(ssl.Purpose.SERVER_AUTH)
    issuer = _issuer_of(host, relaxed)

    return Interception(
        host=host,
        # certifi rejects it but the OS store accepts it: a locally-trusted root is
        # signing for a public host, which is interception by definition.
        intercepted=(not certifi_ok and issuer is not None),
        issuer=issuer or "(unreachable)",
        certifi_ok=certifi_ok,
        os_store_ok=issuer is not None,
    )


def _pem(der: bytes) -> str:
    body = base64.b64encode(der).decode("ascii")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"


def build_bundle(dest: Path = CA_BUNDLE) -> tuple[int, int]:
    """Write certifi's roots plus the OS store's. Returns (certifi_count, added)."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    base = Path(certifi.where()).read_text(encoding="ascii")
    seen: set[bytes] = set()
    for chunk in base.split("-----BEGIN CERTIFICATE-----")[1:]:
        body = chunk.split("-----END CERTIFICATE-----")[0]
        seen.add(base64.b64decode("".join(body.split())))
    certifi_count = len(seen)

    extra: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            entries = ssl.enum_certificates(store)
        except (AttributeError, OSError):
            continue  # Not Windows, or the store is unreadable.
        for der, encoding, trust in entries:
            if encoding != "x509_asn" or der in seen:
                continue
            # `trust` is True for all purposes, or a set of EKU OIDs. Anything that
            # cannot vouch for a server has no business in a server-auth bundle.
            if trust is not True and _SERVER_AUTH_OID not in (trust or ()):
                continue
            seen.add(der)
            extra.append(_pem(der))

    out = base if base.endswith("\n") else base + "\n"
    out += "\n# --- appended from the OS certificate store by ruti ---\n" + "".join(extra)

    tmp = dest.with_suffix(".pem.tmp")
    tmp.write_text(out, encoding="ascii")
    tmp.replace(dest)
    return certifi_count, len(extra)


def apply_to_environment(bundle: Path = CA_BUNDLE) -> bool:
    """Point this process's TLS stack at the merged bundle, if one has been built.

    start-litellm.ps1 exports SSL_CERT_FILE for the *proxy*, but ruti's own commands
    talk to provider APIs directly -- testing a key, listing models -- and would
    otherwise verify against certifi and fail. Without this, `ruti provider add`
    reports TLS interception for every key on an intercepted machine, which is true
    but not useful when the fix is already sitting on disk.
    """
    import os

    if not bundle.is_file():
        return False
    for variable in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        os.environ.setdefault(variable, str(bundle))
    return True


def bundle_works(bundle: Path = CA_BUNDLE, host: str = PROBE_HOSTS[0]) -> bool:
    if not bundle.exists():
        return False
    try:
        context = ssl.create_default_context(cafile=str(bundle))
    except (ssl.SSLError, OSError):
        return False
    return _issuer_of(host, context) is not None
