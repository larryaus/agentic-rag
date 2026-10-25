import asyncio
import os
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from kb_assistant.adapters.local.fs_blobs import BlobTokenError, FsBlobs, load_secret
from kb_assistant.app import create_app
from kb_assistant.settings import Settings

KEY = "uploads/owner/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/doc.md"


@pytest.fixture
def local(tmp_path):
    app = create_app(
        Settings(deploy_target="local", blob_root=str(tmp_path), max_upload_bytes=1024)
    )
    blobs = app.state.container.blobs
    uploads = []
    blobs.on_upload = uploads.append
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
    return client, blobs, uploads, tmp_path


def path_of(url):
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


async def test_signed_upload_and_download_round_trip(local):
    client, blobs, uploads, root = local
    url = await blobs.presign_put(KEY, "text/markdown")
    assert url.startswith("http://localhost:8000/v1/blobs/upload?token=")
    response = await client.put(path_of(url), content=b"# Doc", headers={"content-type": "text/markdown"})
    assert response.status_code == 200 and uploads == [KEY]
    assert (root / KEY).read_bytes() == b"# Doc" and await blobs.head(KEY) == 5
    assert not list(root.rglob("*.part-*"))
    download = await client.get(path_of(await blobs.presign_get(KEY)))
    assert download.status_code == 200 and download.content == b"# Doc"
    assert download.headers["content-disposition"].startswith("attachment")


async def test_overwrite_refused_and_concurrent_puts_publish_once(local):
    client, blobs, uploads, root = local
    url = path_of(await blobs.presign_put(KEY, "text/markdown"))
    responses = await asyncio.gather(
        *(
            client.put(url, content=f"v{i}".encode(), headers={"content-type": "text/markdown"})
            for i in range(5)
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409, 409]
    assert [r.json() for r in responses if r.status_code == 409][0] == {
        "message": "Object already exists"
    }
    assert len(uploads) == 1 and not list(root.rglob("*.part-*"))


async def test_oversize_upload_rejected_and_cleaned(local):
    client, blobs, uploads, root = local
    url = path_of(await blobs.presign_put(KEY, "text/markdown"))
    response = await client.put(url, content=b"x" * 2048, headers={"content-type": "text/markdown"})
    assert response.status_code == 413 and uploads == []
    assert not (root / KEY).exists() and not list(root.rglob("*.part-*"))


async def test_token_checks(local):
    client, blobs, _, _ = local
    put = path_of(await blobs.presign_put(KEY, "text/markdown"))
    get = path_of(await blobs.presign_get(KEY))
    headers = {"content-type": "text/markdown"}
    assert (await client.put(put, content=b"x", headers={"content-type": "text/html"})).status_code == 403
    assert (await client.put(get.replace("download", "upload"), content=b"x", headers=headers)).status_code == 403
    token = parse_qs(urlsplit(put).query)["token"][0]
    body, signature = token.split(".")
    tampered = f"/v1/blobs/upload?token={body}.{'0' * len(signature)}"
    assert (await client.put(tampered, content=b"x", headers=headers)).status_code == 403
    assert (await client.put("/v1/blobs/upload", content=b"x", headers=headers)).status_code == 403
    expired = blobs.sign(dict(op="upload", key=KEY, ct="text/markdown", exp=int(time.time()) - 1))
    assert (await client.put(f"/v1/blobs/upload?token={expired}", content=b"x", headers=headers)).status_code == 403
    assert (await client.get("/v1/documents")).status_code == 401


@pytest.mark.parametrize(
    "key", ["../escape", "/abs/path", "uploads//x", "uploads/./x", "a\\b", "uploads/x/../../y", ""]
)
def test_keys_cannot_escape_the_root(local, key):
    _, blobs, _, _ = local
    with pytest.raises(BlobTokenError):
        blobs.path(key)
    token = blobs.sign(dict(op="download", key=key, exp=int(time.time()) + 60))
    with pytest.raises(BlobTokenError):
        blobs.verify(token, "download")


def test_symlinks_under_the_root_are_refused(local, tmp_path_factory):
    _, blobs, _, root = local
    outside = tmp_path_factory.mktemp("outside")
    (root / "uploads").mkdir()
    os.symlink(outside, root / "uploads" / "link")
    with pytest.raises(BlobTokenError):
        blobs.path("uploads/link/file")


def test_signing_secret_created_once_and_reused(tmp_path):
    first = load_secret(tmp_path, "")
    assert (tmp_path / ".signing-secret").stat().st_mode & 0o777 == 0o600
    assert load_secret(tmp_path, "") == first and len(first) == 64
    assert load_secret(tmp_path, "c" * 40) == b"c" * 40
    other = FsBlobs(tmp_path, "http://x", load_secret(tmp_path, ""))
    assert other.verify(other.sign(dict(op="download", key=KEY, exp=int(time.time()) + 9)), "download")


async def test_sidecar_head_delete_and_part_sweep(local):
    _, blobs, _, root = local
    await blobs.put_sidecar(KEY, "{}")
    assert (root / f"{KEY}.metadata.json").read_text() == "{}"
    assert await blobs.head("uploads/missing/file") is None
    await blobs.delete(f"{KEY}.metadata.json")
    await blobs.delete(f"{KEY}.metadata.json")
    stale = root / "uploads" / "x.part-1"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    fresh = root / "uploads" / "y.part-2"
    fresh.write_bytes(b"y")
    assert blobs.sweep_parts() == 1 and fresh.exists() and not stale.exists()


def test_short_or_empty_secret_file_is_replaced(tmp_path):
    path = tmp_path / ".signing-secret"
    path.write_bytes(b"")
    replaced = load_secret(tmp_path, "")
    assert len(replaced) == 64 and path.read_bytes() == replaced
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_secret(tmp_path, "") == replaced


def test_configured_secret_must_be_long_enough(tmp_path):
    with pytest.raises(ValueError, match="at least 32 bytes"):
        load_secret(tmp_path, "short")
    assert load_secret(tmp_path, "s" * 32) == b"s" * 32
