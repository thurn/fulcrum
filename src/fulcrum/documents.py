"""Read-only discovery for plans, memory, and human-maintained NEWS."""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

import yaml

MAX_DOCUMENT_BYTES = 256_000
IDENTIFIER_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
NEWS_HEADING_PATTERN: re.Pattern[str] = re.compile(
    r"^## (?P<date>\d{4}-\d{2}-\d{2}) \| (?P<title>.+)$"
)
NEWS_CATEGORY = Literal["workflow", "architecture", "progress"]


class DocumentError(ValueError):
    """A Markdown document does not satisfy its declared contract."""


class DocumentDiagnostic(TypedDict):
    path: str
    error: str


class PlanDocument(TypedDict):
    path: str
    plan_id: str
    project: str
    activation: Literal["queued", "future"]
    requires_plans: list[str]
    dependency_cycle: bool
    body: str


class PlanDiscovery(TypedDict):
    plans: list[PlanDocument]
    diagnostics: list[DocumentDiagnostic]


class ProjectSummary(TypedDict):
    project: str
    updated_at: str
    current: str
    next: str


class NewsEntry(TypedDict):
    date: str
    title: str
    projects: list[str]
    category: Literal["workflow", "architecture", "progress"]
    beads: list[str]
    body: str


class NewsDocument(TypedDict):
    path: str
    project_summaries: dict[str, ProjectSummary]
    entries: list[NewsEntry]


