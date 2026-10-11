"""Parses HTML <form> elements into descriptors.

This is the shortcut that replaces request diffing: a form already declares its
method, action and field names, so a replayable request template can be read
straight out of the markup. The subtleties below are not optional -- each one
silently produces a wrong request if skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

ControlKind = Literal["text", "number", "select", "radio", "checkbox", "hidden"]

# Button types that carry no data, plus submit (handled separately as branches).
SKIP_TYPES = {"button", "reset", "image", "submit"}
_WS = re.compile(r"\s+")


def _clean(s: str | None) -> str | None:
    if not s:
        return None
    out = _WS.sub(" ", s).strip()
    return out or None


@dataclass
class Control:
    wire: str
    kind: ControlKind
    required: bool = False
    label: str | None = None
    # Default from the markup: hidden value, `selected` option, `checked` radio.
    value: str | None = None
    options: list[str] = field(default_factory=list)
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None


@dataclass
class SubmitButton:
    text: str
    # Present only when the button carries name+value, which servers branch on.
    wire: str | None = None
    value: str | None = None


@dataclass
class FormDescriptor:
    page: str
    form_index: int
    method: Literal["GET", "POST"]
    # Absolute, resolved against <base href> then the document URL.
    action: str
    enctype: str
    controls: list[Control] = field(default_factory=list)
    submits: list[SubmitButton] = field(default_factory=list)
    heading: str | None = None
    # Stable identity across pages: a header search form must not become N actions.
    signature: str = ""


def _num(el: Tag, name: str) -> float | None:
    raw = el.get(name)
    if not isinstance(raw, str):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _label_for(soup: BeautifulSoup, el: Tag) -> str | None:
    el_id = el.get("id")
    if isinstance(el_id, str):
        by_for = soup.select_one(f'label[for="{el_id}"]')
        if by_for:
            return _clean(by_for.get_text())
    wrapping = el.find_parent("label")
    if isinstance(wrapping, Tag):
        return _clean(wrapping.get_text())
    for attr in ("aria-label", "placeholder"):
        value = el.get(attr)
        if isinstance(value, str) and value.strip():
            return _clean(value)
    return None


def _collect_controls(soup: BeautifulSoup, form: Tag, form_id: str | None) -> list[Control]:
    """Collects the controls a form would actually submit.

    Excluded: disabled, nameless, and non-data button types. Unchecked
    checkboxes and radios stay as parameters but contribute no default.
    """
    controls: dict[str, Control] = {}

    owned: list[Tag] = list(form.select("input, select, textarea"))
    if form_id:
        # Inputs may live outside the form and attach via form="<id>".
        owned += [e for e in soup.select(f'[form="{form_id}"]') if e not in owned]

    for el in owned:
        wire = el.get("name")
        if not isinstance(wire, str) or not wire or el.has_attr("disabled"):
            continue

        tag = el.name.lower()
        default_type = "select" if tag == "select" else "text"
        raw_type = el.get("type", default_type)
        ctype = raw_type.lower() if isinstance(raw_type, str) else default_type

        if ctype == "file":
            continue  # multipart; the form is skipped wholesale by the caller
        if ctype in SKIP_TYPES:
            continue

        label = _label_for(soup, el)
        required = el.has_attr("required")

        if tag == "select":
            options: list[str] = []
            selected: str | None = None
            for opt in el.select("option"):
                raw = opt.get("value")
                # An option with no value attribute submits its text content.
                value = raw if isinstance(raw, str) else (_clean(opt.get_text()) or "")
                options.append(value)
                if opt.has_attr("selected"):
                    selected = value
            controls[wire] = Control(
                wire=wire, kind="select", required=required, label=label,
                value=selected, options=[o for o in options if o],
            )
            continue

        if ctype == "radio":
            value = el.get("value") if isinstance(el.get("value"), str) else "on"
            prev = controls.get(wire)
            controls[wire] = Control(
                wire=wire, kind="radio",
                required=(prev.required if prev else False) or required,
                label=(prev.label if prev else None) or label,
                value=value if el.has_attr("checked") else (prev.value if prev else None),
                options=[*(prev.options if prev else []), value],
            )
            continue

        if ctype == "checkbox":
            value = el.get("value") if isinstance(el.get("value"), str) else "on"
            controls[wire] = Control(
                wire=wire, kind="checkbox", required=required, label=label,
                value=value if el.has_attr("checked") else None,
                options=[value],
            )
            continue

        if ctype == "hidden":
            raw = el.get("value")
            controls[wire] = Control(
                wire=wire, kind="hidden", value=raw if isinstance(raw, str) else "",
            )
            continue

        raw_value = el.get("value")
        pattern = el.get("pattern")
        controls[wire] = Control(
            wire=wire,
            kind="number" if ctype in ("number", "range") else "text",
            required=required, label=label,
            value=raw_value if isinstance(raw_value, str) and raw_value else None,
            pattern=pattern if isinstance(pattern, str) else None,
            minimum=_num(el, "min"), maximum=_num(el, "max"),
            max_length=int(ml) if (ml := _num(el, "maxlength")) else None,
        )

    return list(controls.values())


def _collect_submits(form: Tag) -> list[SubmitButton]:
    submits: list[SubmitButton] = []
    for el in form.select('button, input[type="submit"]'):
        if el.has_attr("disabled"):
            continue
        tag = el.name.lower()
        raw_type = el.get("type", "submit" if tag == "button" else "")
        ctype = raw_type.lower() if isinstance(raw_type, str) else ""
        if tag == "button" and ctype != "submit":
            continue
        raw_value = el.get("value")
        value = raw_value if isinstance(raw_value, str) else None
        text = _clean(el.get_text()) if tag == "button" else _clean(value)
        wire = el.get("name")
        submits.append(
            SubmitButton(
                text=text or "Submit",
                wire=wire if isinstance(wire, str) and wire else None,
                value=value,
            )
        )
    return submits


def _nearest_heading(soup: BeautifulSoup, form: Tag) -> str | None:
    legend = form.select_one("legend")
    if legend:
        return _clean(legend.get_text())
    prev = form.find_previous(["h1", "h2", "h3"])
    if isinstance(prev, Tag):
        return _clean(prev.get_text())
    h1 = soup.select_one("h1")
    return _clean(h1.get_text()) if h1 else None


def parse_forms(html: str, page_url: str) -> list[FormDescriptor]:
    soup = BeautifulSoup(html, "lxml")

    base_tag = soup.select_one("base[href]")
    base_href = base_tag.get("href") if base_tag else None
    base = urljoin(page_url, base_href) if isinstance(base_href, str) else page_url

    out: list[FormDescriptor] = []
    for form_index, form in enumerate(soup.select("form")):
        raw_method = form.get("method", "get")
        method_str = raw_method.lower() if isinstance(raw_method, str) else "get"
        if method_str not in ("get", "post"):
            continue  # method="dialog" and friends
        method: Literal["GET", "POST"] = "POST" if method_str == "post" else "GET"

        # An empty or absent action submits to the document's own URL.
        raw_action = form.get("action")
        try:
            action = urljoin(base, raw_action) if isinstance(raw_action, str) and raw_action else page_url
        except ValueError:
            continue

        form_id = form.get("id")
        controls = _collect_controls(soup, form, form_id if isinstance(form_id, str) else None)
        submits = _collect_submits(form)

        raw_enctype = form.get("enctype", "application/x-www-form-urlencoded")
        enctype = raw_enctype.lower() if isinstance(raw_enctype, str) else "application/x-www-form-urlencoded"

        signature = " ".join([
            method,
            urlsplit(action)._replace(fragment="").geturl(),
            ",".join(sorted(c.wire for c in controls)),
        ])

        out.append(FormDescriptor(
            page=page_url, form_index=form_index, method=method, action=action,
            enctype=enctype, controls=controls, submits=submits,
            heading=_nearest_heading(soup, form), signature=signature,
        ))

    return out
