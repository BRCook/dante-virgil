"""Each Virgil's TLS certificate, and the TLS contexts built from it.

Every Virgil has its own self-signed certificate, made when it's installed.
Members of a group pin each other's certificates: the group port only accepts
connections presenting a member's certificate, and only connects to one.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import socket
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path

TLS_DIR = Path("/etc/dante-virgil/tls")
VALID_DAYS = 3650


class TlsError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    cert_path: Path
    key_path: Path

    @property
    def cert_pem(self) -> str:
        return self.cert_path.read_text()


def default_identity(directory: Path = TLS_DIR) -> Identity:
    return Identity(directory / "cert.pem", directory / "key.pem")


def generate(identity: Identity, hostname: str | None = None) -> None:
    """Make a new self-signed certificate and key. Refuses to replace an existing one."""
    if identity.cert_path.exists() or identity.key_path.exists():
        raise TlsError(f"{identity.cert_path.parent} already has a certificate")
    openssl = shutil.which("openssl")
    if openssl is None:
        raise TlsError("openssl isn't installed")
    hostname = hostname or socket.gethostname()
    identity.cert_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            openssl, "req", "-x509",
            "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", str(VALID_DAYS),
            "-subj", f"/CN=Virgil on {hostname}",
            "-addext", f"subjectAltName=DNS:{hostname},DNS:{hostname}.local",
            # Not a CA, so a member's certificate can't be used to sign others.
            "-addext", "basicConstraints=critical,CA:FALSE",
            "-addext", "keyUsage=critical,digitalSignature",
            "-addext", "extendedKeyUsage=serverAuth,clientAuth",
            "-keyout", str(identity.key_path),
            "-out", str(identity.cert_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TlsError(f"openssl failed: {result.stderr.strip()}")
    identity.key_path.chmod(0o600)
    identity.cert_path.chmod(0o644)


def fingerprint(cert_pem: str) -> str:
    """The SHA-256 fingerprint of a certificate, as lowercase hex."""
    try:
        der = ssl.PEM_cert_to_DER_cert(cert_pem)
    except ValueError as error:
        raise TlsError("not a PEM certificate") from error
    return hashlib.sha256(der).hexdigest()


def fingerprint_of_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def short_code(fingerprint_hex: str) -> str:
    """A short, readable form of a fingerprint, for showing people (e.g. 'K7QF-2MZA-9XWD')."""
    raw = bytes.fromhex(fingerprint_hex)[:8]
    text = base64.b32encode(raw).decode().rstrip("=")[:12]
    return "-".join(text[i : i + 4] for i in range(0, 12, 4))


def _base_context(protocol: int) -> ssl.SSLContext:
    context = ssl.SSLContext(protocol)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    return context


def public_server_context(identity: Identity) -> ssl.SSLContext:
    """The public port: an ordinary server certificate, and no client certificate."""
    context = _base_context(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(identity.cert_path, identity.key_path)
    return context


def group_server_context(identity: Identity, member_certs: list[str]) -> ssl.SSLContext:
    """The group port: only clients presenting a member's certificate get in."""
    context = public_server_context(identity)
    context.verify_mode = ssl.CERT_REQUIRED
    for cert in member_certs:
        context.load_verify_locations(cadata=cert)
    return context


def group_client_context(identity: Identity, member_certs: list[str]) -> ssl.SSLContext:
    """Connecting to a member: present this Virgil's certificate, and only accept a member's.

    Hostnames aren't checked, since Virgils are found by address and addresses
    change. The pinned certificate is what identifies the member.
    """
    context = _base_context(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(identity.cert_path, identity.key_path)
    for cert in member_certs:
        context.load_verify_locations(cadata=cert)
    return context
