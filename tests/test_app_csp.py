"""Content-Security-Policy of the app shell (app/nginx.conf).

Offline: reads the nginx template. The policy must let the inline preview work
(PDF frame, audio, images all from the S3 host only) without loosening anything
that matters: scripts stay same-origin, no 'unsafe-eval', the app cannot be
framed, and nothing is allowed from arbitrary hosts.
"""
import re
from pathlib import Path

NGINX = Path(__file__).resolve().parents[1] / "app" / "nginx.conf"


def _csp() -> dict:
    text = NGINX.read_text(encoding="utf-8")
    match = re.search(r'add_header Content-Security-Policy "([^"]+)"', text)
    assert match, "no Content-Security-Policy header in app/nginx.conf"
    policy = {}
    for part in match.group(1).split(";"):
        name, *values = part.split()
        policy[name] = values
    return policy


def test_preview_sources_are_limited_to_the_s3_host():
    csp = _csp()
    s3 = "https://${S3_DOMAIN}"
    assert s3 in csp["img-src"]          # images
    assert s3 in csp["media-src"]        # audio
    assert csp["frame-src"] == [s3]      # PDF viewer frame, nothing else
    assert s3 in csp["connect-src"]      # direct presigned uploads


def test_the_policy_stays_strict_where_it_matters():
    csp = _csp()
    assert csp["default-src"] == ["'self'"]
    assert csp["script-src"] == ["'self'"]
    assert "'unsafe-eval'" not in sum(csp.values(), [])
    assert csp["frame-ancestors"] == ["'none'"]
    assert csp["object-src"] == ["'none'"]
    # No wildcard or bare scheme source anywhere.
    for name, values in csp.items():
        assert "*" not in values and "https:" not in values, name
