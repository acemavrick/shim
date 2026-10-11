"""POST reads require an exact, trusted operator declaration, never a model guess."""
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReadPostRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    body: dict[str, object] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def absolute_url(cls, value):
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError("POST rule requires an absolute HTTP URL without credentials or fragment")
        return value


def post_is_approved(url, body, rules):
    # Compare types too: true must not equal integer 1 in an authorization rule.
    import json
    if not isinstance(body, dict):
        return False
    normalized = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return any(url == r.url and normalized == json.dumps(r.body, sort_keys=True, separators=(",", ":"))
               for r in rules)


# ------------------------------------------------------- exploration policy
#
# During exploration the browser is a visitor. GETs always go through. A POST
# goes through unless it looks like money, deletion or a credential: a path
# segment naming checkout/orders/payment/delete, a body key naming a password
# or card, or a GraphQL mutation doing one of those. Carts, logins, newsletter
# boxes and the like are reversible and are let through; the model is told
# not to use them. Anything blocked is recorded as evidence the model can see.
# This decides only what the explorer may *send*; whether a POST ships as a
# read tool is decided later by observed-request evidence and the check gate.

import re

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
WRITE_WORDS = r"(checkout|orders?|placeorder|pay|payments?|purchase|buy|delete|destroy|remove)"
WRITE_PATH = re.compile(r"(^|[/_.-])" + WRITE_WORDS + r"([/_.-]|$)", re.I)
WRITE_KEY = re.compile(r"password|passwd|cvv|cvc|card_?number|cardnumber|iban", re.I)
MUTATION = re.compile(r"^\s*mutation\b.*" + WRITE_WORDS, re.I | re.S)


def looks_like_write(url, body) -> bool:
    if WRITE_PATH.search(urlsplit(url).path):
        return True
    if not isinstance(body, dict):
        return False
    for key, value in body.items():
        if WRITE_KEY.search(str(key)):
            return True
        if isinstance(value, str) and MUTATION.search(value):
            return True
    return False


def exploration_request_allowed(method, url, body, *, allow_writes, rules) -> bool:
    method = method.upper()
    if method in SAFE_METHODS or allow_writes:
        return True
    if method != "POST":
        return False
    if post_is_approved(url, body, rules):
        return True
    return not looks_like_write(url, body)
