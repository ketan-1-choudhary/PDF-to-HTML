import re
import unittest
from importlib.util import find_spec
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "pdf-to-html"
PROJECT_ROOT = SKILL_ROOT.parents[2]


class SkillWorkflowTests(unittest.TestCase):
    @unittest.skipUnless(find_spec("yaml"), "PyYAML is needed to validate skill frontmatter")
    def test_skill_frontmatter_is_valid_yaml(self):
        import yaml

        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        _, frontmatter, _ = skill.split("---", 2)
        metadata = yaml.safe_load(frontmatter)
        self.assertEqual(metadata["name"], "pdf-to-html")
        self.assertIn("whole-book profiling", metadata["description"])

    def test_catalog_calls_and_verification_are_wired_in_order(self):
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        sections = re.split(r"^### STEP \d+ [^\n]*\n", skill, flags=re.MULTILINE)
        self.assertEqual(len(sections), 13)
        self.assertIn("--normalized-long-edge 1540", sections[2])
        self.assertIn('scripts/design_catalog.py" brief', sections[2])
        self.assertIn('--designs "$WORK/designs_normalized.json"', sections[2])
        self.assertIn("$WORK/design_brief.json", sections[5])
        self.assertIn("prepare_refine_brief.py", sections[8])
        self.assertIn('$WORK/final_verification.json', sections[9])
        self.assertIn("retained final HTML/CSS", sections[10])
        harvest = sections[11].index("scripts/harvest_designs.py")
        commit = sections[11].index('scripts/design_catalog.py" commit')
        publish = sections[11].index("node \"scripts/publish_page.mjs\"")
        self.assertLess(harvest, commit)
        self.assertLess(commit, publish)
        self.assertIn("Design IDs referenced, harvested and committed", sections[12])

    def test_catalog_generation_contract_is_in_both_references(self):
        contract = (SKILL_ROOT / "references" / "output-contract.md").read_text(encoding="utf-8")
        prompts = (SKILL_ROOT / "references" / "prompts.md").read_text(encoding="utf-8")
        for text in (contract, prompts):
            self.assertIn("data-design-id", text)
            self.assertIn("/* @design", text)
            self.assertIn("/* @end", text)
            self.assertIn("--design-", text)

    def test_container_installs_locked_helpers_and_uses_system_chromium(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("npm ci --prefix /app/.claude/skills/pdf-to-html/scripts", dockerfile)
        self.assertIn("PUPPETEER_SKIP_DOWNLOAD=true", dockerfile)
        self.assertIn("PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium", dockerfile)


if __name__ == "__main__":
    unittest.main()
