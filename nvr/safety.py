"""Small helpers for keeping connection credentials out of diagnostic text."""
import re


def redact_urls(value):
    """Redact userinfo and sensitive query values in URLs embedded in text."""
    text = str(value)
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/]*@", r"\1***@", text)
    return re.sub(r"(?i)([?&](?:password|passwd|pwd|pass|token|access_token|key)=)[^&\s]*",
                  r"\1***", text)
