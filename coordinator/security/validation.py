"""
Input validation utilities for security.

Prevents injection attacks, ReDoS, SSRF, and malformed data.
"""
import re
import ipaddress
import logging
import socket
from typing import Optional, Tuple, List
from urllib.parse import urlparse, urljoin
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


# Heuristic structural detector for nested-quantifier backtracking bombs,
# used as a last-resort gate when no hard-timeout mechanism is available.
# Catches the (X+)+, (X*)+, (X|X)+, (X{n,m})+ families regardless of the inner
# token — far broader than a literal allow/deny list, though still a heuristic.
_REDOS_STRUCTURAL = re.compile(
    r"\([^)]*[+*]\s*[+*]?\s*\)\s*[+*]"          # (..+..)+  / (..*..)*
    r"|\([^)]*\{\d+(?:,\d*)?\}[^)]*\)\s*[+*]"    # (..{n,m}..)+
    r"|\([^)|]+\|[^)]*\)\s*[+*]"                 # (a|a)+  alternation then quantifier
)


class InputValidator:
    """Validates user inputs to prevent security issues."""

    # Allowed URL schemes
    ALLOWED_SCHEMES = {'http', 'https'}

    # Private IP ranges to block in production
    PRIVATE_IP_PATTERNS = [
        r'^127\.',          # Localhost
        r'^10\.',           # Private Class A
        r'^172\.(1[6-9]|2[0-9]|3[01])\.',  # Private Class B
        r'^192\.168\.',     # Private Class C
        r'^169\.254\.',     # Link-local
        r'^::1$',           # IPv6 localhost
        r'^fe80:',          # IPv6 link-local
        r'^fc00:',          # IPv6 unique local
    ]

    # Maximum regex complexity (approximate)
    MAX_REGEX_LENGTH = 500
    MAX_REGEX_NESTING = 5

    @classmethod
    def validate_url(cls, url: str, allow_private: bool = False) -> str:
        """
        Validate URL for security.

        Args:
            url: URL to validate
            allow_private: Whether to allow private IPs (default False for production)

        Returns:
            Validated URL

        Raises:
            HTTPException: If URL is invalid or insecure

        Example:
            >>> InputValidator.validate_url("https://example.com")
            'https://example.com'
            >>> InputValidator.validate_url("file:///etc/passwd")
            HTTPException: Invalid URL scheme
        """
        try:
            parsed = urlparse(url)

            # Check scheme
            if parsed.scheme not in cls.ALLOWED_SCHEMES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid URL scheme. Only {', '.join(cls.ALLOWED_SCHEMES)} allowed."
                )

            # Check for hostname
            if not parsed.hostname:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="URL must have a hostname"
                )

            # Block private IPs in production if not allowed
            if not allow_private:
                hostname = parsed.hostname.lower()
                for pattern in cls.PRIVATE_IP_PATTERNS:
                    if re.match(pattern, hostname):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Private IP addresses are not allowed"
                        )

            # Block localhost
            if parsed.hostname.lower() in ('localhost', '0.0.0.0'):
                if not allow_private:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Localhost is not allowed"
                    )

            return url

        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"URL validation failed for '{url}': {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid URL format"
            )

    @classmethod
    def _is_private_ip(cls, ip_str: str) -> bool:
        """Check if a resolved IP address is private/internal."""
        try:
            ip = ipaddress.ip_address(ip_str)
            return (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            )
        except ValueError:
            return True

    @classmethod
    def resolve_and_validate(cls, url: str, allow_private: bool = False) -> Tuple[str, List[str]]:
        """
        Validate a URL and resolve its hostname, checking EVERY resolved IP
        against private/internal ranges.

        Returns a tuple of (validated_url, [safe_ips]). The IP list lets callers
        pin the actual TCP connection to a vetted address, closing the
        DNS-rebinding TOCTOU window between validation and connect.

        When ``allow_private`` is True (ALLOW_PRIVATE_TARGETS opt-in), resolution is still
        attempted but private IPs are permitted; the returned IP list may be
        empty and callers should fall back to normal hostname resolution.
        """
        url = cls.validate_url(url, allow_private=allow_private)

        parsed = urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="URL must have a hostname"
            )

        # If hostname is already a literal IP, validate it directly.
        try:
            ipaddress.ip_address(hostname)
            is_literal_ip = True
        except ValueError:
            is_literal_ip = False

        safe_ips: List[str] = []
        try:
            resolved_ips = socket.getaddrinfo(hostname, None)
            for _family, _, _, _, sockaddr in resolved_ips:
                ip_str = sockaddr[0]
                if cls._is_private_ip(ip_str):
                    if not allow_private:
                        logger.warning(
                            f"SSRF blocked: {hostname} resolves to private IP {ip_str}"
                        )
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="URL resolves to a private/internal IP address"
                        )
                    # allow_private: keep it as a candidate but it is fine.
                    safe_ips.append(ip_str)
                else:
                    safe_ips.append(ip_str)
        except HTTPException:
            raise
        except socket.gaierror:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not resolve hostname"
            )
        except Exception as e:
            logger.warning(f"DNS resolution failed for {hostname}: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="URL validation failed"
            )

        # De-duplicate while preserving order.
        seen = set()
        deduped = []
        for ip in safe_ips:
            if ip not in seen:
                seen.add(ip)
                deduped.append(ip)
        return url, deduped

    @classmethod
    def validate_url_with_dns(cls, url: str, allow_private: bool = False) -> str:
        """
        Validate URL with DNS resolution to prevent DNS rebinding and SSRF.

        Resolves the hostname and checks the actual IP address against private ranges.
        This prevents attacks where a domain initially resolves to a public IP
        but can be changed to an internal IP after validation.
        """
        if allow_private:
            return cls.validate_url(url, allow_private=allow_private)
        validated_url, _ips = cls.resolve_and_validate(url, allow_private=allow_private)
        return validated_url

    # Maximum redirect hops to follow during a guarded fetch.
    MAX_REDIRECTS = 5

    @classmethod
    async def safe_fetch(
        cls,
        client,
        url: str,
        *,
        allow_private: bool = False,
        method: str = "GET",
        timeout: float = 30.0,
        headers: Optional[dict] = None,
        max_redirects: Optional[int] = None,
        **request_kwargs,
    ):
        """
        SSRF-hardened HTTP fetch.

        - Validates the initial URL via DNS resolution and pins the connection
          to a vetted IP (closing the DNS-rebinding TOCTOU window).
        - Disables httpx automatic redirect following and follows redirects
          MANUALLY, re-validating each hop's resolved IP before connecting.

        ``client`` is an httpx.AsyncClient (e.g. the shared pooled client).
        Returns the final httpx.Response (redirects already resolved).

        Raises HTTPException(400) if any hop fails validation, or
        HTTPException(502) on too many redirects.
        """
        import httpx  # local import; validation.py stays import-light

        if max_redirects is None:
            max_redirects = cls.MAX_REDIRECTS

        current_url = url
        base_headers = dict(headers or {})
        request_kwargs = dict(request_kwargs)

        for _hop in range(max_redirects + 1):
            validated_url, safe_ips = cls.resolve_and_validate(
                current_url, allow_private=allow_private
            )
            parsed = urlparse(validated_url)

            req_headers = dict(base_headers)
            connect_url = validated_url

            # Pin the connection to a validated IP so the socket cannot be
            # rebound to an internal address between our check and the connect.
            extensions = {}
            if safe_ips:
                pin_ip = safe_ips[0]
                # Preserve the original Host header / SNI for routing + TLS.
                host_header = parsed.hostname
                if parsed.port:
                    host_header = f"{host_header}:{parsed.port}"
                req_headers.setdefault("Host", host_header)
                # Replace the netloc host with the literal IP for the actual
                # connection; keep the original hostname for TLS SNI.
                netloc = pin_ip
                if parsed.port:
                    netloc = f"{pin_ip}:{parsed.port}"
                connect_url = parsed._replace(netloc=netloc).geturl()
                if parsed.scheme == "https":
                    extensions["sni_hostname"] = parsed.hostname

            request = client.build_request(
                method,
                connect_url,
                headers=req_headers,
                timeout=timeout,
                **request_kwargs,
            )
            if extensions:
                request.extensions.update(extensions)

            response = await client.send(request, follow_redirects=False)

            if response.is_redirect and response.next_request is not None:
                location = response.headers.get("location", "")
                status_code = response.status_code
                await response.aclose()
                if not location:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="Redirect without Location header",
                    )
                # Resolve relative redirects against the URL we requested.
                next_url = urljoin(current_url, location)

                # Never carry credentials across an origin boundary. This loop
                # follows redirects by hand (httpx's follow_redirects is off, so
                # its own cross-origin Authorization stripping never runs) — so
                # the stripping has to happen here. See services/safe_fetch.py
                # for the same guard on the other redirect loop.
                from services.safe_fetch import (
                    _is_cross_origin,
                    _strip_credential_headers,
                )

                if _is_cross_origin(current_url, next_url):
                    base_headers = _strip_credential_headers(base_headers)

                # 301/302/303 downgrade the method to GET; drop the body with it.
                if status_code in (301, 302, 303) and method.upper() not in ("GET", "HEAD"):
                    method = "GET"
                    request_kwargs = {
                        k: v for k, v in request_kwargs.items()
                        if k not in ("json", "content", "data", "files")
                    }

                current_url = next_url
                continue

            return response

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Too many redirects",
        )

    @classmethod
    def validate_regex(cls, pattern: Optional[str]) -> Optional[str]:
        """
        Validate regex pattern for ReDoS prevention.

        Args:
            pattern: Regex pattern to validate

        Returns:
            Validated pattern or None

        Raises:
            HTTPException: If regex is too complex

        Example:
            >>> InputValidator.validate_regex("simple.*pattern")
            'simple.*pattern'
            >>> InputValidator.validate_regex("(a+)+b" * 100)
            HTTPException: Regex too complex
        """
        if not pattern:
            return None

        # Check length
        if len(pattern) > cls.MAX_REGEX_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Regex pattern too long (max {cls.MAX_REGEX_LENGTH} characters)"
            )

        # Check nesting depth (count nested groups)
        nesting_depth = 0
        max_depth = 0
        for char in pattern:
            if char == '(':
                nesting_depth += 1
                max_depth = max(max_depth, nesting_depth)
            elif char == ')':
                nesting_depth -= 1

        if max_depth > cls.MAX_REGEX_NESTING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Regex pattern too complex (max {cls.MAX_REGEX_NESTING} nesting levels)"
            )

        # Try to compile it first (cheap, no execution).
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid regex pattern: {str(e)}"
            )

        # ReDoS defense: the old literal "dangerous pattern" blocklist was
        # trivially bypassable (e.g. ``(a|a)+``, ``(a{1,2})+``). We replace it
        # with two stronger, cheap mechanisms layered on top of the length +
        # nesting caps above:
        #
        #   1. A structural heuristic that rejects the nested-quantifier
        #      backtracking-bomb families ((X+)+, (X*)*, (X|X)+, (X{n,m})+) for
        #      ANY inner token — not a hand-maintained literal list.
        #   2. When the third-party ``regex`` module is installed, a real
        #      timeout-bounded probe match against an adversarial input. This is
        #      the authoritative check: ``regex`` releases the GIL and honors
        #      ``timeout=``. (A pure-``re`` thread timeout does NOT work — CPython
        #      holds the GIL during C-level backtracking, so a watchdog thread is
        #      never scheduled — hence we do not attempt it here.)
        #
        # Residual risk: if ``regex`` is NOT installed, enforcement relies on the
        # structural heuristic + caps. A novel catastrophic construct outside the
        # heuristic's families could still slip through, but the MAX_REGEX_LENGTH
        # / MAX_REGEX_NESTING caps bound the worst-case blast radius. Install the
        # ``regex`` package to get the hard-timeout guarantee.
        REDOS_TIMEOUT_S = 0.5
        # Adversarial probe: a long run that maximizes backtracking for the
        # classic (x+)+ / (x|x)+ class of patterns.
        probe = ("a" * 64) + "!" + ("a" * 64)

        # 1) Cheap structural gate — rejects nested-quantifier bombs.
        if _REDOS_STRUCTURAL.search(pattern):
            logger.warning(f"ReDoS structural heuristic rejected pattern: {pattern!r}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Regex pattern may cause excessive backtracking (ReDoS risk)"
            )

        # 2) Authoritative timeout-bounded probe when `regex` is available.
        try:
            import regex as _regex  # type: ignore
        except ImportError:
            _regex = None

        if _regex is not None:
            try:
                _regex.compile(pattern).search(probe, timeout=REDOS_TIMEOUT_S)
            except _regex.error:
                # Valid for `re` but not `regex`; the `re` compile already
                # succeeded above, so accept it.
                pass
            except TimeoutError:
                logger.warning(f"ReDoS timeout (regex module) on pattern: {pattern!r}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Regex pattern may cause excessive backtracking (ReDoS risk)"
                )

        return pattern

    # Hard wall-clock budget (seconds) for any single user-supplied regex match
    # executed at request/run time. Mirrors the probe budget in validate_regex.
    REGEX_EXEC_TIMEOUT_S = 0.5

    # Cap the input fed to a regex engine. Catastrophic backtracking scales with
    # input length, so bounding it bounds the worst-case blast radius even when
    # the `regex` hard-timeout module is unavailable.
    MAX_REGEX_INPUT_LENGTH = 100_000

    @classmethod
    def safe_regex_search(
        cls,
        pattern: str,
        text: str,
        *,
        flags: int = 0,
        validate: bool = True,
    ):
        """
        ReDoS-safe ``re.search`` replacement.

        - Optionally re-validates the pattern (length/nesting/structural caps).
        - Caps ``text`` length before matching.
        - Executes under a hard wall-clock timeout when the third-party ``regex``
          module is installed (it releases the GIL and honors ``timeout=``). A
          pure-``re`` thread watchdog does NOT work — CPython holds the GIL during
          C-level backtracking — so without ``regex`` we rely on the validation
          caps + the length cap to bound the worst case.

        Returns a match object (``regex`` or ``re``) or ``None``. On timeout the
        match is abandoned and ``None`` is returned (fail-closed: no match).
        """
        if validate:
            cls.validate_regex(pattern)
        if text is None:
            return None
        if len(text) > cls.MAX_REGEX_INPUT_LENGTH:
            text = text[: cls.MAX_REGEX_INPUT_LENGTH]

        try:
            import regex as _regex  # type: ignore
        except ImportError:
            _regex = None

        if _regex is not None:
            try:
                return _regex.compile(pattern, flags=flags).search(
                    text, timeout=cls.REGEX_EXEC_TIMEOUT_S
                )
            except _regex.error:
                # `regex` rejected a construct `re` accepts — fall back to `re`.
                pass
            except TimeoutError:
                logger.warning("ReDoS timeout (search) on pattern: %r", pattern)
                return None

        return re.compile(pattern, flags=flags).search(text)

    @classmethod
    def safe_regex_findall(
        cls,
        pattern: str,
        text: str,
        *,
        flags: int = 0,
        validate: bool = True,
    ) -> list:
        """
        ReDoS-safe ``re.findall`` replacement (same guarantees as
        :meth:`safe_regex_search`). On timeout returns an empty list.
        """
        if validate:
            cls.validate_regex(pattern)
        if text is None:
            return []
        if len(text) > cls.MAX_REGEX_INPUT_LENGTH:
            text = text[: cls.MAX_REGEX_INPUT_LENGTH]

        try:
            import regex as _regex  # type: ignore
        except ImportError:
            _regex = None

        if _regex is not None:
            try:
                return _regex.compile(pattern, flags=flags).findall(
                    text, timeout=cls.REGEX_EXEC_TIMEOUT_S
                )
            except _regex.error:
                pass
            except TimeoutError:
                logger.warning("ReDoS timeout (findall) on pattern: %r", pattern)
                return []

        return re.compile(pattern, flags=flags).findall(text)

    @classmethod
    def safe_regex_sub(
        cls,
        pattern: str,
        replacement: str,
        text: str,
        *,
        flags: int = 0,
        validate: bool = True,
    ) -> str:
        """
        ReDoS-safe ``re.sub`` replacement (same guarantees as
        :meth:`safe_regex_search`).

        On timeout the substitution is abandoned and the input is returned
        UNCHANGED. That is the fail-closed choice for the callers here: the
        patterns using this are "strip this noise before hashing/comparing", so
        leaving the text intact yields a spurious change notification — noisy, but
        safe — whereas returning empty text would silently erase content the
        operator is monitoring.
        """
        if validate:
            cls.validate_regex(pattern)
        if not text:
            return text
        if len(text) > cls.MAX_REGEX_INPUT_LENGTH:
            text = text[: cls.MAX_REGEX_INPUT_LENGTH]

        try:
            import regex as _regex  # type: ignore
        except ImportError:
            _regex = None

        if _regex is not None:
            try:
                return _regex.compile(pattern, flags=flags).sub(
                    replacement, text, timeout=cls.REGEX_EXEC_TIMEOUT_S
                )
            except _regex.error:
                # `regex` rejected a construct `re` accepts — fall back to `re`.
                pass
            except TimeoutError:
                logger.warning("ReDoS timeout (sub) on pattern: %r", pattern)
                return text

        return re.compile(pattern, flags=flags).sub(replacement, text)

    @classmethod
    def validate_css_selector(cls, selector: Optional[str]) -> Optional[str]:
        """
        Validate CSS selector.

        Args:
            selector: CSS selector to validate

        Returns:
            Validated selector or None

        Raises:
            HTTPException: If selector is too long or complex
        """
        if not selector:
            return None

        MAX_SELECTOR_LENGTH = 500

        if len(selector) > MAX_SELECTOR_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"CSS selector too long (max {MAX_SELECTOR_LENGTH} characters)"
            )

        return selector

    @classmethod
    def sanitize_diff_snippet(cls, snippet: Optional[str], max_length: int = 10000) -> Optional[str]:
        """
        Sanitize diff snippet for safe storage and display.

        Args:
            snippet: Diff snippet to sanitize
            max_length: Maximum length (default 10000)

        Returns:
            Sanitized snippet or None
        """
        if not snippet:
            return None

        try:
            # Remove null bytes
            snippet = snippet.replace('\x00', '')

            # Ensure valid UTF-8
            snippet = snippet.encode('utf-8', errors='ignore').decode('utf-8')

            # Limit length
            if len(snippet) > max_length:
                snippet = snippet[:max_length] + '... (truncated)'

            return snippet

        except Exception as e:
            logger.warning(f"Could not sanitize diff_snippet: {e}")
            return None


