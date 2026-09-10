import unittest
from unittest.mock import Mock

from botocore.exceptions import ClientError
from test_asg_control import FakeAsgClient, import_main


READY = {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": "LOCAL_REDIS_READY\n"}


class RedisStatusTest(unittest.TestCase):
    def setUp(self):
        self.main = import_main()
        self.main.asg_client = FakeAsgClient(
            desired=1, instances=[{"InstanceId": "i-app", "LifecycleState": "InService"}])
        self.client = Mock()
        self.client.send_command.return_value = {"Command": {"CommandId": "redis-check"}}
        self.client.get_command_invocation.return_value = READY.copy()
        self.main.get_ssm_client = Mock(return_value=self.client)
        self.main.time = Mock()

    def test_writable_probe_is_required_and_uses_fixed_document(self):
        self.assertTrue(self.main.check_redis_health().startswith("✅"))
        self.client.send_command.assert_called_once_with(
            InstanceIds=["i-app"], DocumentName="snorose-dev-redis-ready",
            DocumentVersion="$DEFAULT", TimeoutSeconds=30)

    def test_eventual_consistency_and_pending_result_are_retried_without_resending(self):
        self.client.get_command_invocation.side_effect = [
            ClientError({"Error": {"Code": "InvocationDoesNotExist"}}, "GetCommandInvocation"),
            {"Status": "InProgress", "ResponseCode": -1}, READY,
        ]
        self.assertTrue(self.main.check_redis_health().startswith("✅"))
        self.client.send_command.assert_called_once()
        self.assertEqual(self.main.time.sleep.call_count, 2)

    def test_healthy_alb_cannot_mask_failed_or_unknown_redis(self):
        main = self.main
        main.get_instance_state = lambda: "running"
        main.get_instance_status = lambda: "✅ 상태 검사 통과!"
        main.get_fck_nat_state = main.get_warp_state = lambda: "running"
        main.get_rds_state = lambda: "available"
        main.load_active_teams = lambda: ["인프라"]
        main.check_app_health = lambda: "✅ 애플리케이션 응답 정상"
        for reply in [READY, {"Status": "Failed", "ResponseCode": 1},
                      {**READY, "StandardOutputContent": "LOCAL_REDIS_NOT_CONFIGURED"},
                      {"Status": "TimedOut"}]:
            with self.subTest(reply=reply):
                self.client.get_command_invocation.return_value = reply
                message = main.handle_status_dev()
                self.assertTrue(message.startswith("✅" if reply == READY else "⚠️"), message)
                self.assertIn("Redis:", message)

    def test_legacy_ami_is_not_reported_as_healthy_or_as_redis_failure(self):
        self.client.get_command_invocation.return_value = {
            **READY, "StandardOutputContent": "LOCAL_REDIS_NOT_CONFIGURED"}
        message = self.main.check_redis_health()
        self.assertTrue(message.startswith("⚠️"))
        self.assertIn("미설정", message)

    def test_command_output_and_aws_error_text_are_never_exposed(self):
        for status, code, output in [("Success", 0, "private-value"), ("Success", 1, "LOCAL_REDIS_READY"),
                                     ("Failed", 1, "private-value"), ("Cancelled", -1, "private-value")]:
            with self.subTest(status=status, code=code):
                self.client.get_command_invocation.return_value = {
                    "Status": status, "ResponseCode": code,
                    "StandardOutputContent": output, "StandardErrorContent": "private-value"}
                message = self.main.check_redis_health()
                self.assertFalse(message.startswith("✅"))
                self.assertNotIn("private-value", message)
        self.client.send_command.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "private-value"}}, "SendCommand")
        message = self.main.check_redis_health()
        self.assertIn("확인하지 못했습니다", message)
        self.assertNotIn("private-value", message)

    def test_missing_document_or_unmanaged_instance_is_unknown(self):
        for code in ["InvalidDocument", "InvalidInstanceId"]:
            with self.subTest(code=code):
                self.client.send_command.side_effect = ClientError({"Error": {"Code": code}}, "SendCommand")
                self.assertIn("확인하지 못했습니다", self.main.check_redis_health())

    def test_polling_has_a_limit_and_never_claims_pending_is_healthy(self):
        self.client.get_command_invocation.return_value = {"Status": "Pending", "ResponseCode": -1}
        self.assertIn("대기 시간이 초과", self.main.check_redis_health())
        self.assertEqual(self.client.get_command_invocation.call_count, self.main.REDIS_STATUS_MAX_ATTEMPTS)
        self.assertEqual(self.main.time.sleep.call_count, self.main.REDIS_STATUS_MAX_ATTEMPTS - 1)
        self.client.send_command.assert_called_once()

    def test_ambiguous_or_terminating_instances_are_not_probed(self):
        for instances in [[], [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}],
                          [{"InstanceId": "i-app", "LifecycleState": "Terminating:Wait"}]]:
            with self.subTest(instances=instances):
                self.main.asg_client.group["Instances"] = instances
                self.assertIn("하나로 확인", self.main.check_redis_health())
        self.client.send_command.assert_not_called()

    def test_replaced_instance_result_is_not_reported_as_current_health(self):
        def replace(**kwargs):
            self.main.asg_client.group["Instances"] = [{"InstanceId": "i-new"}]
            return READY
        self.client.get_command_invocation.side_effect = replace
        self.assertIn("교체·종료", self.main.check_redis_health())

    def test_stopped_environment_is_not_started_or_probed_by_status(self):
        main = self.main
        main.get_instance_state = lambda: "stopped"
        main.get_instance_status = lambda: ""
        main.get_fck_nat_state = main.get_warp_state = main.get_rds_state = lambda: "stopped"
        main.load_active_teams = lambda: []
        main.check_redis_health = Mock()
        self.assertIn("중지되었습니다", main.handle_status_dev())
        main.check_redis_health.assert_not_called()
        self.assertEqual(main.asg_client.set_calls, [])

    def test_stop_notice_only_follows_a_successful_scale_down(self):
        self.assertIn("다시 로그인", self.main.stop_instance())
        self.assertEqual(self.main.asg_client.set_calls, [0])
        self.assertNotIn("다시 로그인", self.main.stop_instance())  # Already stopped.
        self.main.asg_client.group.update(DesiredCapacity=1, MinSize=1)
        self.assertNotIn("다시 로그인", self.main.stop_instance())  # Invalid ASG configuration.

    def test_other_teams_and_deployment_protection_do_not_claim_session_reset(self):
        main = self.main
        main.load_active_teams = lambda: ["인프라", "백엔드"]
        main.save_active_teams = Mock()
        self.assertNotIn("다시 로그인", main.handle_stop_dev(["인프라"]))
        main.load_active_teams = lambda: ["인프라"]
        main.has_active_deployment = lambda: True
        self.assertNotIn("다시 로그인", main.handle_stop_dev(["인프라"]))
        self.assertEqual(main.asg_client.set_calls, [])


if __name__ == "__main__":
    unittest.main()
