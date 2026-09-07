import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from botocore.exceptions import ClientError
from test_fck_nat_control import FakeRdsClient, FakeS3Client, import_main


class RdsLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.main = import_main()
        self.main.RDS_INSTANCE_IDENTIFIER = "snorose-dev"

    def worker(self, action):
        return self.main.handle_internal_event({"action": action})

    def test_cold_start_resumes_on_schedule_only_after_database_is_available(self):
        main = self.main
        main.rds_client.state = "stopped"
        self.assertIn("RDS: starting", main.handle_start_dev(["인프라"]))
        self.assertEqual(self.worker(main.START_APP_AFTER_NAT_ACTION)["status"], "waiting")
        self.assertEqual(main.asg_client.set_calls, [])
        self.assertEqual(main.ssm_client.calls, [])

        # Recovery can take longer than a Lambda invocation: no sleeps or self-loop.
        for _ in range(3):
            self.assertEqual(self.worker(main.RECONCILE_RDS_ACTION)["status"], "waiting")
        self.assertEqual(main.rds_client.start_calls, ["snorose-dev"])

        main.rds_client.state = "available"
        self.assertEqual(self.worker(main.RECONCILE_RDS_ACTION)["status"], "scheduled")
        self.assertEqual(self.worker(main.START_APP_AFTER_NAT_ACTION)["status"], "completed")
        self.assertEqual(main.asg_client.set_calls, [1])
        self.assertEqual([c["DocumentName"] for c in main.ssm_client.calls],
                         [main.NAT_READINESS_DOCUMENT, main.WARP_READINESS_DOCUMENT])

    def test_repeated_start_does_not_restart_an_available_database(self):
        main = self.main
        main.asg_client.group["DesiredCapacity"] = 1
        main.start_dev_stack()
        self.worker(main.START_APP_AFTER_NAT_ACTION)
        self.assertEqual(main.rds_client.start_calls, [])
        self.assertEqual(main.asg_client.set_calls, [])

    def test_stop_keeps_database_up_until_last_team_and_app_are_gone(self):
        main = self.main
        main.s3_client = FakeS3Client(["인프라", "프론트엔드"])
        main.asg_client.group.update(DesiredCapacity=1, Instances=[{"InstanceId": "i-app"}])
        main.handle_stop_dev(["인프라"])
        self.assertEqual(main.asg_client.set_calls, [])
        self.worker(main.RECONCILE_RDS_ACTION)
        self.assertEqual(main.rds_client.stop_calls, [])

        main.handle_stop_dev(["프론트엔드"])
        self.assertEqual(main.asg_client.set_calls, [0])
        main.APP_STOP_MAX_ATTEMPTS = 1
        with self.assertRaises(TimeoutError):
            self.worker(main.STOP_NAT_AFTER_APP_ACTION)
        self.worker(main.RECONCILE_RDS_ACTION)
        self.assertEqual(main.rds_client.stop_calls, [])

        main.asg_client.group["Instances"] = []
        self.worker(main.STOP_NAT_AFTER_APP_ACTION)
        self.assertEqual(main.rds_client.stop_calls, ["snorose-dev"])
        self.worker(main.RECONCILE_RDS_ACTION)
        self.assertEqual(main.rds_client.stop_calls, ["snorose-dev"])

    def test_seven_day_auto_restart_is_stopped_when_idle(self):
        main = self.main
        main.s3_client.teams = []
        self.assertEqual(self.worker(main.RECONCILE_RDS_ACTION)["rds"], "stopping")
        main.rds_client.state = "stopped"
        self.assertEqual(self.worker(main.RECONCILE_RDS_ACTION)["status"], "completed")
        self.assertEqual(main.rds_client.stop_calls, ["snorose-dev"])

    def test_start_during_stopping_waits_then_restarts_database(self):
        main = self.main
        main.rds_client.state = "stopping"
        self.worker(main.START_APP_AFTER_NAT_ACTION)
        self.assertEqual(main.rds_client.start_calls, [])
        self.assertEqual(main.asg_client.set_calls, [])
        main.rds_client.state = "stopped"
        self.assertEqual(self.worker(main.RECONCILE_RDS_ACTION)["rds"], "starting")
        self.assertEqual(main.rds_client.start_calls, ["snorose-dev"])

    def test_rds_change_during_network_checks_prevents_app_start(self):
        main = self.main
        main.wait_for_network_ready = lambda *args: setattr(main.rds_client, "state", "stopping")
        self.assertEqual(self.worker(main.START_APP_AFTER_NAT_ACTION)["status"], "waiting")
        self.assertEqual(main.asg_client.set_calls, [])

    def test_cancelled_start_never_starts_database(self):
        main = self.main
        main.s3_client.teams = []
        main.rds_client.state = "stopped"
        self.assertEqual(self.worker(main.START_APP_AFTER_NAT_ACTION)["status"], "cancelled")
        self.assertEqual(main.rds_client.start_calls, [])

    def test_new_team_between_idle_checks_prevents_database_stop(self):
        main = self.main
        main.s3_client.teams = []
        def describe(**kwargs):
            main.s3_client.teams = ["프론트엔드"]
            return {"DBInstances": [{"DBInstanceStatus": "available"}]}
        main.rds_client.describe_db_instances = describe
        main.stop_rds_if_idle()
        self.assertEqual(main.rds_client.stop_calls, [])

    def test_old_stop_worker_does_not_stop_database_after_new_start(self):
        main = self.main
        main.asg_client.group["DesiredCapacity"] = 1
        self.assertEqual(self.worker(main.STOP_NAT_AFTER_APP_ACTION)["status"], "cancelled")
        self.assertEqual(main.rds_client.stop_calls, [])

    def test_manual_app_capacity_protects_database_without_team_registration(self):
        main = self.main
        main.s3_client.teams = []
        main.asg_client.group["DesiredCapacity"] = 1
        self.worker(main.RECONCILE_RDS_ACTION)
        self.assertEqual(main.rds_client.stop_calls, [])
        main.rds_client.state = "stopped"
        self.worker(main.RECONCILE_RDS_ACTION)
        self.assertEqual(main.rds_client.start_calls, ["snorose-dev"])

    def test_missing_or_invalid_team_state_cannot_stop_database(self):
        main = self.main
        invalid_bodies = [b"", b"{}", b"[]", b"not json", b'{"active_teams": null}',
                          b'{"active_teams": "infra"}', b'{"active_teams": [null]}']
        for body in invalid_bodies:
            with self.subTest(body=body):
                main.s3_client.get_object = lambda **kwargs: {"Body": io.BytesIO(body)}
                with self.assertRaises(main.ActiveTeamsStateError):
                    self.worker(main.RECONCILE_RDS_ACTION)
        main.s3_client.get_object = Mock(side_effect=ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"))
        with self.assertRaises(main.ActiveTeamsStateError):
            main.stop_rds_if_idle()
        self.assertEqual(main.rds_client.stop_calls, [])

    def test_aws_lookup_failures_cannot_stop_database(self):
        main = self.main
        main.s3_client.teams = []
        main.asg_client.describe_auto_scaling_groups = Mock(side_effect=RuntimeError("unavailable"))
        with self.assertRaises(main.AsgLookupError):
            self.worker(main.RECONCILE_RDS_ACTION)
        self.assertEqual(main.rds_client.stop_calls, [])

    def test_database_permission_failure_prevents_app_start(self):
        main = self.main
        main.rds_client.describe_db_instances = Mock(side_effect=ClientError(
            {"Error": {"Code": "AccessDenied"}}, "DescribeDBInstances"))
        with self.assertRaises(ClientError):
            self.worker(main.START_APP_AFTER_NAT_ACTION)
        self.assertEqual(main.asg_client.set_calls, [])

    def test_concurrent_database_transition_is_reloaded(self):
        main = self.main
        main.rds_client.state = "stopped"
        def start(**kwargs):
            main.rds_client.state = "starting"
            raise ClientError({"Error": {"Code": "InvalidDBInstanceState"}}, "StartDBInstance")
        main.rds_client.start_db_instance = start
        self.assertEqual(main.start_rds(), "starting")

    def test_transitional_or_failed_database_does_not_start_application(self):
        main = self.main
        for state in ["backing-up", "modifying", "rebooting", "failed", "incompatible-network"]:
            with self.subTest(state=state):
                main.rds_client.state = state
                self.assertEqual(self.worker(main.START_APP_AFTER_NAT_ACTION)["status"], "waiting")
        self.assertEqual(main.asg_client.set_calls, [])

    def test_status_warns_when_app_is_running_but_database_is_not_ready(self):
        main = self.main
        main.get_instance_state = lambda: "running"
        main.get_instance_status = lambda: "✅ 상태 검사 통과!"
        main.get_fck_nat_state = main.get_warp_state = lambda: "running"
        main.check_app_health = lambda: "✅ 애플리케이션 응답 정상"
        main.rds_client.state = "starting"
        message = main.handle_status_dev()
        self.assertTrue(message.startswith("⚠️"))
        self.assertIn("RDS: starting", message)

    def lease(self, active=True):
        main = self.main
        original_get = main.s3_client.get_object
        key = "dev-manager/deployments/123-1.json"
        main.s3_client.list_objects_v2 = lambda **kwargs: {"Contents": [{"Key": key}]}
        expires_at = int(datetime.now(timezone.utc).timestamp()) + 7200 if active else 0
        def get(Bucket, Key):
            if Key == key:
                return {"Body": io.BytesIO(json.dumps({"expires_at": expires_at}).encode())}
            return original_get(Bucket=Bucket, Key=Key)
        main.s3_client.get_object = get

    def test_deployment_without_active_teams_protects_database(self):
        main = self.main
        self.lease()
        main.s3_client.teams = []
        self.worker(main.RECONCILE_RDS_ACTION)
        self.assertEqual(main.rds_client.stop_calls, [])
        self.assertEqual(main.lambda_client.invocations, [])

    def test_last_team_cannot_stop_resources_during_deployment(self):
        main = self.main
        self.lease()
        main.asg_client.group["DesiredCapacity"] = 1
        message = main.handle_stop_dev(["인프라"])
        self.assertIn("배포", message)
        self.assertEqual(main.s3_client.teams, ["인프라"])
        self.assertEqual(main.asg_client.set_calls, [])
        self.assertEqual(main.rds_client.stop_calls, [])

    def test_status_recognizes_cd_start_without_registered_teams(self):
        main = self.main
        self.lease()
        main.s3_client.teams = []
        main.rds_client.state = "starting"
        main.get_instance_state = lambda: "stopped"
        main.get_instance_status = lambda: ""
        main.get_fck_nat_state = main.get_warp_state = lambda: "stopped"
        message = main.handle_status_dev()
        self.assertIn("시작 중", message)
        self.assertIn("RDS: starting", message)
        self.assertNotIn("중지 중", message)

    def test_deployment_cancels_an_old_network_stop_worker(self):
        main = self.main
        self.lease()
        main.s3_client.teams = []
        self.assertEqual(self.worker(main.STOP_NAT_AFTER_APP_ACTION)["status"], "cancelled")
        self.assertEqual(main.ec2_client.stop_calls, [])
        self.assertEqual(main.rds_client.stop_calls, [])

    def test_expired_lease_does_not_keep_idle_database_up(self):
        main = self.main
        self.lease(active=False)
        main.s3_client.teams = []
        self.worker(main.RECONCILE_RDS_ACTION)
        self.assertEqual(main.rds_client.stop_calls, ["snorose-dev"])

    def test_unreadable_deployment_state_prevents_stop(self):
        main = self.main
        main.s3_client.teams = []
        main.s3_client.list_objects_v2 = Mock(side_effect=RuntimeError("AccessDenied"))
        with self.assertRaises(RuntimeError):
            self.worker(main.RECONCILE_RDS_ACTION)
        self.assertEqual(main.rds_client.stop_calls, [])

    def test_deployment_listing_is_paginated(self):
        main = self.main
        self.lease()
        main.s3_client.list_objects_v2 = Mock(side_effect=[
            {"Contents": [], "IsTruncated": True, "NextContinuationToken": "next"},
            {"Contents": [{"Key": "dev-manager/deployments/123-1.json"}]},
        ])
        self.assertTrue(main.has_active_deployment())
        self.assertEqual(main.s3_client.list_objects_v2.call_args.kwargs["ContinuationToken"], "next")


if __name__ == "__main__":
    unittest.main()
