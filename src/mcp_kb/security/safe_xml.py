"""Phase 6: XXE-hardened XML parsing for untrusted repository manifests.

Every XML file this project parses (`pom.xml`, `.csproj`, Liquibase
changelogs, ...) comes from an arbitrarily-cloned git repository — i.e.
untrusted input from the platform's own threat-model perspective. `lxml`'s
defaults are reasonably safe on modern versions, but explicitly disabling
DTD loading, entity resolution and network access is the standard
defense-in-depth XXE mitigation (OWASP XXE Prevention Cheat Sheet) and
removes any dependence on the installed lxml version's default behavior.
"""
from __future__ import annotations

from pathlib import Path

from lxml import etree

_SAFE_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    dtd_validation=False,
    load_dtd=False,
    huge_tree=False,  # also caps XML-bomb-style entity/attribute expansion
)


def safe_parse(path: str | Path) -> etree._ElementTree:
    """XXE-hardened equivalent of `lxml.etree.parse(path)`."""
    return etree.parse(str(path), parser=_SAFE_PARSER)


def safe_fromstring(data: bytes | str) -> etree._Element:
    """XXE-hardened equivalent of `lxml.etree.fromstring(data)`."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return etree.fromstring(data, parser=_SAFE_PARSER)
