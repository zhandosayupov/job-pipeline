"""Find the fields of an application form in page HTML (stdlib only, so it is unit-tested on fixtures).

Each field gets a key the page can fill again: `#id` when the element has an id, else
`[name="..."]`. Labels come from <label for>, an enclosing <label>, aria-label, placeholder or name.
Radio buttons with one name are one field whose options are their labels.
"""

from __future__ import annotations

from html.parser import HTMLParser

from ..services.forms import FormField

_SKIP_TYPES = {"hidden", "submit", "button", "reset", "image", "search"}


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.labels_for: dict[str, str] = {}
        self.elements: list[dict] = []
        self._label: dict | None = None
        self._select: dict | None = None
        self._option: dict | None = None
        self._textarea: dict | None = None
        self._radio_wrap: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "label":
            self._label = {"for": a.get("for"), "text": [], "inputs": []}
        elif tag in ("input", "select", "textarea"):
            typ = (
                a.get("type") or ("select" if tag == "select" else "textarea" if tag == "textarea" else "text")
            ).lower()
            if typ in _SKIP_TYPES:
                return
            el = {
                "tag": tag,
                "type": typ,
                "id": a.get("id"),
                "name": a.get("name"),
                "aria": a.get("aria-label"),
                "placeholder": a.get("placeholder"),
                "required": "required" in a or a.get("aria-required") == "true",
                "value": a.get("value"),
                "options": [],
                "label": None,
            }
            self.elements.append(el)
            if self._label is not None:
                self._label["inputs"].append(el)
            if tag == "select":
                self._select = el
            if tag == "textarea":
                self._textarea = el
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"), "text": []}

    def handle_endtag(self, tag):
        if tag == "label" and self._label is not None:
            text = " ".join("".join(self._label["text"]).split())
            if self._label["for"]:
                self.labels_for[self._label["for"]] = text
            for el in self._label["inputs"]:
                el["label"] = el["label"] or text
            self._label = None
        elif tag == "option" and self._option is not None and self._select is not None:
            text = " ".join("".join(self._option["text"]).split())
            if text and (self._option["value"] or "") != "":
                self._select["options"].append(text)
            self._option = None
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._option is not None:
            self._option["text"].append(data)
        elif self._label is not None:
            self._label["text"].append(data)


def scan_form(html: str) -> list[FormField]:
    s = _Scanner()
    s.feed(html)
    fields: list[FormField] = []
    radios: dict[str, FormField] = {}
    for el in s.elements:
        label = (
            s.labels_for.get(el["id"] or "") or el["label"] or el["aria"] or el["placeholder"] or el["name"] or ""
        ).strip()
        required = el["required"] or label.endswith("*")
        label = label.rstrip(" *").strip()
        if el["type"] == "radio":
            name = el["name"] or el["id"] or label
            group = radios.get(name)
            if group is None:
                group = FormField(key=f'[name="{name}"]', label=name, kind="radio", required=required)
                radios[name] = group
                fields.append(group)
            group.options.append(label)
            group.required = group.required or required
            continue
        key = f"#{el['id']}" if el["id"] else f'[name="{el["name"]}"]' if el["name"] else None
        if key is None or not label:
            continue
        kind = {
            "select": "select",
            "textarea": "textarea",
            "file": "file",
            "checkbox": "checkbox",
            "email": "email",
            "tel": "tel",
            "url": "url",
        }.get(el["type"], "text")
        fields.append(FormField(key=key, label=label, kind=kind, required=required, options=el["options"]))
    # A radio group's label is usually a <legend>/<div> above it; the worker may refine it, the name
    # still resolves through the topic patterns (e.g. "visa_sponsorship").
    return fields
