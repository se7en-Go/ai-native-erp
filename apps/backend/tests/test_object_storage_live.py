from __future__ import annotations

from uuid import uuid4

import httpx

from erp_core.shared.config import get_settings
from erp_core.shared.storage import ObjectStorageService


async def test_object_storage_uploads_and_fetches_presigned_url() -> None:
    storage = ObjectStorageService(get_settings())
    await storage.ensure_bucket_exists()
    key = f"live-probe/{uuid4().hex}.bin"
    payload = b"erp-live-s3-bytes-\x00\xff"
    stored_key = await storage.upload_bytes(
        key,
        payload,
        content_type="application/octet-stream",
    )
    assert stored_key == key
    url = await storage.generate_presigned_get_url(key, expires_in=300)
    assert url.startswith("http://")
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.content == payload
