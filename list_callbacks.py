#!/usr/bin/env python3

from __future__ import annotations

import ast
import io
import json
import pathlib
import re
import tarfile

CALLBACK_PATTERN = re.compile(
    r"^[^/]+/(?P<namespace>[^-]+)-(?P<collection>[^-]+)-(?P<version>[^/]+)/plugins/callback/(?P<name>[^/]+)\.py$"
)

ATTRS = ("CALLBACK_NAME", "CALLBACK_TYPE", "CALLBACK_VERSION")


def extract_callback_attrs(source: str) -> dict[str, str]:
    attrs: dict[str, str] = {a: "unknown" for a in ATTRS}

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return attrs

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        for item in ast.iter_child_nodes(node):
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id in ATTRS and isinstance(item.value, ast.Constant):
                        attrs[target.id] = str(item.value.value)

    return attrs


def main() -> None:
    archive = pathlib.Path(__file__).parent / "collections.tar.gz"

    callbacks: dict[str, dict[str, str]] = {}
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            m = CALLBACK_PATTERN.match(member.name)
            if not m:
                continue

            name = m.group("name")
            if name == "__init__":
                continue

            f = tf.extractfile(member)
            if f is None:
                continue

            source = io.TextIOWrapper(f, encoding="utf-8", errors="replace").read()
            attrs = extract_callback_attrs(source)

            fqcn = f"{m.group('namespace')}.{m.group('collection')}"
            key = f"{fqcn}.{name}"
            callbacks[key] = {
                "collection_version": m.group("version"),
                "name": attrs["CALLBACK_NAME"],
                "type": attrs["CALLBACK_TYPE"],
                "version": attrs["CALLBACK_VERSION"],
            }

    print(json.dumps(callbacks, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
