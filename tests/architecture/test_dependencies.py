"""Executable dependency rules for Workfold's modular monolith."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "workfold"

ALLOWED_DEPENDENCIES: dict[str, frozenset[str]] = {
    "domain": frozenset({"domain"}),
    "folding": frozenset({"domain", "folding"}),
    "configuration": frozenset({"configuration", "domain", "folding"}),
    "collection": frozenset({"collection", "domain"}),
    "application": frozenset({"application", "collection", "configuration", "domain", "folding"}),
    "reporting": frozenset({"application", "configuration", "domain", "folding", "reporting"}),
    "cli": frozenset({"application", "cli", "collection", "configuration", "domain", "folding", "reporting"}),
}


def test_internal_dependencies_point_toward_stable_capabilities() -> None:
    violations: list[str] = []
    for source_layer, allowed in ALLOWED_DEPENDENCIES.items():
        for path in sorted((PACKAGE_ROOT / source_layer).rglob("*.py")):
            for dependency, line in _workfold_dependencies(path):
                if dependency not in allowed:
                    relative = path.relative_to(PACKAGE_ROOT.parent)
                    violations.append(f"{relative}:{line}: {source_layer} -> {dependency}")

    assert not violations, "forbidden Workfold package dependencies:\n" + "\n".join(violations)


def test_package_root_contains_only_entrypoint_modules() -> None:
    root_modules = {path.name for path in PACKAGE_ROOT.glob("*.py")}
    assert root_modules == {"__init__.py", "__main__.py"}

    root_packages = {
        path.name
        for path in PACKAGE_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith("__") and any(path.rglob("*.py"))
    }
    assert root_packages == set(ALLOWED_DEPENDENCIES)


def test_core_uses_only_the_standard_library_and_workfold() -> None:
    violations: list[str] = []
    for package in ("domain", "folding"):
        for path in sorted((PACKAGE_ROOT / package).rglob("*.py")):
            for dependency, line in _imports(path):
                root = dependency.split(".", maxsplit=1)[0]
                if root != "workfold" and root not in sys.stdlib_module_names:
                    relative = path.relative_to(PACKAGE_ROOT.parent)
                    violations.append(f"{relative}:{line}: imports {root}")

    assert not violations, "core packages must remain standard-library-only:\n" + "\n".join(violations)


def test_argparse_is_owned_by_the_cli() -> None:
    violations: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if path.relative_to(PACKAGE_ROOT).parts[0] == "cli":
            continue
        for dependency, line in _imports(path):
            if dependency == "argparse" or dependency.startswith("argparse."):
                relative = path.relative_to(PACKAGE_ROOT.parent)
                violations.append(f"{relative}:{line}")

    assert not violations, "argparse imports outside workfold.cli:\n" + "\n".join(violations)


def test_collection_adapters_do_not_import_each_other() -> None:
    violations: list[str] = []
    for source, forbidden in (("git", "filesystem"), ("filesystem", "git")):
        for path in sorted((PACKAGE_ROOT / "collection" / source).rglob("*.py")):
            for dependency, line in _imports(path):
                if dependency == f"workfold.collection.{forbidden}" or dependency.startswith(
                    f"workfold.collection.{forbidden}."
                ):
                    relative = path.relative_to(PACKAGE_ROOT.parent)
                    violations.append(f"{relative}:{line}: {source} -> {forbidden}")

    assert not violations, "collection adapters import each other:\n" + "\n".join(violations)


def test_application_uses_the_git_source_boundary() -> None:
    violations: list[str] = []
    allowed_prefix = "workfold.collection.git.evidence"
    for path in sorted((PACKAGE_ROOT / "application").rglob("*.py")):
        for dependency, line in _imports(path):
            if dependency.startswith("workfold.collection.git.") and not dependency.startswith(allowed_prefix):
                relative = path.relative_to(PACKAGE_ROOT.parent)
                violations.append(f"{relative}:{line}: imports {dependency}")

    assert not violations, "application bypasses the Git source boundary:\n" + "\n".join(violations)


def test_reporting_consumes_only_the_stable_report_contract() -> None:
    forbidden = (
        "workfold.application.collection",
        "workfold.application.execution",
        "workfold.application.report_context",
        "workfold.collection",
    )
    violations: list[str] = []
    for path in sorted((PACKAGE_ROOT / "reporting").rglob("*.py")):
        for dependency, line in _imports(path):
            if any(dependency == prefix or dependency.startswith(prefix + ".") for prefix in forbidden):
                relative = path.relative_to(PACKAGE_ROOT.parent)
                violations.append(f"{relative}:{line}: imports {dependency}")

    assert not violations, "reporting bypasses the report contract:\n" + "\n".join(violations)


def test_filesystem_root_scan_has_explicit_boundary_bundles() -> None:
    path = PACKAGE_ROOT / "collection" / "filesystem" / "root.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "collect_root"
    )

    assert [item.arg for item in function.args.args] == ["root_snapshot"]
    assert [item.arg for item in function.args.kwonlyargs] == ["request", "sinks", "services"]


def test_workfold_module_graph_is_acyclic() -> None:
    graph = _module_graph()
    cycle = _find_cycle(graph)
    assert cycle is None, "Workfold module dependency cycle: " + " -> ".join(cycle or ())


def _workfold_dependencies(path: Path) -> tuple[tuple[str, int], ...]:
    dependencies: list[tuple[str, int]] = []
    for name, line in _imports(path):
        parts = name.split(".")
        if len(parts) >= 2 and parts[0] == "workfold":
            dependencies.append((parts[1], line))
    return tuple(dependencies)


def _imports(path: Path) -> tuple[tuple[str, int], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = _absolute_import_name(path, node)
            if name is not None:
                imports.append((name, node.lineno))
    return tuple(imports)


def _absolute_import_name(path: Path, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    source_package = ("workfold", *path.parent.relative_to(PACKAGE_ROOT).parts)
    ancestor_length = len(source_package) - node.level + 1
    if ancestor_length < 1:
        return node.module
    parts = (*source_package[:ancestor_length], *((node.module or "").split(".")))
    return ".".join(part for part in parts if part)


def _module_graph() -> dict[str, set[str]]:
    modules = {_module_name(path): path for path in PACKAGE_ROOT.rglob("*.py")}
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        for dependency, _line in _imports(path):
            candidate = dependency
            while candidate.startswith("workfold"):
                if candidate in modules:
                    if candidate != name:
                        graph[name].add(candidate)
                    break
                if "." not in candidate:
                    break
                candidate = candidate.rsplit(".", maxsplit=1)[0]
    return graph


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = relative.parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _find_cycle(graph: dict[str, set[str]]) -> tuple[str, ...] | None:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        if node in visiting:
            start = stack.index(node)
            return (*stack[start:], node)
        if node in visited:
            return None
        visiting.add(node)
        stack.append(node)
        for dependency in sorted(graph[node]):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in sorted(graph):
        cycle = visit(node)
        if cycle is not None:
            return cycle
    return None
