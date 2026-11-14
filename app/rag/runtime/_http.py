"""Bounded downloads and S3 Signature V4 without a blocking SDK."""

import asyncio
import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from rag.contracts import StepError
from rag.runtime._state import _get


async def _read(response, maximum):
    length = response.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > maximum:
                raise StepError("image_too_large", False)
        except ValueError as exc:
            raise StepError("invalid_image_response",False) from exc
    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > maximum:
            raise StepError("image_too_large",False)
        content.extend(chunk)
    return bytes(content)


def _signed(method, url, content, headers):
    settings = _get().settings
    access, secret = settings.s3_access_key.get_secret_value(), settings.s3_secret_key.get_secret_value()
    if not access or not secret:
        raise StepError("s3_not_configured",False)
    parsed = urlsplit(url)
    if parsed.scheme not in ("http","https") or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise StepError("s3_invalid_endpoint",False)
    now = datetime.now(UTC)
    day, timestamp = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(content).hexdigest()
    headers = {key.lower():str(value).strip() for key,value in headers.items()}
    headers.update(host=parsed.netloc, **{"x-amz-date":timestamp,"x-amz-content-sha256":digest})
    names = ";".join(sorted(headers))
    canonical = method + "\n" + parsed.path + "\n\n" + "".join(
        key+":"+headers[key]+"\n" for key in sorted(headers)) + "\n" + names + "\n" + digest
    scope = f"{day}/{settings.s3_region}/s3/aws4_request"
    to_sign = "AWS4-HMAC-SHA256\n"+timestamp+"\n"+scope+"\n"+hashlib.sha256(canonical.encode()).hexdigest()
    signing = ("AWS4"+secret).encode()
    for part in (day,settings.s3_region,"s3","aws4_request"):
        signing = hmac.new(signing,part.encode(),hashlib.sha256).digest()
    signature = hmac.new(signing,to_sign.encode(),hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={names}, Signature={signature}"
    return headers


async def _s3(method, key=None, content=b"", headers=None):
    state = _get()
    bucket = state.settings.s3_bucket
    url = state.settings.s3_endpoint_url.rstrip("/")+"/"+quote(bucket,safe="")
    if key is not None:
        url += "/"+quote(key,safe="/")
    signed = _signed(method,url,content,headers or {})
    try:
        async with asyncio.timeout(state.settings.io_timeout_s):
            async with state.http.stream(method,url,headers=signed,content=content) as response:
                data = await _read(response,state.settings.max_image_bytes)
                return response.status_code,dict(response.headers),data
    except (httpx.HTTPError,OSError,TimeoutError) as exc:
        raise StepError("s3_unavailable",True) from exc


def _s3_status(status):
    if status == 404:
        raise StepError("image_not_found",False)
    if status < 200 or status >= 300:
        raise StepError("s3_unavailable" if status==429 or status>=500 else "s3_rejected",
                        status==429 or status>=500)


async def _ensure_bucket():
    status, _, _ = await _s3("HEAD")
    if status == 404:
        region = _get().settings.s3_region
        content = (b"" if region=="us-east-1" else
            ('<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
             '<LocationConstraint>'+region+'</LocationConstraint></CreateBucketConfiguration>').encode())
        status, _, _ = await _s3("PUT",content=content)
        if status == 409:
            status, _, _ = await _s3("HEAD")
    _s3_status(status)
