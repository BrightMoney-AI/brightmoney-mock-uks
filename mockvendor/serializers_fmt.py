"""Format serializer plugins (design §3.4).

A serializer renders the canonical (format-neutral) body into the target
format's bytes. Adding a format = write one class + add one line to
``settings.MOCKVENDOR_SERIALIZERS`` — no change to matcher, delay engine,
models, or migrations.

Resolution order (design §3.4): the scenario's explicit format, then the
request's Accept header, then the endpoint default.
"""
from __future__ import annotations

import importlib
import json
from typing import Protocol, runtime_checkable
from xml.sax.saxutils import escape

from django.conf import settings


@runtime_checkable
class FormatSerializer(Protocol):
    name: str
    content_type: str

    def serialize(self, canonical: dict, options: dict) -> bytes: ...


class JsonSerializer:
    name = "json"
    content_type = "application/json"

    def serialize(self, canonical: dict, options: dict) -> bytes:
        return json.dumps(canonical if canonical is not None else {}).encode("utf-8")


class XmlSerializer:
    """Minimal, dependency-free dict -> XML renderer.

    Not a general-purpose XML library — enough to render the canonical maps the
    mock stores. For byte-exact / malformed XML, use ``raw_override`` instead.
    """

    name = "xml"
    content_type = "application/xml"

    def serialize(self, canonical: dict, options: dict) -> bytes:
        root = (options or {}).get("xml_root", "response")
        body = self._to_xml(canonical if canonical is not None else {})
        doc = f'<?xml version="1.0" encoding="UTF-8"?><{root}>{body}</{root}>'
        return doc.encode("utf-8")

    def _to_xml(self, value) -> str:
        if isinstance(value, dict):
            return "".join(
                f"<{k}>{self._to_xml(v)}</{k}>" for k, v in value.items()
            )
        if isinstance(value, (list, tuple)):
            return "".join(f"<item>{self._to_xml(v)}</item>" for v in value)
        if isinstance(value, bool):
            return "true" if value else "false"
        return escape(str(value))


_CACHE: dict[str, FormatSerializer] = {}


def _load(path: str) -> FormatSerializer:
    module_path, _, cls_name = path.rpartition(".")
    cls = getattr(importlib.import_module(module_path), cls_name)
    return cls()


def registry() -> dict[str, FormatSerializer]:
    """name -> serializer instance, built from settings.MOCKVENDOR_SERIALIZERS."""
    if not _CACHE:
        for path in getattr(settings, "MOCKVENDOR_SERIALIZERS", []):
            inst = _load(path)
            _CACHE[inst.name] = inst
    return _CACHE


def get_serializer(name: str) -> FormatSerializer:
    reg = registry()
    if name not in reg:
        raise KeyError(f"No serializer registered for format {name!r}. Known: {sorted(reg)}")
    return reg[name]