def _read_document(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise DocumentError(f"could not read document: {error}") from error
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise DocumentError(
            f"document exceeds the {MAX_DOCUMENT_BYTES}-byte reader limit"
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DocumentError(f"document is not valid UTF-8: {error}") from error


def _split_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = _read_document(path)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise DocumentError("missing opening YAML frontmatter delimiter")
    closing: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing = index
            break
    if closing is None:
        raise DocumentError("missing closing YAML frontmatter delimiter")
    source = "".join(lines[1:closing])
    try:
        loaded: object = yaml.safe_load(source)
    except yaml.YAMLError as error:
        raise DocumentError(f"invalid YAML frontmatter: {error}") from error
    if loaded is None:
        metadata: dict[str, Any] = {}
    elif isinstance(loaded, dict) and all(isinstance(key, str) for key in loaded):
        metadata = cast(dict[str, Any], loaded)
    else:
        raise DocumentError("frontmatter must be a mapping with string keys")
    return metadata, "".join(lines[closing + 1 :])


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        raise DocumentError(f"{field} must use lowercase letters, digits, and hyphens")
    return value


def parse_plan(path: Path, known_projects: set[str]) -> PlanDocument:
    """Parse one plan and verify its metadata agrees with its default path."""

    metadata, body = _split_frontmatter(path)
    plan_id = _identifier(metadata.get("plan_id"), "plan_id")
    project = _identifier(metadata.get("project"), "project")
    activation = metadata.get("activation")
    if activation not in {"queued", "future"}:
        raise DocumentError("activation must be 'queued' or 'future'")
    if project not in known_projects:
        raise DocumentError(f"unknown project {project!r}")
    if path.parent.name != project or path.stem != plan_id:
        raise DocumentError(
            "plan path must be plans/<project>/<plan_id>.md and match frontmatter"
        )
    raw_requirements = metadata.get("requires_plans", [])
    if not isinstance(raw_requirements, list):
        raise DocumentError("requires_plans must be a list")
    requirements = [
        _identifier(requirement, f"requires_plans[{index}]")
        for index, requirement in enumerate(raw_requirements)
    ]
    if len(requirements) != len(set(requirements)):
        raise DocumentError("requires_plans must not contain duplicates")
    return {
        "path": str(path),
        "plan_id": plan_id,
        "project": project,
        "activation": activation,
        "requires_plans": requirements,
        "dependency_cycle": False,
        "body": body,
    }


def _cycle_members(plans: list[PlanDocument]) -> set[str]:
    known_ids = {plan["plan_id"] for plan in plans}
    graph: dict[str, list[str]] = {
        plan["plan_id"]: [
            requirement
            for requirement in plan["requires_plans"]
            if requirement in known_ids
        ]
        for plan in plans
    }
    state: dict[str, Literal["visiting", "done"]] = {}
    stack: list[str] = []
    members: set[str] = set()

    def visit(plan_id: str) -> None:
        marker = state.get(plan_id)
        if marker == "done":
            return
        if marker == "visiting":
            start = stack.index(plan_id)
            members.update(stack[start:])
            return
        state[plan_id] = "visiting"
        stack.append(plan_id)
        for requirement in graph[plan_id]:
            visit(requirement)
        stack.pop()
        state[plan_id] = "done"

    for plan_id in graph:
        visit(plan_id)
    return members


def discover_plans(brain_root: Path, known_projects: set[str]) -> PlanDiscovery:
    """Discover plan metadata without dispatching work or modifying documents."""

    root = brain_root.resolve(strict=False)
    plans: list[PlanDocument] = []
    diagnostics: list[DocumentDiagnostic] = []
    for path in sorted((root / "plans").glob("*/*.md")):
        try:
            plans.append(parse_plan(path, known_projects))
        except Exception as error:
            diagnostics.append({"path": str(path), "error": str(error)})

    by_id: dict[str, list[PlanDocument]] = {}
    for plan in plans:
        by_id.setdefault(plan["plan_id"], []).append(plan)
    duplicate_ids = {plan_id for plan_id, matches in by_id.items() if len(matches) > 1}
    for plan_id in sorted(duplicate_ids):
        for plan in by_id[plan_id]:
            diagnostics.append(
                {"path": plan["path"], "error": f"duplicate plan_id {plan_id!r}"}
            )
    plans = [plan for plan in plans if plan["plan_id"] not in duplicate_ids]

    cycles = _cycle_members(plans)
    for plan in plans:
        if plan["plan_id"] in cycles:
            plan["dependency_cycle"] = True
            diagnostics.append(
                {
                    "path": plan["path"],
                    "error": "plan dependency cycle includes " + plan["plan_id"],
                }
            )
    return {"plans": plans, "diagnostics": diagnostics}


def _timestamp(value: object, field: str) -> str:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None or value.utcoffset() != datetime.timedelta(0):
            raise DocumentError(f"{field} must be a UTC timestamp")
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.endswith("Z"):
        try:
            datetime.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        except ValueError as error:
            raise DocumentError(f"{field} is not a valid timestamp") from error
        return value
    raise DocumentError(f"{field} must be a UTC timestamp ending in Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocumentError(f"{field} must be a non-empty string")
    return value


def _csv_field(line: str, field: str) -> list[str]:
    prefix = f"{field}:"
    if not line.startswith(prefix):
        raise DocumentError(f"NEWS entry must declare {field}")
    values = [value.strip() for value in line.removeprefix(prefix).split(",")]
    if not values or any(not value for value in values):
        raise DocumentError(f"NEWS {field} must contain comma-separated values")
    return values


def parse_news(path: Path, known_projects: set[str]) -> NewsDocument:
    """Parse current project summaries and dated NEWS entries."""

    metadata, body = _split_frontmatter(path)
    raw_summaries = metadata.get("project_summaries")
    if not isinstance(raw_summaries, dict):
        raise DocumentError("project_summaries must be a mapping")
    summaries: dict[str, ProjectSummary] = {}
    for raw_project, raw_summary in raw_summaries.items():
        project = _identifier(raw_project, "project_summaries key")
        if project not in known_projects:
            raise DocumentError(f"unknown project {project!r} in project_summaries")
        if not isinstance(raw_summary, dict):
            raise DocumentError(f"project_summaries.{project} must be a mapping")
        summary = cast(dict[str, Any], raw_summary)
        summaries[project] = {
            "project": project,
            "updated_at": _timestamp(
                summary.get("updated_at"), f"project_summaries.{project}.updated_at"
            ),
            "current": _text(
                summary.get("current"), f"project_summaries.{project}.current"
            ),
            "next": _text(summary.get("next"), f"project_summaries.{project}.next"),
        }

    lines = body.splitlines()
    entries: list[NewsEntry] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        heading = NEWS_HEADING_PATTERN.fullmatch(lines[index])
        if heading is None:
            raise DocumentError(
                f"NEWS line {index + 1} must be a dated level-two heading"
            )
        try:
            datetime.date.fromisoformat(heading.group("date"))
        except ValueError as error:
            raise DocumentError(f"NEWS line {index + 1} has an invalid date") from error
        if index + 3 >= len(lines):
            raise DocumentError("NEWS entry is missing metadata fields")
        projects = _csv_field(lines[index + 1], "Projects")
        for project in projects:
            _identifier(project, "NEWS project")
            if project not in known_projects:
                raise DocumentError(f"unknown project {project!r} in NEWS entry")
        category_values = _csv_field(lines[index + 2], "Category")
        if len(category_values) != 1 or category_values[0] not in {
            "workflow",
            "architecture",
            "progress",
        }:
            raise DocumentError(
                "NEWS Category must be workflow, architecture, or progress"
            )
        beads = _csv_field(lines[index + 3], "Beads")
        content_start = index + 4
        next_entry = content_start
        while (
            next_entry < len(lines)
            and NEWS_HEADING_PATTERN.fullmatch(lines[next_entry]) is None
        ):
            next_entry += 1
        entry_body = "\n".join(lines[content_start:next_entry]).strip()
        if not entry_body:
            raise DocumentError(f"NEWS entry {heading.group('date')} has an empty body")
        entries.append(
            {
                "date": heading.group("date"),
                "title": heading.group("title"),
                "projects": projects,
                "category": cast(
                    Literal["workflow", "architecture", "progress"],
                    category_values[0],
                ),
                "beads": beads,
                "body": entry_body,
            }
        )
        index = next_entry
    return {"path": str(path), "project_summaries": summaries, "entries": entries}
