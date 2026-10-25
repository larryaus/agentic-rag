import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

UPLOAD = "upload"
DOWNLOAD = "download"
PART_MAX_AGE_SECONDS = 3600


class BlobTokenError(Exception):
    pass


MIN_SECRET_BYTES = 32


def load_secret(root: Path, configured: str) -> bytes:
    """The configured secret, or one generated once and kept with the files."""
    if configured:
        if len(configured.encode()) < MIN_SECRET_BYTES:
            raise ValueError(f"BLOB_SIGNING_SECRET must be at least {MIN_SECRET_BYTES} bytes")
        return configured.encode()
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".signing-secret"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_bytes().strip()
        if len(value) >= MIN_SECRET_BYTES:
            return value
        # A crash between creating and writing the file left it short; replace it whole.
        value = secrets.token_hex(32).encode()
        temp = path.with_name(f".signing-secret.part-{secrets.token_hex(8)}")
        temp.write_bytes(value)
        temp.chmod(0o600)
        os.replace(temp, path)
        return value
    value = secrets.token_hex(32).encode()
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
    return value


class FsBlobs:
    """Files on a local volume, behind HMAC-signed URLs served by the app itself.

    The URLs play the part of S3 presigned URLs: the browser uploads and downloads with
    them directly, and they authenticate by signature alone.
    """

    def __init__(
        self,
        root: Path,
        public_base_url: str,
        secret: bytes,
        *,
        on_upload: Callable[[str], Any] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.public_base_url = public_base_url.rstrip("/")
        self.secret = secret
        self.on_upload = on_upload

    def path(self, key: str) -> Path:
        parts = key.split("/")
        if (
            not key
            or key.startswith("/")
            or "\\" in key
            or "\x00" in key
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise BlobTokenError("Invalid key")
        path = self.root.joinpath(*parts)
        # Refuse symlinks anywhere under the root, so a key can never leave it.
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise BlobTokenError("Invalid key")
        return path

    def sign(self, claims: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(
            json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
        ).decode().rstrip("=")
        signature = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{signature}"

    def verify(self, token: str, op: str) -> dict[str, Any]:
        try:
            body, signature = token.split(".")
            expected = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise BlobTokenError("Invalid signature")
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except BlobTokenError:
            raise
        except Exception as exc:
            raise BlobTokenError("Malformed token") from exc
        if not isinstance(claims, dict) or claims.get("op") != op:
            raise BlobTokenError("Wrong operation")
        if not isinstance(claims.get("exp"), int) or claims["exp"] < time.time():
            raise BlobTokenError("Expired")
        self.path(claims.get("key", ""))
        return claims

    def _url(self, op: str, claims: dict[str, Any]) -> str:
        return f"{self.public_base_url}/v1/blobs/{op}?token={quote(self.sign(claims))}"

    async def presign_put(self, key: str, content_type: str, expires_in: int = 300) -> str:
        exp = int(time.time()) + expires_in
        return self._url(UPLOAD, dict(op=UPLOAD, key=key, ct=content_type, exp=exp))

    async def presign_get(self, key: str, expires_in: int = 300) -> str:
        return self._url(DOWNLOAD, dict(op=DOWNLOAD, key=key, exp=int(time.time()) + expires_in))

    async def put_sidecar(self, key: str, body: str) -> None:
        await asyncio.to_thread(self._write_replace, self.path(f"{key}.metadata.json"), body.encode())

    @staticmethod
    def _write_replace(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.part-{secrets.token_hex(8)}")
        temp.write_bytes(data)
        os.replace(temp, path)

    async def head(self, key: str) -> int | None:
        try:
            return (await asyncio.to_thread(self.path(key).stat)).st_size
        except FileNotFoundError:
            return None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.path(key).unlink, missing_ok=True)

    async def get_bytes(self, key: str) -> bytes:
        return await asyncio.to_thread(self.path(key).read_bytes)

    def sweep_parts(self, max_age: float = PART_MAX_AGE_SECONDS) -> int:
        """Deletes temporary upload files left by interrupted uploads."""
        removed = 0
        cutoff = time.time() - max_age
        for part in self.root.rglob("*.part-*"):
            try:
                if part.is_file() and part.stat().st_mtime < cutoff:
                    part.unlink()
                    removed += 1
            except FileNotFoundError:
                pass
        return removed
