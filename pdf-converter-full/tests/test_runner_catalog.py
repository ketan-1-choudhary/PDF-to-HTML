import asyncio
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_skill


class ResultMessage:
    def __init__(self, is_error=False, subtype="success"):
        self.content = []
        self.error = None
        self.is_error = is_error
        self.subtype = subtype
        self.duration_ms = 1
        self.num_turns = 1


class RunnerTests(unittest.TestCase):
    def test_prompt_uses_skill_root_and_requires_sequential_pages(self):
        prompt = run_skill.build_prompt("input.pdf", 1, "user", "book", "bucket", "work")
        self.assertIn(".claude/skills/pdf-to-html/SKILL.md", prompt)
        self.assertIn("skill root", prompt)
        self.assertIn("sequentially", prompt)

    def test_catalog_preflight_propagates_mismatch(self):
        failed = subprocess.CompletedProcess([], 1, "", "designId not in catalog: box-x")
        with patch.object(run_skill.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "box-x"):
                run_skill.preflight_design_catalog("book", 1, "bucket", "work")

    def test_agent_failure_and_missing_final_result_fail_the_job(self):
        async def failed_query(**kwargs):
            yield ResultMessage(is_error=True, subtype="error_during_execution")

        async def missing_query(**kwargs):
            if False:
                yield None

        env = {"S3_BUCKET": "bucket", "DDB_TABLE_NAME": "table"}
        with patch.dict(os.environ, env), patch.object(run_skill, "preflight_design_catalog"):
            with patch.object(run_skill, "query", failed_query):
                with self.assertRaisesRegex(RuntimeError, "conversion agent failed"):
                    asyncio.run(run_skill.convert_page("input.pdf", 1, "user", "book", "bucket", "work"))
            with patch.object(run_skill, "query", missing_query):
                with self.assertRaisesRegex(RuntimeError, "without a final result"):
                    asyncio.run(run_skill.convert_page("input.pdf", 1, "user", "book", "bucket", "work"))


if __name__ == "__main__":
    unittest.main()
