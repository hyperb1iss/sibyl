"""Bounded outbound HTTP fetches with DNS rebinding and private-IP guards."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import ParseResult, urljoin, urlparse, urlunsplit

SAFE_FETCH_MAX_BYTES = 2 * 1024 * 1024
SAFE_FETCH_MAX_REDIRECTS = 5
SAFE_FETCH_TIMEOUT_SECONDS = 15.0
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ALLOWED_METHODS = {"GET", "HEAD"}


@dataclass(frozen=True, slots=True)
class SafeFetchResponse:
    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes


async def safe_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    allow_private_network: bool = False,
    follow_redirects: bool = True,
    max_bytes: int = SAFE_FETCH_MAX_BYTES,
    max_redirects: int = SAFE_FETCH_MAX_REDIRECTS,
    timeout: float = SAFE_FETCH_TIMEOUT_SECONDS,
    user_agent: str = "Sibyl/1.0",
    accept: str = "*/*",
) -> SafeFetchResponse:
    request_method = method.upper()
    if request_method not in _ALLOWED_METHODS:
        msg = f"safe_fetch method is unsupported: {method}"
        raise ValueError(msg)

    current_url = normalize_safe_url(url, allow_private_network=allow_private_network)
    for redirect_count in range(max_redirects + 1):
        response = await _fetch_once(
            current_url,
            method=request_method,
            headers=headers or {},
            allow_private_network=allow_private_network,
            max_bytes=max_bytes,
            timeout=timeout,
            user_agent=user_agent,
            accept=accept,
        )
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        if not follow_redirects:
            return response

        location = response.headers.get("location")
        if not location:
            return response
        if redirect_count >= max_redirects:
            raise ValueError("URL redirected too many times")
        current_url = normalize_safe_url(
            urljoin(response.url, location),
            allow_private_network=allow_private_network,
        )

    raise ValueError("URL redirected too many times")


def normalize_safe_url(url: str, *, allow_private_network: bool = False) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        msg = f"URL must be http(s): {url}"
        raise ValueError(msg)
    host = parsed.hostname
    if not host:
        msg = f"URL must include a host: {url}"
        raise ValueError(msg)
    host = host.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        msg = f"URL port is invalid: {url}"
        raise ValueError(msg) from exc
    if not allow_private_network:
        _reject_private_host(host)
    netloc_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc_host = f"{netloc_host}:{port}"
    path = parsed.path or ""
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc_host, path, parsed.query, ""))


def decode_safe_fetch_body(
    body: bytes,
    headers: Mapping[str, str],
    *,
    max_bytes: int = SAFE_FETCH_MAX_BYTES,
) -> str:
    encoding = headers.get("content-encoding", "").lower()
    if encoding == "gzip":
        body = _bounded_inflate(body, wbits=16 + zlib.MAX_WBITS, max_bytes=max_bytes)
    elif encoding == "deflate":
        body = _bounded_inflate(body, wbits=zlib.MAX_WBITS, max_bytes=max_bytes)
    elif encoding and encoding != "identity":
        raise ValueError(f"Unsupported URL content encoding: {encoding}")

    charset = "utf-8"
    content_type = headers.get("content-type", "")
    for part in content_type.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip('"')
            break
    return body.decode(charset, errors="replace")


async def _fetch_once(
    url: str,
    *,
    method: str,
    headers: Mapping[str, str],
    allow_private_network: bool,
    max_bytes: int,
    timeout: float,
    user_agent: str,
    accept: str,
) -> SafeFetchResponse:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"URL must be http(s): {url}")
    host = parsed.hostname.lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connect_host = host
    if not allow_private_network:
        connect_host = _public_connect_host(host)
    return await _http1_fetch(
        url=url,
        parsed=parsed,
        connect_host=connect_host,
        host=host,
        port=port,
        method=method,
        headers=headers,
        max_bytes=max_bytes,
        timeout=timeout,
        user_agent=user_agent,
        accept=accept,
    )


async def _http1_fetch(
    *,
    url: str,
    parsed: ParseResult,
    connect_host: str,
    host: str,
    port: int,
    method: str,
    headers: Mapping[str, str],
    max_bytes: int,
    timeout: float,
    user_agent: str,
    accept: str,
) -> SafeFetchResponse:
    ssl_context = ssl.create_default_context() if parsed.scheme == "https" else None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            connect_host,
            port,
            ssl=ssl_context,
            server_hostname=host if ssl_context is not None else None,
        ),
        timeout=timeout,
    )
    try:
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        request_lines = [
            f"{method} {target} HTTP/1.1",
            f"Host: {_host_header(host, port, parsed.scheme)}",
            f"User-Agent: {user_agent}",
            f"Accept: {accept}",
            "Accept-Encoding: identity",
            "Connection: close",
        ]
        for name, value in headers.items():
            lower_name = name.strip().lower()
            if lower_name in {
                "accept",
                "accept-encoding",
                "connection",
                "content-length",
                "host",
                "transfer-encoding",
                "user-agent",
            }:
                continue
            request_lines.append(f"{name}: {value}")
        request = "\r\n".join([*request_lines, "", ""])
        writer.write(request.encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        raw_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        status_code, response_headers = _parse_response_headers(raw_headers)
        body = (
            b""
            if method == "HEAD"
            else await _read_response_body(
                reader,
                response_headers,
                max_bytes=max_bytes,
                timeout=timeout,
            )
        )
        return SafeFetchResponse(
            url=url,
            status_code=status_code,
            headers=response_headers,
            body=body,
        )
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _host_header(host: str, port: int, scheme: str) -> str:
    bracketed_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return bracketed_host
    return f"{bracketed_host}:{port}"


def _parse_response_headers(raw_headers: bytes) -> tuple[int, dict[str, str]]:
    header_text = raw_headers.decode("iso-8859-1")
    lines = header_text.split("\r\n")
    status_parts = lines[0].split(maxsplit=2)
    if len(status_parts) < 2 or not status_parts[1].isdigit():
        raise ValueError("URL returned an invalid HTTP response")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return int(status_parts[1]), headers


async def _read_response_body(
    reader: asyncio.StreamReader,
    headers: Mapping[str, str],
    *,
    max_bytes: int,
    timeout: float,
) -> bytes:
    transfer_encoding = headers.get("transfer-encoding", "").lower()
    if "chunked" in transfer_encoding:
        return await _read_chunked_body(reader, max_bytes=max_bytes, timeout=timeout)

    content_length = headers.get("content-length")
    if content_length and content_length.isdigit():
        expected_size = int(content_length)
        if expected_size > max_bytes:
            raise ValueError("URL response is too large")
        return await asyncio.wait_for(reader.readexactly(expected_size), timeout=timeout)

    chunks: list[bytes] = []
    total_size = 0
    while chunk := await asyncio.wait_for(reader.read(65536), timeout=timeout):
        total_size += len(chunk)
        if total_size > max_bytes:
            raise ValueError("URL response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_chunked_body(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int,
    timeout: float,
) -> bytes:
    chunks: list[bytes] = []
    total_size = 0
    while True:
        size_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        size_text = size_line.split(b";", 1)[0].strip()
        try:
            chunk_size = int(size_text, 16)
        except ValueError as exc:
            raise ValueError("URL returned an invalid chunked response") from exc
        if chunk_size == 0:
            await asyncio.wait_for(reader.readline(), timeout=timeout)
            break
        total_size += chunk_size
        if total_size > max_bytes:
            raise ValueError("URL response is too large")
        chunks.append(await asyncio.wait_for(reader.readexactly(chunk_size), timeout=timeout))
        await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
    return b"".join(chunks)


def _bounded_inflate(body: bytes, *, wbits: int, max_bytes: int) -> bytes:
    decompressor = zlib.decompressobj(wbits)
    inflated = decompressor.decompress(body, max_bytes + 1)
    if len(inflated) > max_bytes or decompressor.unconsumed_tail or not decompressor.eof:
        raise ValueError("URL response is too large")
    inflated += decompressor.flush()
    if len(inflated) > max_bytes:
        raise ValueError("URL response is too large")
    return inflated


def _reject_private_host(host: str) -> None:
    normalized_host = _normalized_host(host)
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
    ):
        raise ValueError(f"URL host is private: {host}")
    if _is_private_address(normalized_host):
        raise ValueError(f"URL host is private: {host}")
    for resolved_host in _resolve_host_addresses(normalized_host):
        if _is_private_address(resolved_host):
            raise ValueError(f"URL host is private: {host}")


def _public_connect_host(host: str) -> str:
    normalized_host = _normalized_host(host)
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
    ):
        raise ValueError(f"URL host is private: {host}")
    if _is_private_address(normalized_host):
        raise ValueError(f"URL host is private: {host}")
    addresses = _resolve_host_addresses(normalized_host)
    if not addresses:
        raise ValueError(f"URL host could not be resolved: {host}")
    for resolved_host in addresses:
        if _is_private_address(resolved_host):
            raise ValueError(f"URL host is private: {host}")
    return addresses[0]


def _normalized_host(host: str) -> str:
    return host.strip("[]").strip().lower().rstrip(".")


def _is_private_address(value: str) -> bool:
    address = _coerce_ip_address(value)
    if address is None:
        return False
    return not address.is_global or address.is_multicast


def _coerce_ip_address(value: str) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(value)
    except ValueError:
        pass

    try:
        if value.isdigit():
            return IPv4Address(int(value, 10))
        if value.startswith("0x"):
            return IPv4Address(int(value, 16))
        if len(value) > 1 and value.startswith("0") and all(char in "01234567" for char in value):
            return IPv4Address(int(value, 8))
    except ValueError:
        return None
    return None


def _resolve_host_addresses(host: str) -> list[str]:
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    addresses: list[str] = []
    for result in results:
        sockaddr = result[4]
        if isinstance(sockaddr, tuple) and sockaddr:
            addresses.append(str(sockaddr[0]))
    return list(dict.fromkeys(addresses))


__all__ = [
    "SAFE_FETCH_MAX_BYTES",
    "SAFE_FETCH_MAX_REDIRECTS",
    "SAFE_FETCH_TIMEOUT_SECONDS",
    "SafeFetchResponse",
    "decode_safe_fetch_body",
    "normalize_safe_url",
    "safe_fetch",
]
