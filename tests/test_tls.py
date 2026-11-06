import asyncio
import shutil
import ssl

import pytest

from virgil.tls import (
    Identity,
    TlsError,
    fingerprint,
    generate,
    group_client_context,
    group_server_context,
    public_server_context,
    short_code,
)

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


@pytest.fixture(scope="module")
def virgils(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls")
    identities = {}
    for name in ("a", "b", "c"):
        identity = Identity(directory / name / "cert.pem", directory / name / "key.pem")
        generate(identity, hostname=f"virgil-{name}")
        identities[name] = identity
    return identities


def test_generate(virgils):
    a = virgils["a"]
    assert a.key_path.stat().st_mode & 0o777 == 0o600
    assert "BEGIN CERTIFICATE" in a.cert_pem
    with pytest.raises(TlsError):
        generate(a)


def test_fingerprints(virgils):
    a, b = fingerprint(virgils["a"].cert_pem), fingerprint(virgils["b"].cert_pem)
    assert len(a) == 64 and a != b
    code = short_code(a)
    assert len(code) == 14 and code.count("-") == 2
    with pytest.raises(TlsError):
        fingerprint("not a certificate")


async def _connect(server_ctx, client_ctx):
    async def handle(reader, writer):
        writer.write(b"ok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx)
        data = await reader.read(2)
        writer.close()
        return data
    finally:
        server.close()
        await server.wait_closed()


def _group(virgils, *names):
    return [virgils[n].cert_pem for n in names]


def test_members_can_talk(virgils):
    server = group_server_context(virgils["a"], _group(virgils, "a", "b"))
    client = group_client_context(virgils["b"], _group(virgils, "a", "b"))
    assert asyncio.run(_connect(server, client)) == b"ok"


def test_a_stranger_is_turned_away(virgils):
    server = group_server_context(virgils["a"], _group(virgils, "a", "b"))
    client = group_client_context(virgils["c"], _group(virgils, "a", "b", "c"))
    # With TLS 1.3 the server rejects the client certificate after the
    # handshake, so the client sees the connection close, or an error.
    try:
        data = asyncio.run(_connect(server, client))
    except (ssl.SSLError, ConnectionError):
        data = b""
    assert data == b""


def test_an_impostor_server_is_refused(virgils):
    server = group_server_context(virgils["c"], _group(virgils, "b", "c"))
    client = group_client_context(virgils["b"], _group(virgils, "a", "b"))
    with pytest.raises(ssl.SSLCertVerificationError):
        asyncio.run(_connect(server, client))


def test_public_port_needs_no_client_certificate(virgils):
    server = public_server_context(virgils["a"])
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    assert asyncio.run(_connect(server, client)) == b"ok"
