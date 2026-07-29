"""Reading declared dependencies out of a project's manifest files.

Knowing the framework and libraries before reading a single line of code is
cheap context: it is plain file parsing, no AST involved. Every parser is
best-effort — a malformed manifest yields no dependencies rather than an error,
because a scan must never fail over one unparseable file.
"""

import json
import re
import tomllib
from dataclasses import dataclass
from xml.etree import ElementTree

ANY_VERSION = "*"


@dataclass(frozen=True)
class Dependency:
    name: str
    version: str
    ecosystem: str
    manifest: str  # path of the manifest, relative to the scan root


def _parse_package_json(text: str) -> list[tuple[str, str]]:
    data = json.loads(text)
    found = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name, version in (data.get(section) or {}).items():
            found.append((name, str(version)))
    return found


def _parse_requirements_txt(text: str) -> list[tuple[str, str]]:
    found = []
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        # "-r other.txt" and "-e ." are directives, not dependencies.
        if not entry or entry.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9._-]+)\s*(\[[^\]]*\])?\s*(.*)$", entry)
        if match:
            found.append((match.group(1), match.group(3).strip() or ANY_VERSION))
    return found


def _requirement_name_and_version(requirement: str) -> tuple[str, str]:
    match = re.match(r"^([A-Za-z0-9._-]+)\s*(\[[^\]]*\])?\s*(.*)$", requirement.strip())
    if not match:
        return requirement.strip(), ANY_VERSION
    return match.group(1), match.group(3).strip() or ANY_VERSION


def _parse_pyproject_toml(text: str) -> list[tuple[str, str]]:
    data = tomllib.loads(text)
    found = []

    for requirement in data.get("project", {}).get("dependencies", []) or []:
        found.append(_requirement_name_and_version(requirement))

    # Poetry keeps its own table and spells versions differently.
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue
        version = spec if isinstance(spec, str) else spec.get("version", ANY_VERSION)
        found.append((name, str(version)))

    return found


def _parse_cargo_toml(text: str) -> list[tuple[str, str]]:
    data = tomllib.loads(text)
    found = []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, spec in (data.get(section) or {}).items():
            version = spec if isinstance(spec, str) else spec.get("version", ANY_VERSION)
            found.append((name, str(version)))
    return found


def _parse_go_mod(text: str) -> list[tuple[str, str]]:
    found = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue

        entry = line[len("require ") :].strip() if line.startswith("require ") else line
        if not (in_block or line.startswith("require ")):
            continue

        parts = entry.split()
        if len(parts) >= 2:
            found.append((parts[0], parts[1]))
    return found


def _parse_csproj(text: str) -> list[tuple[str, str]]:
    root = ElementTree.fromstring(text)
    found = []
    for node in root.iter():
        if not node.tag.endswith("PackageReference"):
            continue
        name = node.get("Include") or node.get("Update")
        if name:
            found.append((name, node.get("Version") or ANY_VERSION))
    return found


def _strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _parse_pom_xml(text: str) -> list[tuple[str, str]]:
    root = ElementTree.fromstring(text)
    found = []
    for node in root.iter():
        if _strip_namespace(node.tag) != "dependency":
            continue
        fields = {_strip_namespace(child.tag): (child.text or "").strip() for child in node}
        artifact = fields.get("artifactId")
        if artifact:
            group = fields.get("groupId", "")
            name = f"{group}:{artifact}" if group else artifact
            found.append((name, fields.get("version") or ANY_VERSION))
    return found


def _parse_composer_json(text: str) -> list[tuple[str, str]]:
    data = json.loads(text)
    found = []
    for section in ("require", "require-dev"):
        for name, version in (data.get(section) or {}).items():
            found.append((name, str(version)))
    return found


# Exact file names, matched case-insensitively.
PARSERS_BY_NAME = {
    "package.json": ("node", _parse_package_json),
    "requirements.txt": ("python", _parse_requirements_txt),
    "pyproject.toml": ("python", _parse_pyproject_toml),
    "cargo.toml": ("rust", _parse_cargo_toml),
    "go.mod": ("go", _parse_go_mod),
    "pom.xml": ("java", _parse_pom_xml),
    "composer.json": ("php", _parse_composer_json),
}

# Extensions, for manifests whose name varies with the project.
PARSERS_BY_SUFFIX = {
    ".csproj": ("dotnet", _parse_csproj),
}


def is_manifest(name: str) -> bool:
    lowered = name.lower()
    if lowered in PARSERS_BY_NAME:
        return True
    return any(lowered.endswith(suffix) for suffix in PARSERS_BY_SUFFIX)


def parse_manifest(name: str, text: str, rel_path: str) -> list[Dependency]:
    """Extract dependencies from one manifest, or nothing if it cannot be read."""
    lowered = name.lower()

    entry = PARSERS_BY_NAME.get(lowered)
    if entry is None:
        for suffix, candidate in PARSERS_BY_SUFFIX.items():
            if lowered.endswith(suffix):
                entry = candidate
                break

    if entry is None:
        return []

    ecosystem, parser = entry

    try:
        pairs = parser(text)
    except Exception:
        # Any malformed manifest: no dependencies, but the scan carries on.
        return []

    return [
        Dependency(name=dep_name, version=version, ecosystem=ecosystem, manifest=rel_path)
        for dep_name, version in pairs
    ]
