import json
import time
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from test_fck_nat_control import FakeAsgClient, FakeS3Client, import_main


class FakeElbv2Client:
    def __init__(self, targets):
        self.targets = targets

    def describe_target_health(self, TargetGroupArn):
        return {"TargetHealthDescriptions": self.targets}


class ReadyNotificationTest(unittest.TestCase):
    def setUp(self):
        self.main = import_main()
        self.main.time.time = time.time
        self.notification = {
            "interaction_id": "123", "user_id": "321", "channel_id": "789",
            "roles": ["인프라"], "deadline": time.time() + 3600,
        }

    def test_start_schedules_notification_independently_of_rds_recovery(self):
        main = self.main
        main.rds_client.state = "stopped"

        message = main.handle_start_dev(["인프라"], self.notification)

        self.assertIn("멘션해 알려드립니다", message)
        self.assertEqual(
            [call["Payload"]["action"] for call in main.lambda_client.invocations],
            [main.START_APP_AFTER_NAT_ACTION, main.WAIT_FOR_APP_READY_ACTION],
        )
        self.assertEqual(main.lambda_client.invocations[1]["Payload"]["notification"], self.notification)

    def test_healthy_old_target_does_not_trigger_ready_message(self):
        main = self.main
        main.asg_client = FakeAsgClient(
            desired=1, instances=[{"InstanceId": "i-current", "LifecycleState": "InService"}],
        )
        main.asg_client.group["TargetGroupARNs"] = ["arn:target-group"]
        main.elbv2_client = FakeElbv2Client([
            {"Target": {"Id": "i-old"}, "TargetHealth": {"State": "healthy"}},
            {"Target": {"Id": "i-current"}, "TargetHealth": {"State": "initial"}},
        ])

        self.assertFalse(main.is_app_ready())

    def test_ready_message_mentions_only_requester(self):
        main = self.main
        main.DISCORD_BOT_TOKEN = "test-token"
        main.asg_client = FakeAsgClient(
            desired=1, instances=[{"InstanceId": "i-current", "LifecycleState": "InService"}],
        )
        main.asg_client.group["TargetGroupARNs"] = ["arn:target-group"]
        main.elbv2_client = FakeElbv2Client([
            {"Target": {"Id": "i-current"}, "TargetHealth": {"State": "healthy"}},
        ])

        with patch("urllib.request.urlopen", return_value=nullcontext()) as send:
            result = main.handle_internal_event({
                "action": main.WAIT_FOR_APP_READY_ACTION, "notification": self.notification,
            })

        self.assertEqual(result["status"], "completed")
        req = send.call_args.args[0]
        self.assertEqual(req.full_url, "https://discord.com/api/v10/channels/789/messages")
        payload = json.loads(req.data)
        self.assertEqual(payload["content"], "<@321> ✅ DEV 앱 서버 준비가 완료됐습니다.")
        self.assertEqual(payload["allowed_mentions"], {"parse": [], "users": ["321"]})
        self.assertEqual(payload["nonce"], "123")
        self.assertTrue(payload["enforce_nonce"])

    def test_wait_continues_while_rds_starts(self):
        main = self.main
        main.rds_client.state = "starting"
        main.APP_READY_MAX_ATTEMPTS = 1

        result = main.wait_for_app_ready(self.notification)

        self.assertEqual(result["status"], "waiting")
        self.assertEqual(main.lambda_client.invocations[0]["Payload"]["notification"], self.notification)

    def test_notification_stops_when_requesting_team_stops(self):
        main = self.main
        main.s3_client = FakeS3Client([])

        with patch("urllib.request.urlopen") as send:
            result = main.wait_for_app_ready(self.notification)

        self.assertEqual(result["status"], "cancelled")
        send.assert_not_called()

    def test_timeout_mentions_requester_without_claiming_ready(self):
        main = self.main
        main.DISCORD_BOT_TOKEN = "test-token"
        main.rds_client.state = "starting"
        self.notification["deadline"] = 0

        with patch("urllib.request.urlopen", return_value=nullcontext()) as send:
            result = main.wait_for_app_ready(self.notification)

        self.assertEqual(result["status"], "timed_out")
        content = json.loads(send.call_args.args[0].data)["content"]
        self.assertIn("<@321>", content)
        self.assertIn("준비되지 않았습니다", content)


if __name__ == "__main__":
    unittest.main()
