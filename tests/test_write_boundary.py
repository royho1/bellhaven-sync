"""The scheduled path must not be able to write, as a property of the imports.

`apply.py` (added in the write phase) is the only module allowed to issue POST
or PATCH. Anything reachable from `cli sync` must never import it, so this test
walks the transitive import graph rather than trusting convention.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

PACKAGE = "bellhaven_sync"
APPLY_MODULE = f"{PACKAGE}.apply"

# Modules that run inside the read-only sync path. Entries that do not exist
# yet are skipped, so this test keeps working as later phases land.
READ_ONLY_MODULES = (
    f"{PACKAGE}.crm_client",
    f"{PACKAGE}.schema_probe",
    f"{PACKAGE}.pipeline",
    f"{PACKAGE}.scraper",
    f"{PACKAGE}.matching",
    f"{PACKAGE}.proposals",
    f"{PACKAGE}.chow",
    f"{PACKAGE}.store",
    f"{PACKAGE}.review_app",
    f"{PACKAGE}.cli",
    f"{PACKAGE}.rules",
)


def module_source(module_name: str) -> Path | None:
    parts = module_name.split(".")
    path = Path(__file__).resolve().parent.parent.joinpath(*parts).with_suffix(".py")
    return path if path.exists() else None


def internal_imports(module_name: str) -> set[str]:
    path = module_source(module_name)
    if path is None:
        return set()

    tree = ast.parse(path.read_text(encoding="utf-8"))
    package_parts = module_name.split(".")[:-1]
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PACKAGE):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - node.level + 1]
                prefix = ".".join(base + ([node.module] if node.module else []))
            elif node.module and node.module.startswith(PACKAGE):
                prefix = node.module
            else:
                continue
            found.add(prefix)
            for alias in node.names:
                candidate = f"{prefix}.{alias.name}"
                if module_source(candidate) is not None:
                    found.add(candidate)

    return {name for name in found if name.startswith(PACKAGE)}


def transitive_imports(module_name: str) -> set[str]:
    seen: set[str] = set()
    queue = [module_name]
    while queue:
        current = queue.pop()
        for dependency in internal_imports(current):
            if dependency not in seen:
                seen.add(dependency)
                queue.append(dependency)
    return seen


def test_read_only_modules_never_reach_the_apply_module():
    for module_name in READ_ONLY_MODULES:
        if module_source(module_name) is None:
            continue
        reachable = transitive_imports(module_name)
        assert APPLY_MODULE not in reachable, f"{module_name} can reach {APPLY_MODULE}"


def test_crm_client_defines_no_write_capability():
    path = module_source(f"{PACKAGE}.crm_client")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    called_methods = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert not ({"post", "patch", "put", "delete"} & called_methods)


def test_the_package_currently_has_no_apply_module_on_this_branch():
    # Phase 0 is strictly read-only; the writer arrives in feat/apply-and-schedule.
    assert importlib.util.find_spec(APPLY_MODULE) is None
