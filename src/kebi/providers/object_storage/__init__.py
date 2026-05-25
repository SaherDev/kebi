"""Provider-agnostic object storage abstraction.

`ObjectStorageProtocol` is the seam between the app and whichever
S3-compatible bucket happens to be wired in (Railway, AWS S3,
Cloudflare R2, MinIO). Concrete adapters live alongside in this
package.

Today the surface is intentionally small — only what extraction needs
to write an append-only evidence ledger. Add methods as new consumers
appear; do not pre-add a CRUD API.
"""

from __future__ import annotations

from kebi.providers.object_storage.null import NullObjectStorage
from kebi.providers.object_storage.protocol import ObjectStorageProtocol
from kebi.providers.object_storage.s3 import S3ObjectStorage

__all__ = [
    "NullObjectStorage",
    "ObjectStorageProtocol",
    "S3ObjectStorage",
]
