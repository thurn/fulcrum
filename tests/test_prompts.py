from __future__ import annotations

import unittest

from fulcrum.prompts import build_prompt, load_template


class PromptsTest(unittest.TestCase):
    def test_specialists_receive_distinct_role_guidance(self) -> None:
        self.assertIn("whole-codebase", load_template("specialist", role="inquisitor"))
        self.assertIn("postmortem", load_template("specialist", role="sage"))

    def test_prompt_combines_restored_role_guidance_and_exact_action_contract(
        self,
    ) -> None:
        prompt = build_prompt(
            action_kind="review",
            task={
                "title": "Overseer",
                "native_thread_id": "thread-1",
                "role": "overseer",
            },
            action={"payload": {"candidate": "candidate-1"}},
            assignment={
                "id": 1,
                "bead_id": "p-1",
                "stage": "reviewing",
                "scope_snapshot": "Approved scope",
                "candidate_id": "candidate-1",
                "source_oid": "abc",
                "worktree_path": "/tmp/worktree",
            },
            evidence=["implementation evidence"],
        )
        self.assertIn("Review the exact submitted candidate", prompt)
        self.assertIn("Approved scope", prompt)
        self.assertIn("implementation evidence", prompt)
        self.assertIn('"required_change"', prompt)


if __name__ == "__main__":
    unittest.main()
