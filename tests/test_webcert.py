"""Importing a certificate of the person's own for the ports people browse to."""

import asyncio
import shutil
import ssl
import subprocess
import time
from pathlib import Path

import pytest

from virgil import cli, tls

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def _run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def ca(tmp_path):
    """A CA, and a certificate from it for virgil-a.lan, *.show.lan, 127.0.0.1 and 10.10.100.253."""
    folder = tmp_path / "ca"
    folder.mkdir()
    ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes"]
    _run("openssl", "req", "-x509", *ec, "-keyout", "ca.key", "-out", "ca.pem", "-days", "30",
         "-subj", "/CN=Highpass Test CA", cwd=folder)
    _run("openssl", "req", *ec, "-keyout", "leaf.key", "-out", "leaf.csr", "-subj", "/CN=virgil-a.lan", cwd=folder)
    (folder / "ext").write_text("subjectAltName=DNS:virgil-a.lan,DNS:*.show.lan,IP:127.0.0.1,IP:10.10.100.253\n")
    _run("openssl", "x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
         "-out", "leaf.pem", "-days", "20", "-extfile", "ext", cwd=folder)
    (folder / "fullchain.pem").write_text((folder / "leaf.pem").read_text() + (folder / "ca.pem").read_text())
    return folder


@pytest.fixture
def tls_dir(tmp_path):
    folder = tmp_path / "tls"
    tls.generate(tls.default_identity(folder), hostname="a")
    return folder


def test_inspect_and_cover(ca):
    info = tls.inspect(ca / "fullchain.pem")
    assert info["subject"] == "virgil-a.lan" and info["issuer"] == "Highpass Test CA" and not info["self_signed"]
    assert 19 * 86400 < info["not_after"] - time.time() < 21 * 86400
    assert tls.covers(info, "10.10.100.253") and tls.covers(info, "VIRGIL-A.lan") and tls.covers(info, "foh.show.lan")
    assert not tls.covers(info, "10.0.0.9") and not tls.covers(info, "a.b.show.lan")  # a wildcard covers one label


def test_a_key_that_doesnt_belong(ca):
    with pytest.raises(tls.TlsError, match="doesn't belong"):
        tls.check_pair(ca / "fullchain.pem", ca / "ca.key")


def test_import_show_and_remove(ca, tls_dir, monkeypatch, capsys):
    restarts = []
    monkeypatch.setattr(cli, "_restart_virgild", lambda: restarts.append(True))
    monkeypatch.setattr(cli, "_own_names", lambda: ["debian-server-1", "10.10.100.253"])
    args = ["--tls-dir", str(tls_dir), "tls", "import-web", "--cert", str(ca / "fullchain.pem"),
            "--key", str(ca / "leaf.key"), "--yes", "--owner", ""]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "It covers this Virgil as: 10.10.100.253" in out and "expires in under 30 days" in out
    web = tls.web_identity(tls_dir)
    assert web is not None and (web.key_path.stat().st_mode & 0o777) == 0o600 and restarts == [True]
    # The identity certificate is untouched: the group still knows this Virgil.
    assert tls.default_identity(tls_dir).cert_path.read_text() != web.cert_path.read_text()
    assert cli.main(["--tls-dir", str(tls_dir), "tls", "show"]) == 0
    assert "Issued by: Highpass Test CA" in capsys.readouterr().out
    assert cli.main(["--tls-dir", str(tls_dir), "tls", "remove-web", "--yes"]) == 0
    assert tls.web_identity(tls_dir) is None


def test_import_refuses_a_mismatched_key(ca, tls_dir, capsys):
    args = ["--tls-dir", str(tls_dir), "tls", "import-web", "--cert", str(ca / "fullchain.pem"),
            "--key", str(ca / "ca.key"), "--yes", "--owner", ""]
    assert cli.main(args) == 1
    assert "doesn't belong" in capsys.readouterr().err and tls.web_identity(tls_dir) is None


def test_browsers_get_the_web_certificate_and_trust_it(ca, tls_dir):
    """A real TLS connection to the browser ports, checked against the CA, as a browser would."""
    tls.install_web(ca / "fullchain.pem", ca / "leaf.key", tls_dir)
    server = tls.web_server_context(tls.default_identity(tls_dir), tls_dir)

    async def scenario():
        async def handle(reader, writer):
            writer.close()

        listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server)
        port = listener.sockets[0].getsockname()[1]
        client = ssl.create_default_context(cafile=str(ca / "ca.pem"))  # trusts only our CA, and checks the name
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client, server_hostname="127.0.0.1")
            subject = dict(item for rdn in writer.get_extra_info("peercert")["subject"] for item in rdn)
            writer.close()
            return subject["commonName"]
        finally:
            listener.close()

    assert asyncio.run(scenario()) == "virgil-a.lan"
    # Without one, the identity certificate is still served.
    tls.remove_web(tls_dir)
    plain = tls.web_server_context(tls.default_identity(tls_dir), tls_dir)
    assert isinstance(plain, ssl.SSLContext)
