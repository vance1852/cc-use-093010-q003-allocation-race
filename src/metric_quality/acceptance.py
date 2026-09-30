"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import MetricQualityService


def run() -> dict:
    service = MetricQualityService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "metric-admin")
    service.create_lot(token, "BATCH-DEMO", "cross-border-service-index", "POLICY-3.2", 10)
    for report_period, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, "BATCH-DEMO", report_period, response, .01, "reporting-gateway-1")
    result = service.analyze(token, "BATCH-DEMO")
    service.approve(token, "BATCH-DEMO", "hold", "awaiting data quality review")
    return {"status": "ok", "batch": result["lot_id"], "peak_period": result["response_profile"]["peak_test_frequency_hz"], "events": len(service.audit(token, "BATCH-DEMO"))}


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
