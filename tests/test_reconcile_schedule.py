import unittest
from unittest.mock import Mock

from test_fck_nat_control import import_main


class ReconcileScheduleTest(unittest.TestCase):
    def setUp(self):
        self.main = import_main()
        self.main.PENDING_RECONCILE_RULE = "snorose-dev-manager-bot-rds-pending"
        self.main.events_client = Mock()

    def reconcile(self):
        return self.main.handle_internal_event({"action": self.main.RECONCILE_RDS_ACTION})

    def idle(self):
        self.main.s3_client.teams = []
        self.main.rds_client.state = "stopped"
        self.main.ec2_client.state = self.main.ec2_client.warp_state = "stopped"

    def test_stopped_idle_environment_disables_fast_checks_without_starting_resources(self):
        self.idle()
        self.reconcile()
        self.main.events_client.disable_rule.assert_called_once_with(Name=self.main.PENDING_RECONCILE_RULE)
        self.main.events_client.enable_rule.assert_not_called()
        self.assertEqual(self.main.rds_client.start_calls, [])
        self.assertEqual(self.main.ec2_client.start_calls, [])
        self.assertEqual(self.main.lambda_client.invocations, [])

    def test_healthy_running_app_does_not_need_fast_checks(self):
        self.main.asg_client.group["DesiredCapacity"] = 1
        self.reconcile()
        self.main.events_client.disable_rule.assert_called_once()
        self.main.events_client.enable_rule.assert_not_called()

    def test_start_enables_fast_checks_and_stops_them_after_capacity_is_ready(self):
        main = self.main
        main.rds_client.state = "stopped"
        main.handle_start_dev(["인프라"])
        main.events_client.enable_rule.assert_called_once_with(Name=main.PENDING_RECONCILE_RULE)
        self.reconcile()
        self.assertEqual(main.asg_client.set_calls, [])
        main.rds_client.state = "available"
        self.assertEqual(self.reconcile()["status"], "scheduled")
        main.handle_internal_event({"action": main.START_APP_AFTER_NAT_ACTION})
        self.reconcile()
        main.events_client.disable_rule.assert_called_once()
        self.assertEqual(main.asg_client.set_calls, [1])

    def test_reconcile_after_lost_event_recovers_requested_start(self):
        # The same handler is reached by the 15-minute safety net and RDS events.
        self.main.rds_client.state = "available"
        self.assertEqual(self.reconcile()["status"], "scheduled")
        self.main.events_client.enable_rule.assert_called_once()
        self.main.events_client.disable_rule.assert_not_called()

    def test_stop_keeps_fast_checks_until_database_and_network_finish(self):
        main = self.main
        main.asg_client.group.update(DesiredCapacity=1, Instances=[{"InstanceId": "i-app"}])
        main.ec2_client.state = main.ec2_client.warp_state = "running"
        def enabled_after_intent(**kwargs):
            self.assertEqual(main.asg_client.group["DesiredCapacity"], 0)
        main.events_client.enable_rule.side_effect = enabled_after_intent
        main.handle_stop_dev(["인프라"])
        self.reconcile()
        main.events_client.disable_rule.assert_not_called()
        self.assertEqual(main.rds_client.stop_calls, [])
        main.asg_client.group["Instances"] = []
        self.reconcile()
        main.events_client.disable_rule.assert_not_called()
        main.rds_client.state = "stopped"
        self.reconcile()
        main.events_client.disable_rule.assert_not_called()  # NAT is still stopping.
        main.ec2_client.state = "stopped"
        self.reconcile()
        main.events_client.disable_rule.assert_called_once()

    def test_automatic_database_restart_wakes_cleanup_without_a_team(self):
        self.idle()
        self.main.rds_client.state = "available"
        self.reconcile()
        self.assertEqual(self.main.rds_client.stop_calls, ["snorose-dev"])
        self.main.events_client.enable_rule.assert_called_once()
        self.main.rds_client.state = "stopped"
        self.reconcile()
        self.main.events_client.disable_rule.assert_called_once()

    def test_deployment_without_a_team_keeps_fast_checks_until_lease_is_released(self):
        self.idle()
        self.main.has_active_deployment = lambda: True
        self.reconcile()
        self.main.events_client.enable_rule.assert_called_once()
        self.assertEqual(self.main.rds_client.stop_calls, [])
        self.main.has_active_deployment = lambda: False
        self.main.rds_client.state = "available"
        self.reconcile()
        self.assertEqual(self.main.rds_client.stop_calls, ["snorose-dev"])

    def test_new_start_during_disable_reenables_fast_checks(self):
        self.idle()
        def concurrent_start(**kwargs):
            self.main.s3_client.teams = ["인프라"]
        self.main.events_client.disable_rule.side_effect = concurrent_start
        self.main.sync_reconcile_schedule()
        self.main.events_client.disable_rule.assert_called_once()
        self.main.events_client.enable_rule.assert_called_once()

    def test_new_stop_during_disable_reenables_fast_checks(self):
        self.main.asg_client.group.update(DesiredCapacity=1, Instances=[{"InstanceId": "i-app"}])
        def concurrent_stop(**kwargs):
            self.main.s3_client.teams = []
            self.main.asg_client.group["DesiredCapacity"] = 0
        self.main.events_client.disable_rule.side_effect = concurrent_stop
        self.main.sync_reconcile_schedule()
        self.main.events_client.enable_rule.assert_called_once()

    def test_unknown_state_never_disables_fast_checks(self):
        self.main.load_active_teams = Mock(side_effect=self.main.ActiveTeamsStateError("missing state"))
        with self.assertRaises(self.main.ActiveTeamsStateError):
            self.main.sync_reconcile_schedule()
        self.main.events_client.disable_rule.assert_not_called()

    def test_failed_recheck_restores_fast_checks(self):
        self.main.needs_fast_reconcile = Mock(side_effect=[False, RuntimeError("read failed")])
        with self.assertRaisesRegex(RuntimeError, "read failed"):
            self.main.sync_reconcile_schedule()
        self.main.events_client.disable_rule.assert_called_once()
        self.main.events_client.enable_rule.assert_called_once()

    def test_rule_permission_failure_prevents_new_resource_start(self):
        self.main.rds_client.state = "stopped"
        self.main.events_client.enable_rule.side_effect = RuntimeError("rule permission denied")
        self.assertIn("시작 예약 실패", self.main.handle_start_dev(["인프라"]))
        self.assertEqual(self.main.rds_client.start_calls, [])
        self.assertEqual(self.main.ec2_client.start_calls, [])

    def test_stopping_does_not_claim_cleanup_scheduled_when_enable_fails(self):
        self.main.asg_client.group["DesiredCapacity"] = 1
        self.main.events_client.enable_rule.side_effect = RuntimeError("rule permission denied")
        self.assertIn("후속 정리 예약 실패", self.main.handle_stop_dev(["인프라"]))
        self.assertEqual(self.main.asg_client.set_calls, [0])
        self.assertEqual(self.main.lambda_client.invocations, [])

    def test_legacy_configuration_uses_existing_periodic_reconciliation(self):
        self.main.PENDING_RECONCILE_RULE = None
        self.main.set_pending_reconcile(True)
        self.main.sync_reconcile_schedule()
        self.assertEqual(self.main.events_client.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
