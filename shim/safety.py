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
