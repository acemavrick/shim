"""The contract between the compiler and the server.

The compiler writes ``manifest.json``; the MCP server reads it and nothing else.
Both halves derive their types from this module, so they cannot drift.
Change it deliberately.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

Tier = Literal["read", "reversible", "irreversible"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------- input


class PropertySchema(Strict):
    """One property of an action's input, in JSON Schema terms.

    Emitted verbatim to MCP. The spec defines ``input`` as JSON Schema, which is
    already what MCP wants, so there is no conversion step in either direction.
    """

    type: Literal["string", "number", "integer", "boolean"]
    description: str | None = None
    # Enumerated from <select> options or radio groups, so an agent cannot guess wrong.
    enum: list[str] | None = None
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    maxLength: int | None = None
    default: Any | None = None
    # Values observed on the site (link ids, slugs). The check uses two of
    # them so a detail-page tool can be verified by varying the id.
    examples: list[str] | None = None


class InputSchema(Strict):
    type: Literal["object"] = "object"
    properties: dict[str, PropertySchema] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    additionalProperties: bool = False


# --------------------------------------------------------------- backend


class FromInput(Strict):
    """Agent-supplied. ``name`` keys into InputSchema.properties."""

    from_: Literal["input"] = Field("input", alias="from")
    name: str
    # Checkboxes only: the wire value to send when the boolean input is true.
    # Absent means send the value verbatim.
    true_value: str | None = None


class FromConst(Strict):
    """Fixed by the markup or by observation: hidden fields, preselected
    options, submit name/value, or a value an API always receives. Kept typed
    so JSON bodies send 8 and false, not "8" and "false"."""

    from_: Literal["const"] = Field("const", alias="from")
    value: str | int | float | bool


class FromGenerate(Strict):
    """Minted at call time. Client-generated identifiers (a cart line id) are
    the caller's job, never an agent's input and never a pinned constant."""

    from_: Literal["generate"] = Field("generate", alias="from")
    kind: Literal["uuid", "timestamp"] = "uuid"


class FromPage(Strict):
    """Re-scraped from the form page at call time, for rotating tokens (CSRF).

    Requires ``refetch_from``. A token captured at compile time is already stale
    by the time anyone calls the tool, so it must never be stored as a const.
    """

    from_: Literal["page"] = Field("page", alias="from")
    field: str


ParamSource = Annotated[Union[FromInput, FromConst, FromPage, FromGenerate], Field(discriminator="from_")]


class HttpBackend(Strict):
    kind: Literal["http"] = "http"
    method: Literal["GET", "POST"]
    # Absolute URL, already resolved against <base href> and the document URL.
    # May contain {param} placeholders, filled from the agent's input.
    # For GET the query string is REPLACED by `fields` -- per the HTML form
    # submission algorithm, action="/s?page=2" submitting q=x loses page=2.
    url: str
    encoding: Literal["query", "form", "json"] = "query"
    fields: dict[str, ParamSource] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    # GET this page and re-scrape hidden fields before submitting.
    refetch_from: str | None = None


class ScriptBackend(Strict):
    """Declared for spec compatibility; no runtime implementation in this MVP."""

    kind: Literal["script"] = "script"
    file: str


Backend = Annotated[Union[HttpBackend, ScriptBackend], Field(discriminator="kind")]


# --------------------------------------------------------------- extract


class Extract(Strict):
    """Turns a response into records.

    Field spec syntax:
        ".title"          text of the first match
        "a@href"          an attribute (URLs resolved against the response URL)
        ".price|number"   text coerced to a number ("$89.00" -> 89)
        ""                the item root itself
    """

    # "jsonld": the page's declared schema.org records; no selectors needed.
    kind: Literal["css", "json", "jsonld"] = "css"
    # Selector (or dot path) for a repeating item. None means one record per page.
    items: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)


# ----------------------------------------------------------------- check


class CheckResult(Strict):
    """The one test an action passed at compile time.

    Load-bearing, not decorative: form-first extraction emits a confident,
    plausible, broken template when a page intercepts submission in JavaScript.
    Firing the request is the only way to catch that. No ``passed``, no tool.
    """

    input: dict[str, Any] = Field(default_factory=dict)
    passed: bool = False
    items_found: int | None = None
    status: int | None = None
    error: str | None = None
    checked_at: str | None = None


class Session(Strict):
    """Under stdio there is one connection per process, so per-connection == per-process."""

    scope: Literal["none", "per-connection"] = "none"
    carry_cookies: bool = False


class Source(Strict):
    """Provenance, for debugging a wrong template."""

    page: str
    form_index: int
    submit: str | None = None


class Action(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    tier: Tier
    input: InputSchema
    backend: Backend
    extract: Extract
    check: CheckResult | None = None
    session: Session | None = None
    source: Source | None = None


# -------------------------------------------------------------- coverage

SkipReason = Literal[
    "bot-protection",
    "auth-required",
    "multipart-upload",
    "no-submittable-fields",
    "duplicate-form",
    "robots-disallowed",
    "fetch-error",
    "check-failed",
    "unsupported-method",
    "not-useful",
]


class Skip(Strict):
    what: str
    reason: SkipReason
    detail: str | None = None


class Coverage(Strict):
    """Why the compiler did not ship something.

    This is what turns a hostile URL from a crash into
    "0 actions compiled - 1 skipped: bot protection".
    """

    pages_crawled: int = 0
    pages_failed: int = 0
    forms_found: int = 0
    actions_generated: int = 0
    actions_shipped: int = 0
    # Declared (JSON-LD) records found across crawled pages.
    records_extracted: int = 0
    skipped: list[Skip] = Field(default_factory=list)


class Page(Strict):
    """Content surface. Optional in this MVP: the end-to-end claim is actions."""

    url: str
    title: str | None = None
    markdown: str | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)


class Step(Strict):
    """One instruction in a procedure. Exactly one of action / url / note."""

    action: str | None = None
    url: str | None = None
    note: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")


class Procedure(Strict):
    """How to accomplish an intent with the shipped tools -- an MCP prompt."""

    intent: str
    steps: list[Step]
    source: Literal["model", "author"] = "model"


class Manifest(Strict):
    site: str
    host: str
    compiled_at: str
    # What the person compiling wanted an agent to be able to do here.
    intent: str | None = None
    notes: list[str] = Field(default_factory=list)
    exploration_summary: str | None = None
    site_description: str | None = None
    procedures: list[Procedure] = Field(default_factory=list)
    pages: list[Page] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2, by_alias=True, exclude_none=True)
