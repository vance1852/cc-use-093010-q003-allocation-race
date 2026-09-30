from __future__ import annotations

import unittest
from pathlib import Path

from cooperation_assurance.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observation_count"], 6)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(len(result["input_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
