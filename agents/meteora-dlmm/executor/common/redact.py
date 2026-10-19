#!/usr/bin/env python3
"""Safe rendering of RPC endpoints for logs and health checks.

An RPC URL routinely carries its credential in the query string, e.g.

    https://mainnet.helius-rpc.com/?api-key=0123456789abcdef

Truncating such a string (``url[:60] + "..."``) is NOT redaction: the first
characters of the key survive, and a 32-char key can be largely recovered from
a 60-char prefix. This module removes the whole query and fragment, plus any
userinfo, and keeps only scheme + host + port + path — enough to identify the
provider, nothing that authenticates.
"""

from urllib.parse import urlsplit

# Shown in place of any stripped credential-bearing component.
MASK = "***"


def redact_url(url) -> str:
    """Return a log-safe form of ``url``.

    Scheme, host, port and path are preserved so the endpoint stays
    identifiable. Query string, fragment and userinfo are dropped entirely
    rather than partially masked: partial masking of a secret is disclosure.

    Non-URL or unparseable input returns a constant placeholder, never the
    original text, because the input may itself be the secret.
    """
    if not isinstance(url, str):
        return "<none>"
    url = url.strip()
    if not url:
        return "<none>"

    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable>"

    if not parts.scheme or not parts.netloc:
        # Not a URL (could be a bare key or a placeholder): do not echo it.
        return "<not-a-url>"

    host = parts.hostname or ""
    if not host:
        return "<not-a-url>"

    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"

    redacted = f"{parts.scheme}://{netloc}{parts.path}"
    if parts.query:
        redacted += f"?{MASK}"
    if parts.fragment:
        redacted += f"#{MASK}"
    # A stripped userinfo is itself a credential-bearing removal.
    if parts.username or parts.password:
        redacted += " (credentials stripped)"
    return redacted


def redact_urls_in(text) -> str:
    """Redact every http(s) URL found in a free-form string.

    Used for exception text and subprocess output that may embed an endpoint.
    """
    if not isinstance(text, str):
        return ""
    out = []
    i = 0
    lowered = text.lower()
    while True:
        start = min(
            (p for p in (lowered.find("http://", i), lowered.find("https://", i))
             if p != -1),
            default=-1,
        )
        if start == -1:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = start
        while end < len(text) and not text[end].isspace() and text[end] not in "\"')],;":
            end += 1
        out.append(redact_url(text[start:end]))
        i = end
    return "".join(out)
