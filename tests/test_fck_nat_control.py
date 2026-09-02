import importlib
import io
import json
import os
import pathlib
import sys
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "src" / "app"
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


class FakeEc2Client:
    def __init__(self, state="stopped", ready=True):
        self.state = state
        self.ready = ready
        self.start_calls = []
        self.stop_calls = []

    def describe_instances(self, Filters):
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-fcknat",
                            "State": {"Name": self.state},
                        }
                    ]
                }
            ]
        }

    def describe_instance_status(self, InstanceIds, IncludeAllInstances):
        if self.state != "running" or not self.ready:
            return {"InstanceStatuses": []}
        return {
            "InstanceStatuses": [
                {
                    "InstanceStatus": {"Status": "ok"},
                    "SystemStatus": {"Status": "ok"},
                }
            ]
        }

    def start_instances(self, InstanceIds):
        self.start_calls.append(InstanceIds)
        self.state = "running"

    def stop_instances(self, InstanceIds):
        self.stop_calls.append(InstanceIds)
        self.state = "stopping"


class FakeAsgClient:
    def __init__(self, desired=0, instances=None):
        self.group = {
            "AutoScalingGroupName": "snorose-dev-application-asg",
            "MinSize": 0,
            "MaxSize": 1,
            "DesiredCapacity": desired,
            "Instances": instances or [],
            "TargetGroupARNs": [],
        }
        self.set_calls = []

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {"AutoScalingGroups": [self.group]}

    def set_desired_capacity(self, AutoScalingGroupName, DesiredCapacity, HonorCooldown):
        self.set_calls.append(DesiredCapacity)
        self.group["DesiredCapacity"] = DesiredCapacity


class FakeLambdaClient:
    def __init__(self):
        self.invocations = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invocations.append(
            {
                "FunctionName": FunctionName,
                "InvocationType": InvocationType,
                "Payload": json.loads(Payload),
            }
        )
        return {"StatusCode": 202}


class FakeS3Client:
    def __init__(self, teams):
        self.teams = teams

    def get_object(self, Bucket, Key):
        body = json.dumps({"active_teams": self.teams}).encode()
        return {"Body": io.BytesIO(body)}


def import_main():
    sys.modules.pop("main", None)
    return importlib.import_module("main")


class FckNatControlTest(unittest.TestCase):
    def test_start_sequence_starts_nat_and_dispatches_async_worker(self):
        main = import_main()
        fake_ec2 = FakeEc2Client(state="stopped")
        fake_lambda = FakeLambdaClient()
        main.ec2_client = fake_ec2
        main.lambda_client = fake_lambda
        main.LAMBDA_FUNCTION_NAME = "SnoroseDevManagerBot"

        message = main.start_dev_stack()

        self.assertEqual(fake_ec2.start_calls, [["i-fcknat"]])
        self.assertEqual(
            fake_lambda.invocations[0]["Payload"]["action"],
            main.START_APP_AFTER_NAT_ACTION,
        )
        self.assertIn("NAT 준비가 끝나면", message)

    def test_start_worker_waits_for_nat_then_starts_app_asg(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running", ready=True)
        main.asg_client = FakeAsgClient(desired=0)
        main.s3_client = FakeS3Client(["인프라"])

        result = main.handle_internal_event(
            {"source": main.INTERNAL_EVENT_SOURCE, "action": main.START_APP_AFTER_NAT_ACTION}
        )

        self.assertEqual(main.asg_client.set_calls, [1])
        self.assertEqual(result["status"], "completed")

    def test_wait_for_nat_restarts_instance_that_is_stopped(self):
        main = import_main()
        fake_ec2 = FakeEc2Client(state="stopped", ready=True)
        main.ec2_client = fake_ec2
        main.time.sleep = lambda seconds: None

        instance_id = main.wait_for_fck_nat_ready()

        self.assertEqual(instance_id, "i-fcknat")
        self.assertEqual(fake_ec2.start_calls, [["i-fcknat"]])

    def test_start_worker_cancels_and_stops_nat_when_no_team_remains(self):
        main = import_main()
        fake_ec2 = FakeEc2Client(state="running", ready=True)
        main.ec2_client = fake_ec2
        main.asg_client = FakeAsgClient(desired=0)
        main.s3_client = FakeS3Client([])

        result = main.handle_internal_event(
            {"source": main.INTERNAL_EVENT_SOURCE, "action": main.START_APP_AFTER_NAT_ACTION}
        )

        self.assertEqual(main.asg_client.set_calls, [])
        self.assertEqual(fake_ec2.stop_calls, [["i-fcknat"]])
        self.assertEqual(result["status"], "cancelled")

    def test_stop_sequence_scales_app_down_before_dispatching_worker(self):
        main = import_main()
        fake_asg = FakeAsgClient(desired=1)
        fake_lambda = FakeLambdaClient()
        main.asg_client = fake_asg
        main.lambda_client = fake_lambda
        main.LAMBDA_FUNCTION_NAME = "SnoroseDevManagerBot"

        message = main.stop_dev_stack()

        self.assertEqual(fake_asg.set_calls, [0])
        self.assertEqual(
            fake_lambda.invocations[0]["Payload"]["action"],
            main.STOP_NAT_AFTER_APP_ACTION,
        )
        self.assertIn("앱 서버가 종료되면", message)

    def test_stop_worker_stops_nat_after_app_asg_is_empty(self):
        main = import_main()
        fake_ec2 = FakeEc2Client(state="running")
        main.ec2_client = fake_ec2
        main.asg_client = FakeAsgClient(desired=0, instances=[])
        main.s3_client = FakeS3Client([])

        result = main.handle_internal_event(
            {"source": main.INTERNAL_EVENT_SOURCE, "action": main.STOP_NAT_AFTER_APP_ACTION}
        )

        self.assertEqual(fake_ec2.stop_calls, [["i-fcknat"]])
        self.assertEqual(result["status"], "completed")

    def test_old_stop_worker_does_not_stop_nat_after_new_start(self):
        main = import_main()
        fake_ec2 = FakeEc2Client(state="running")
        main.ec2_client = fake_ec2
        main.asg_client = FakeAsgClient(desired=1)
        main.s3_client = FakeS3Client(["인프라"])

        result = main.handle_internal_event(
            {"source": main.INTERNAL_EVENT_SOURCE, "action": main.STOP_NAT_AFTER_APP_ACTION}
        )

        self.assertEqual(fake_ec2.stop_calls, [])
        self.assertEqual(result["status"], "cancelled")

    def test_status_reports_starting_while_nat_precedes_app(self):
        main = import_main()
        main.get_instance_state = lambda: "stopped"
        main.get_instance_status = lambda: "⚠️ 실행 중인 인스턴스가 없습니다."
        main.get_fck_nat_state = lambda: "pending"
        main.s3_client = FakeS3Client(["인프라"])

        message = main.handle_status_dev()

        self.assertIn("DEV 서버가 시작 중입니다", message)
        self.assertIn("fck-nat 시작 중", message)

    def test_status_reports_stopping_until_nat_is_stopped(self):
        main = import_main()
        main.get_instance_state = lambda: "stopped"
        main.get_instance_status = lambda: "⚠️ 실행 중인 인스턴스가 없습니다."
        main.get_fck_nat_state = lambda: "stopping"
        main.s3_client = FakeS3Client([])

        message = main.handle_status_dev()

        self.assertIn("DEV 서버가 중지 중입니다", message)
        self.assertIn("fck-nat 중지 중", message)


if __name__ == "__main__":
    unittest.main()
