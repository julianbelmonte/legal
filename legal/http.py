"""HTTP client helpers for legal source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from time import sleep
from typing import Any, Mapping

import httpx

from legal.errors import LegalCliError, network_error, not_found, parse_error, source_unavailable
from legal.models import Provenance


DEFAULT_TIMEOUT = 30.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)
DEFAULT_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 0.25
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}
TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
BODY_SNIPPET_LIMIT = 500


@dataclass(frozen=True)
class HttpSettings:
    timeout: float | httpx.Timeout = DEFAULT_TIMEOUT
    user_agent: str = USER_AGENT
    retries: int = DEFAULT_RETRIES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF
    headers: Mapping[str, str] | None = None


def build_client(
    settings: HttpSettings | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    base_url: str | httpx.URL = "",
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
) -> httpx.Client:
    """Create a portable httpx client with conservative defaults."""
    resolved = settings or HttpSettings()
    resolved_headers = {
        **DEFAULT_HEADERS,
        "User-Agent": resolved.user_agent,
        **(resolved.headers or {}),
        **(headers or {}),
    }
    if transport is not None:
        # A transport and a proxy are mutually exclusive in httpx; transport wins
        # (keeps offline tests injecting MockTransport working).
        resolved_proxy: str | None = None
    elif proxy is not None:
        resolved_proxy = proxy
    else:
        from legal.providers.proxy import resolve_proxy

        resolved_proxy = resolve_proxy()
    client_kwargs: dict[str, Any] = {
        "base_url": base_url,
        "headers": resolved_headers,
        "follow_redirects": True,
        "timeout": resolved.timeout,
        "transport": transport,
    }
    if resolved_proxy is not None:
        client_kwargs["proxy"] = resolved_proxy
    return httpx.Client(**client_kwargs)


class LegalHttpClient:
    """Small sync HTTP client wrapper with legal-tool error normalization."""

    def __init__(
        self,
        settings: HttpSettings | None = None,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        base_url: str | httpx.URL = "",
        headers: Mapping[str, str] | None = None,
        proxy: str | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")
        self.settings = settings or HttpSettings()
        self._client = client or build_client(
            self.settings,
            transport=transport,
            base_url=base_url,
            headers=headers,
            proxy=proxy,
        )
        self._owns_client = client is None

    def __enter__(self) -> "LegalHttpClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    @property
    def headers(self) -> httpx.Headers:
        return self._client.headers

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        """Run a request, retry transient failures, and raise LegalCliError."""
        attempts = max(0, self.settings.retries) + 1
        last_request_error: httpx.RequestError | None = None

        for attempt in range(attempts):
            try:
                response = self._client.request(method, url, **kwargs)
                if _is_retryable_status(response.status_code) and attempt < attempts - 1:
                    response.close()
                    self._sleep_before_retry(attempt)
                    continue
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                raise _legal_error_from_response(exc.response) from exc
            except httpx.RequestError as exc:
                last_request_error = exc
                if isinstance(exc, RETRYABLE_EXCEPTIONS) and attempt < attempts - 1:
                    self._sleep_before_retry(attempt)
                    continue
                raise _legal_error_from_request(exc) from exc

        if last_request_error is not None:
            raise _legal_error_from_request(last_request_error) from last_request_error
        raise RuntimeError("request loop exited without response or error")

    def fetch_json(self, method: str, url: str | httpx.URL, **kwargs: Any) -> Any:
        response = self.request(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise parse_error(
                "source response was not valid JSON",
                details=_response_evidence(response, include_body=True),
                provenance=_response_provenance(response),
            ) from exc

    def fetch_text(self, method: str, url: str | httpx.URL, **kwargs: Any) -> str:
        return self.request(method, url, **kwargs).text

    def fetch_bytes(self, method: str, url: str | httpx.URL, **kwargs: Any) -> bytes:
        return self.request(method, url, **kwargs).content

    def get_json(self, url: str | httpx.URL, **kwargs: Any) -> Any:
        return self.fetch_json("GET", url, **kwargs)

    def post_json(self, url: str | httpx.URL, **kwargs: Any) -> Any:
        return self.fetch_json("POST", url, **kwargs)

    def get_text(self, url: str | httpx.URL, **kwargs: Any) -> str:
        return self.fetch_text("GET", url, **kwargs)

    def post_text(self, url: str | httpx.URL, **kwargs: Any) -> str:
        return self.fetch_text("POST", url, **kwargs)

    def get_bytes(self, url: str | httpx.URL, **kwargs: Any) -> bytes:
        return self.fetch_bytes("GET", url, **kwargs)

    def head(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("HEAD", url, **kwargs)

    def _sleep_before_retry(self, attempt: int) -> None:
        if self.settings.retry_backoff <= 0:
            return
        sleep(self.settings.retry_backoff * (2**attempt))


def _is_retryable_status(status_code: int) -> bool:
    return status_code in TRANSIENT_STATUS_CODES or 500 <= status_code <= 599


def _legal_error_from_response(response: httpx.Response) -> LegalCliError:
    details = _response_evidence(response, include_body=True)
    provenance = _response_provenance(response)
    status_code = response.status_code

    if status_code == 404:
        return not_found(
            f"HTTP {status_code} while requesting {response.url}",
            details=details,
            provenance=provenance,
        )
    if _is_retryable_status(status_code):
        return source_unavailable(
            f"HTTP {status_code} while requesting {response.url}",
            details=details,
            provenance=provenance,
        )
    return LegalCliError(
        code="source_unavailable",
        message=f"HTTP {status_code} while requesting {response.url}",
        retryable=False,
        details=details,
        provenance=provenance,
    )


def _legal_error_from_request(exc: httpx.RequestError) -> LegalCliError:
    request = _request_from_error(exc)
    url = str(request.url) if request is not None else None
    method = request.method if request is not None else None
    details: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    if url is not None:
        details["url"] = url
    if method is not None:
        details["method"] = method
    return network_error(
        "network request failed" if url is None else f"network request failed for {url}",
        details=details,
        provenance=Provenance.now(
            source_urls=[url] if url else [],
            fetched_urls=[url] if url else [],
            raw={"method": method, "error_type": type(exc).__name__},
        ),
    )


def _request_from_error(exc: httpx.RequestError) -> httpx.Request | None:
    try:
        return exc.request
    except RuntimeError:
        return None


def _response_evidence(response: httpx.Response, *, include_body: bool = False) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "url": str(response.url),
        "method": response.request.method,
        "status_code": response.status_code,
        "reason_phrase": response.reason_phrase,
    }
    useful_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"content-type", "location", "retry-after"}
    }
    if useful_headers:
        evidence["headers"] = useful_headers
    if include_body:
        body_snippet = _body_snippet(response)
        if body_snippet:
            evidence["body_snippet"] = body_snippet
    return evidence


def _response_provenance(response: httpx.Response) -> Provenance:
    return Provenance.now(
        source_urls=[str(response.url)],
        fetched_urls=[str(response.url)],
        raw={
            "method": response.request.method,
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
        },
    )


def _body_snippet(response: httpx.Response) -> str:
    text = response.text.strip()
    if len(text) <= BODY_SNIPPET_LIMIT:
        return text
    return text[:BODY_SNIPPET_LIMIT]
