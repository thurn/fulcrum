"""Tests for deterministic Markdown plan, memory, and NEWS readers."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fulcrum.cli import main
from fulcrum.config import RuntimePaths
from fulcrum.context import read_task_context
from fulcrum.documents import DocumentError, discover_plans, parse_news
from fulcrum.state import atomic_write_record

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
PROJECTS = {"fulcrum", "battlement"}


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / "records" / name).read_text())


def role_registry() -> dict[str, object]:
    registry = fixture("unresolved-task-identity.json")
    role = registry["roles"][0]  # type: ignore[index]
    role.update(  # type: ignore[union-attr]
        {
            "project_id": "fulcrum",
            "task_id": "task-executor-3",
            "identity_state": "resolved",
        }
    )
    role.pop("client_thread_id")  # type: ignore[union-attr]
    return registry


def project_registry() -> dict[str, object]:
    registry = fixture("project-registry.json")
    registry["projects"] = [
        {
            "project_id": project,
            "repo_path": f"/{project}",
            "host_id": "local",
            "codex_project_id": f"codex-{project}",
            "tollgate_repo_id": f"tollgate-{project}",
            "enabled": True,
        }
        for project in sorted(PROJECTS)
    ]
    return registry


class PlanReaderTest(unittest.TestCase):
    def test_valid_manual_plans_are_discovered_without_rewriting(self) -> None:
        brain = FIXTURE_ROOT / "brain"
        before = {
            path: path.read_bytes() for path in sorted((brain / "plans").glob("*/*.md"))
        }
        result = discover_plans(brain, PROJECTS)
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(
            [plan["plan_id"] for plan in result["plans"]],
            ["mobile-polish", "reader-foundations"],
        )
        mobile = next(
            plan for plan in result["plans"] if plan["plan_id"] == "mobile-polish"
        )
        self.assertEqual(mobile["activation"], "future")
        self.assertEqual(mobile["requires_plans"], ["reader-foundations"])
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_malformed_plan_has_file_specific_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plans" / "fulcrum" / "broken.md"
            path.parent.mkdir(parents=True)
            path.write_text("---\nplan_id: [\n---\n", encoding="utf-8")
            result = discover_plans(Path(temporary), {"fulcrum"})
        self.assertEqual(result["plans"], [])
        self.assertIn("broken.md", result["diagnostics"][0]["path"])
        self.assertIn("invalid YAML", result["diagnostics"][0]["error"])

    def test_unsafe_yaml_object_tag_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plans" / "fulcrum" / "unsafe.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\nplan_id: unsafe\nproject: fulcrum\nactivation: queued\n"
                "requires_plans: !!python/object/apply:builtins.list []\n---\nBody\n",
                encoding="utf-8",
            )
            result = discover_plans(Path(temporary), {"fulcrum"})
        self.assertEqual(result["plans"], [])
        self.assertIn("invalid YAML", result["diagnostics"][0]["error"])

    def test_duplicate_unknown_and_invalid_activation_are_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plans = root / "plans"
            for project, plan_id, activation in (
                ("fulcrum", "same", "queued"),
                ("battlement", "same", "future"),
                ("mystery", "unknown", "future"),
                ("fulcrum", "later", "later"),
            ):
                path = plans / project / f"{plan_id}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "---\n"
                    f"plan_id: {plan_id}\nproject: {project}\n"
                    f"activation: {activation}\nrequires_plans: []\n---\nBody\n",
                    encoding="utf-8",
                )
            result = discover_plans(root, PROJECTS)
        errors = "\n".join(item["error"] for item in result["diagnostics"])
        self.assertIn("duplicate plan_id", errors)
        self.assertIn("unknown project", errors)
        self.assertIn("activation must", errors)

    def test_dependency_cycle_marks_each_involved_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for plan_id, requirement in (("alpha", "beta"), ("beta", "alpha")):
                path = root / "plans" / "fulcrum" / f"{plan_id}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "---\n"
                    f"plan_id: {plan_id}\nproject: fulcrum\nactivation: queued\n"
                    f"requires_plans: [{requirement}]\n---\nBody\n",
                    encoding="utf-8",
                )
            result = discover_plans(root, {"fulcrum"})
        self.assertTrue(all(plan["dependency_cycle"] for plan in result["plans"]))
        self.assertEqual(len(result["diagnostics"]), 2)


class NewsReaderTest(unittest.TestCase):
    def test_multi_project_entries_preserve_scope_links_dates_and_body(self) -> None:
        news = parse_news(FIXTURE_ROOT / "brain" / "NEWS.md", PROJECTS)
        self.assertEqual(set(news["project_summaries"]), PROJECTS)
        first = news["entries"][0]
        self.assertEqual(first["projects"], ["fulcrum", "battlement"])
        self.assertEqual(first["category"], "architecture")
        self.assertEqual(first["beads"], ["brain-17", "brain-18"])
        self.assertIn("**body Markdown**", first["body"])
        self.assertEqual(first["date"], "2026-09-11")
        self.assertIn("plans/fulcrum", news["project_summaries"]["fulcrum"]["current"])

    def test_invalid_news_category_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "NEWS.md"
            shutil.copy(FIXTURE_ROOT / "brain" / "NEWS.md", path)
            path.write_text(
                path.read_text().replace("Category: architecture", "Category: feature"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DocumentError, "Category"):
                parse_news(path, PROJECTS)


class ContextAndCliTest(unittest.TestCase):
    def test_context_reads_concise_role_and_project_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brain = root / "brain"
            shutil.copytree(FIXTURE_ROOT / "brain", brain)
            paths = RuntimePaths(brain, root / "state", root / "config.json")
            atomic_write_record(paths, role_registry())
            result = read_task_context(paths, "task-executor-3")
        self.assertIn("source, tested", result["memory"]["role"])
        self.assertIn("scripts/check", result["memory"]["project"])

    def test_plans_list_uses_registered_projects_and_json_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brain = root / "brain"
            shutil.copytree(FIXTURE_ROOT / "brain", brain)
            paths = RuntimePaths(brain, root / "state", root / "config.json")
            atomic_write_record(paths, role_registry())
            atomic_write_record(paths, project_registry())
            output = io.StringIO()
            with (
                patch.dict(
                    "os.environ", {"FULCRUM_CONFIG": str(paths.config_file)}, clear=True
                ),
                redirect_stdout(output),
            ):
                code = main(
                    [
                        "--brain-root",
                        str(brain),
                        "--state-root",
                        str(paths.state_root),
                        "plans",
                        "list",
                        "--project",
                        "fulcrum",
                    ]
                )
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertEqual([plan["project"] for plan in value["plans"]], ["fulcrum"])


if __name__ == "__main__":
    unittest.main()
