from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path

from cooperation_assurance.contracts import Observation, Protocol, ValidationError
from cooperation_assurance.jsonio import load_json


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.protocol = Protocol.from_dict(self.raw_protocol)

    def test_protocol_evidence_revisions_indexes(self) -> None:
        self.assertEqual(self.protocol.version, 1)
        self.assertEqual(self.protocol.stratum_keys, {"verified-channel", "restricted-channel"})
        self.assertEqual(set(self.protocol.metric_map), {"compliant", "review_latency_hours", "exception_count"})

    def test_protocol_rejects_duplicate_metric(self) -> None:
        raw = deepcopy(self.raw_protocol)
        raw["metrics"].append(deepcopy(raw["metrics"][0]))
        with self.assertRaisesRegex(ValidationError, "不能重复"):
            Protocol.from_dict(raw)

    def test_observation_rejects_unknown_stratum(self) -> None:
        raw = {
            "source_batch": "batch",
            "source_row": "1",
            "program_id": "r1",
            "protocol_id": self.protocol.protocol_id,
            "protocol_version": self.protocol.version,
            "stratum_key": "unknown",
            "observed_at": "2026-09-21T10:00:00+08:00",
            "metrics": {"compliant": 1, "review_latency_hours": 4, "exception_count": 0},
            "excluded_reason": None,
        }
        with self.assertRaisesRegex(ValidationError, "未在协议中声明"):
            Observation.from_dict(raw, self.protocol)

    def test_binary_metric_is_strict(self) -> None:
        raw = {
            "source_batch": "batch",
            "source_row": "1",
            "program_id": "r1",
            "protocol_id": self.protocol.protocol_id,
            "protocol_version": self.protocol.version,
            "stratum_key": "verified-channel",
            "observed_at": "2026-09-21T10:00:00+08:00",
            "metrics": {"compliant": 2, "review_latency_hours": 4, "exception_count": 0},
            "excluded_reason": None,
        }
        with self.assertRaisesRegex(ValidationError, "必须是 0 或 1"):
            Observation.from_dict(raw, self.protocol)


if __name__ == "__main__":
    unittest.main()