def escape_like(s: str) -> str:
    """Escape SQL LIKE special characters."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sanitize_error_message(message: str, include_details: bool = False) -> str:
    """
    Sanitize error messages to prevent information leakage.

    Args:
        message: Original error message
        include_details: Whether to include details (only for authenticated users)

    Returns:
        Sanitized error message

    Example:
        >>> sanitize_error_message("Agent abc123 not found")
        'Resource not found'
        >>> sanitize_error_message("Agent abc123 not found", include_details=True)
        'Agent not found'
    """
    if not include_details:
        # Generic messages for unauthenticated users
        generic_messages = {
            'not found': 'Resource not found',
            'invalid': 'Invalid request',
            'forbidden': 'Access denied',
            'unauthorized': 'Authentication required',
            'exists': 'Resource conflict',
        }

        message_lower = message.lower()
        for key, generic in generic_messages.items():
            if key in message_lower:
                return generic

        return 'An error occurred'

    # Remove sensitive info even for authenticated users
    # Remove IDs, tokens, paths
    message = re.sub(r'\b[a-f0-9]{32,}\b', '[ID]', message)  # IDs/hashes
    message = re.sub(r'/[a-zA-Z0-9/_-]+', '[PATH]', message)  # Paths
    message = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '[IP]', message)  # IPs

    return message
