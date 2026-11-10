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


# The web certificate: an optional certificate of the person's own, from their
# CA or a public one, for the ports people browse to (443, and 8742 for
# netaudio). The identity certificate above stays on the group port and on
# 8740, where members, invites and virgilctl pin it, so importing one never
# changes who a Virgil is to its group.

WEB_DIR = "web"
EXPIRY_WARNING_DAYS = 30


def web_identity(directory: Path = TLS_DIR) -> Identity | None:
    folder = directory / WEB_DIR
    identity = Identity(folder / "cert.pem", folder / "key.pem")
    return identity if identity.cert_path.is_file() and identity.key_path.is_file() else None


def check_pair(cert_path: Path, key_path: Path) -> None:
    """That the key belongs to the certificate, and both can be loaded."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert_path, key_path, password=lambda: b"")
    except ssl.SSLError as error:
        reason = str(error)
        if "KEY_VALUES_MISMATCH" in reason or "key values mismatch" in reason.lower():
            raise TlsError("the key doesn't belong to that certificate") from error
        if "bad decrypt" in reason.lower() or "BAD_DECRYPT" in reason:
            raise TlsError("the key is protected by a passphrase; give Virgil an unencrypted key") from error
        raise TlsError(f"couldn't load the certificate and key: {reason}") from error
    except OSError as error:
        raise TlsError(f"couldn't read the certificate or key: {error}") from error


def _name(fields) -> str | None:
    """A certificate subject or issuer's most telling name: its common name, else its organisation."""
    values = {key: value for rdn in fields or () for key, value in rdn}
    return values.get("commonName") or values.get("organizationName")


def inspect(cert_path: Path) -> dict:
    """Who a certificate is for, who issued it, when it's valid, and which names it covers."""
    try:
        decoded = ssl._ssl._test_decode_cert(str(cert_path))  # the first certificate in the file
    except (ssl.SSLError, OSError) as error:
        raise TlsError(f"couldn't read the certificate: {error}") from error
    names = [f"{kind}:{value}" for kind, value in decoded.get("subjectAltName", ())]
    subject, issuer = _name(decoded.get("subject")), _name(decoded.get("issuer"))
    return {
        "subject": subject,
        "issuer": issuer,
        "self_signed": decoded.get("subject") == decoded.get("issuer"),
        "not_before": ssl.cert_time_to_seconds(decoded["notBefore"]),
        "not_after": ssl.cert_time_to_seconds(decoded["notAfter"]),
        "names": names,
    }


def covers(info: dict, name: str) -> bool:
    """Whether a certificate is valid for a host name or address, as a browser judges it."""
    for entry in info["names"]:
        kind, _, value = entry.partition(":")
        if kind == "IP Address" and value.rstrip() == name:
            return True
        if kind == "DNS":
            value = value.lower()
            if value == name.lower():
                return True
            if value.startswith("*.") and "." in name and name.lower().split(".", 1)[1] == value[2:]:
                return True
    return False


def install_web(cert_path: Path, key_path: Path, directory: Path = TLS_DIR, owner: str | None = None) -> Identity:
    """Copy a web certificate and key into place, the key readable only by virgild."""
    import os
    import pwd
    import shutil

    folder = directory / WEB_DIR
    folder.mkdir(mode=0o755, exist_ok=True)
    identity = Identity(folder / "cert.pem", folder / "key.pem")
    for source, target, mode in ((cert_path, identity.cert_path, 0o644), (key_path, identity.key_path, 0o600)):
        temporary = target.with_suffix(".new")
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        if owner is not None and target is identity.key_path:
            user = pwd.getpwnam(owner)
            os.chown(temporary, user.pw_uid, user.pw_gid)
        os.replace(temporary, target)
    return identity


def remove_web(directory: Path = TLS_DIR) -> bool:
    folder = directory / WEB_DIR
    removed = False
    for name in ("cert.pem", "key.pem"):
        path = folder / name
        if path.exists():
            path.unlink()
            removed = True
    return removed


def web_server_context(identity: Identity, directory: Path = TLS_DIR) -> ssl.SSLContext:
    """For the ports people browse to: the web certificate if there is one, else the identity certificate."""
    return public_server_context(web_identity(directory) or identity)
