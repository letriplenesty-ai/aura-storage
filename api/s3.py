"""S3 data-plane access. Keys stay server-side; browsers only get presigned URLs."""
import hashlib
import re

import boto3
from botocore.client import Config

def make_client(endpoint: str, access_key: str, secret_key: str, region: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def make_clients(public_endpoint: str, internal_endpoint: str,
                 access_key: str, secret_key: str, region: str):
    """(presign_client, ops_client): presigning is local crypto (no network),
    so it uses the public base; all real calls use the internal endpoint."""
    presign = make_client(public_endpoint, access_key, secret_key, region)
    ops = make_client(internal_endpoint, access_key, secret_key, region)
    return presign, ops


# SeaweedFS applies SSE-S3 only when the write asks for it (no default
# encryption on buckets AURA uses), so every upload must carry this header.
SSE_ALGORITHM = "AES256"
SSE_HEADER = "x-amz-server-side-encryption"


def presigned_put(client, bucket: str, key: str, content_type: str, expires: int,
                  content_length: int) -> str:
    """Presign a PUT bound to an exact ``Content-Length``.

    ``content_length`` becomes a *signed* header, so the storage backend rejects
    any upload whose length differs from the value the API authorised. This
    closes the "declare nothing, push a huge object before commit rejects it"
    window (#40): an uploader can no longer fill the volume beyond the size the
    API approved. The caller (upload-intent handler) enforces the upper bound
    (``settings.max_object_bytes``) before presigning.

    ``x-amz-server-side-encryption: AES256`` is signed too: the object is
    encrypted at rest by SeaweedFS (SSE-S3), and a PUT that omits the header
    fails the signature check (403). The caller must forward it, which the
    upload-intent response does through its ``headers`` map.

    Note: enforcement relies on the gateway validating signed headers; this is
    exercised live on the pilot, not in the offline suite.
    """
    params = {"Bucket": bucket, "Key": key, "ContentType": content_type,
              "ServerSideEncryption": SSE_ALGORITHM}
    if content_length is not None:
        params["ContentLength"] = content_length
    return client.generate_presigned_url(
        "put_object", Params=params, ExpiresIn=expires
    )


_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_IP_LIKE_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_RESERVED_PREFIXES = ("xn--", "sthree-", "amzn-s3-demo-")
_RESERVED_SUFFIXES = ("-s3alias", "--ol-s3", ".mrap", "--x-s3", "--table-s3")


def is_valid_bucket_name(name: str) -> bool:
    """Validate a general-purpose, DNS-compliant S3 bucket name (#41).

    Mirrors the AWS rules that matter for a default bucket derived from a
    space name/environment: 3-63 characters of lowercase letters, digits, dots
    and hyphens; starts and ends alphanumeric; no consecutive dots and no
    dot-adjacent hyphen; not shaped like an IPv4 address; not a reserved
    prefix/suffix. Rejecting invalid names up front avoids creating buckets that
    S3 would refuse later.
    """
    if not isinstance(name, str) or not _BUCKET_RE.match(name):
        return False
    if ".." in name or ".-" in name or "-." in name:
        return False
    if _IP_LIKE_RE.match(name):
        return False
    if name.startswith(_RESERVED_PREFIXES) or name.endswith(_RESERVED_SUFFIXES):
        return False
    return True


def presigned_get(client, bucket: str, key: str, expires: int,
                  content_disposition: str | None = None,
                  content_type: str | None = None) -> str:
    """Presign a GET, optionally overriding the response headers.

    ``content_disposition`` (``inline`` to render in the browser, ``attachment``
    to force a download) and ``content_type`` map to the get_object
    ``ResponseContentDisposition`` / ``ResponseContentType`` parameters, which
    S3 echoes as response headers on the presigned request. Both default to
    ``None`` so an existing caller (download-intent) produces exactly the URL it
    did before — no response override is added unless one is requested.
    """
    params = {"Bucket": bucket, "Key": key}
    if content_disposition is not None:
        params["ResponseContentDisposition"] = content_disposition
    if content_type is not None:
        params["ResponseContentType"] = content_type
    return client.generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires
    )


class ObjectTooLarge(Exception):
    """Raised by stream_sha256 when the object exceeds the configured limit."""


def stream_sha256(body, max_bytes: int, chunk_bytes: int = 1024 * 1024):
    """Hash a streaming S3 body without ever holding the whole object in memory.

    Returns ``(total_bytes, hex_digest)``. Raises :class:`ObjectTooLarge` as soon
    as the accumulated size exceeds ``max_bytes`` so the API aborts early (#37).
    """
    digest = hashlib.sha256()
    total = 0
    for chunk in body.iter_chunks(chunk_size=chunk_bytes):
        total += len(chunk)
        if total > max_bytes:
            raise ObjectTooLarge()
        digest.update(chunk)
    return total, digest.hexdigest()
