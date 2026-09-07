import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from botocore.exceptions import ClientError
from test_fck_nat_control import import_main


class StartupLockTests(unittest.TestCase):
    def setUp(self):
        self.main = import_main()

    def test_only_one_concurrent_invocation_can_acquire_a_missing_lock(self):
        main = self.main
        original = main.s3_client.put_object
        barrier = threading.Barrier(2, timeout=3)

        def put(**kwargs):
            if "IfNoneMatch" in kwargs:
                barrier.wait()
            return original(**kwargs)

        main.s3_client.put_object = put
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: main.acquire_startup_lock(), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_repeated_worker_does_not_repeat_network_or_asg_operations(self):
        main = self.main
        main.acquire_startup_lock()
        result = main.handle_internal_event({"action": main.START_APP_AFTER_NAT_ACTION})
        self.assertEqual(result["status"], "busy")
        self.assertEqual(main.ssm_client.calls, [])
        self.assertEqual(main.asg_client.set_calls, [])

    def test_only_one_invocation_can_replace_an_expired_lock(self):
        main = self.main
        main.release_startup_lock(main.acquire_startup_lock())
        original = main.s3_client.get_object
        barrier = threading.Barrier(2, timeout=3)

        def read(**kwargs):
            response = original(**kwargs)
            barrier.wait()
            return response

        main.s3_client.get_object = read
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: main.acquire_startup_lock(), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_expired_lock_can_be_replaced_and_old_owner_cannot_release_it(self):
        main = self.main
        old = main.acquire_startup_lock()
        main.release_startup_lock(old)
        current = main.acquire_startup_lock()
        self.assertNotEqual(current, old)
        main.release_startup_lock(old)
        self.assertIsNone(main.acquire_startup_lock())
        main.release_startup_lock(current)
        self.assertIsNotNone(main.acquire_startup_lock())

    def test_worker_releases_lock_after_network_failure(self):
        main = self.main
        main.wait_for_fck_nat_ready = Mock(side_effect=RuntimeError("network failed"))
        with self.assertRaisesRegex(RuntimeError, "network failed"):
            main.handle_internal_event({"action": main.START_APP_AFTER_NAT_ACTION})
        self.assertIsNotNone(main.acquire_startup_lock())

    def test_worker_releases_lock_while_waiting_for_database(self):
        main = self.main
        main.rds_client.state = "starting"
        self.assertEqual(main.handle_internal_event({"action": main.START_APP_AFTER_NAT_ACTION})["status"], "waiting")
        self.assertIsNotNone(main.acquire_startup_lock())

    def test_invalid_or_inaccessible_lock_cannot_start_resources(self):
        main = self.main
        main.acquire_startup_lock()
        for read in [
            Mock(return_value={"Body": io.BytesIO(b'{"expires_at":"invalid"}'), "ETag": "old"}),
            Mock(side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")),
        ]:
            main.s3_client.get_object = read
            with self.assertRaises(Exception):
                main.handle_internal_event({"action": main.START_APP_AFTER_NAT_ACTION})
        self.assertEqual(main.ec2_client.start_calls, [])
        self.assertEqual(main.asg_client.set_calls, [])

    def test_first_acquisition_does_not_need_a_missing_object_read(self):
        main = self.main
        main.s3_client.get_object = Mock(side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject"))
        self.assertIsNotNone(main.acquire_startup_lock())
        main.s3_client.get_object.assert_not_called()

    def test_lock_expiration_outlasts_lambda_timeout(self):
        main = self.main
        main.acquire_startup_lock()
        body, _ = main.s3_client.objects[main.STARTUP_LOCK_KEY]
        from datetime import datetime, timezone
        self.assertGreater(json.loads(body)["expires_at"] - datetime.now(timezone.utc).timestamp(), 600)


if __name__ == '__main__':
    unittest.main()
