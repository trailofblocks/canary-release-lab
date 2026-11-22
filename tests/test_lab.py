import unittest

from canary_lab.app import Checkout, Counters
from canary_lab.runner import LabError, traffic_delta


class CheckoutTests(unittest.TestCase):
    def test_healthy_and_faulty_releases_are_deterministic(self):
        healthy = Checkout("v2", "aaa", 0)
        faulty = Checkout("v3", "bbb", 3)
        self.assertEqual([healthy.respond() for _ in range(6)], [200] * 6)
        self.assertEqual([faulty.respond() for _ in range(6)], [200, 200, 500, 200, 200, 500])

    def test_metrics_keep_revision_and_error_class_separate(self):
        traffic = Counters()
        traffic.record("v2", "aaa", 200)
        before = traffic.snapshot()
        traffic.record("v2", "aaa", 200)
        traffic.record("v3", "bbb", 500)
        traffic.record(None, None, 503)
        self.assertIn('revision="bbb",outcome="error"} 1', traffic.metrics())
        self.assertEqual(traffic_delta(before, traffic.snapshot()), {
            "v2": {"requests": 1, "errors": 0},
            "v3": {"requests": 1, "errors": 1},
            "unknown": {"requests": 1, "errors": 1},
        })

    def test_counter_reset_cannot_turn_into_a_healthy_sample(self):
        traffic = Counters()
        traffic.record("v2", "aaa", 200)
        with self.assertRaisesRegex(LabError, "counters reset"):
            traffic_delta(traffic.snapshot(), [])
