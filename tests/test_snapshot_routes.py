from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from src.routers import analytics, cameras  # noqa: E402


class SnapshotRouteContractTests(unittest.TestCase):
    def test_snapshot_is_exposed_by_cameras_only(self):
        analytics_paths = {route.path for route in analytics.router.routes}
        camera_paths = {route.path for route in cameras.router.routes}

        self.assertNotIn(
            "/admin/analytics/detections/{detection_run_id}/snapshot",
            analytics_paths,
        )
        self.assertNotIn(
            "/admin/analytics/detections/{detection_run_id}/labels",
            analytics_paths,
        )
        self.assertIn("/cameras/{camera_id}/snapshot", camera_paths)


if __name__ == "__main__":
    unittest.main()
