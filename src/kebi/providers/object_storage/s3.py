"""S3-compatible object storage adapter.

One adapter covers Railway Object Storage, AWS S3, Cloudflare R2, and
MinIO — only the endpoint URL differs. Uses `aioboto3` (async wrapper
over boto3) so writes don't block the FastAPI event loop.

`aioboto3` is imported lazily inside `__init__` so the module loads in
environments where boto3 isn't installed (local dev / tests using
`NullObjectStorage`). Production wiring always installs it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class S3ObjectStorage:
    """S3-compatible bucket adapter.

    Args mirror the S3 wire protocol — every S3-compatible provider
    accepts these. `endpoint_url=None` falls back to AWS S3's default.
    `region` is ignored by R2 but required by some providers; default
    `"auto"` works for R2 and AWS S3.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        access_key_id: str,
        secret_access_key: str,
        region: str = "auto",
    ) -> None:
        import aioboto3  # type: ignore[import-not-found]

        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._region = region
        self._session = aioboto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )

    def _client(self) -> Any:
        return self._session.client("s3", endpoint_url=self._endpoint_url)

    async def put_json(self, key: str, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )

    async def get_json(self, key: str) -> Any | None:
        from botocore.exceptions import ClientError  # type: ignore[import-not-found]

        async with self._client() as s3:
            try:
                resp = await s3.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in {"NoSuchKey", "404"}:
                    return None
                raise
            body = await resp["Body"].read()
        return json.loads(body.decode("utf-8"))

    async def list_prefix(self, prefix: str) -> list[str]:
        keys: list[str] = []
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for item in page.get("Contents", []) or []:
                    keys.append(item["Key"])
        return keys
