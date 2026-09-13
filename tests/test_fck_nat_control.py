import importlib
import io
import json
import os
import pathlib
import sys
import unittest
import time
import hashlib
import threading
from types import SimpleNamespace
from botocore.exceptions import ClientError


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "src" / "app"
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


class FakeEc2Client:
    def __init__(self, state="stopped", ready=True):
        self.state = state
        self.warp_state = "stopped"
        self.events = []
        self.ready = ready
        self.start_calls = []
        self.stop_calls = []

    def describe_instances(self, Filters):
        is_warp = Filters[0]["Values"] == ["WARPConnector-dev"]
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-warp" if is_warp else "i-fcknat",
                            "State": {"Name": self.warp_state if is_warp else self.state},
                        }
                    ]
                }
            ]
        }

    def describe_instance_status(self, InstanceIds, IncludeAllInstances):
        state = self.warp_state if InstanceIds == ["i-warp"] else self.state
        if state != "running" or not self.ready:
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
        self.events.append(("start", InstanceIds[0]))
        if InstanceIds == ["i-warp"]:
            self.warp_state = "running"
        else:
            self.state = "running"

    def stop_instances(self, InstanceIds):
        self.stop_calls.append(InstanceIds)
        self.events.append(("stop", InstanceIds[0]))
        if InstanceIds == ["i-warp"]:
            self.warp_state = "stopped"
        else:
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
        self.objects = {}
        self.guard = threading.Lock()

    def get_object(self, Bucket, Key):
        if Key == "dev-manager/startup-lock.json":
            with self.guard:
                if Key not in self.objects:
                    raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
                body, etag = self.objects[Key]
            return {"Body": io.BytesIO(body), "ETag": etag}
        body = json.dumps({"active_teams": self.teams}).encode()
        return {"Body": io.BytesIO(body)}

    def list_objects_v2(self, **kwargs):
        return {"Contents": []}

    def put_object(self, **kwargs):
        if kwargs["Key"] == "dev-manager/startup-lock.json":
            with self.guard:
                current = self.objects.get(kwargs["Key"])
                if (kwargs.get("IfNoneMatch") == "*" and current is not None) or (
                    "IfMatch" in kwargs and (current is None or current[1] != kwargs["IfMatch"])
                ):
                    raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
                body = kwargs["Body"].encode()
                etag = hashlib.sha256(body).hexdigest()
                self.objects[kwargs["Key"]] = (body, etag)
                return {"ETag": etag}
        self.teams = json.loads(kwargs["Body"])["active_teams"]


class FakeRdsClient:
    def __init__(self, state="available"):
        self.state = state
        self.start_calls = []
        self.stop_calls = []

    def describe_db_instances(self, DBInstanceIdentifier):
        return {"DBInstances": [{"DBInstanceStatus": self.state}]}

    def start_db_instance(self, DBInstanceIdentifier):
        self.start_calls.append(DBInstanceIdentifier)
        self.state = "starting"
        return {"DBInstance": {"DBInstanceStatus": self.state}}

    def stop_db_instance(self, DBInstanceIdentifier):
        self.stop_calls.append(DBInstanceIdentifier)
        self.state = "stopping"
        return {"DBInstance": {"DBInstanceStatus": self.state}}


class FakeSsmClient:
    def __init__(self, status="Success"):
        self.status = status
        self.calls = []

    def send_command(self, **kwargs):
        self.calls.append(kwargs)
        return {"Command": {"CommandId": str(len(self.calls))}}

    def get_command_invocation(self, **kwargs):
        return {"Status": self.status, "ResponseCode": 0 if self.status == "Success" else 1}


def import_main():
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    main.ssm_client = FakeSsmClient()
    main.lambda_client = FakeLambdaClient()
    main.LAMBDA_FUNCTION_NAME = "SnoroseDevManagerBot"
    main.s3_client = FakeS3Client(["인프라"])
    main.ec2_client = FakeEc2Client()
    main.asg_client = FakeAsgClient()
    main.rds_client = FakeRdsClient()
    main.time = SimpleNamespace(sleep=lambda seconds: None, monotonic=time.monotonic)
    return main


class FckNatControlTest(unittest.TestCase):
    def test_ec2_checks_alone_do_not_start_warp_or_app(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running")
        main.ssm_client = FakeSsmClient(status="Failed")
        main.NETWORK_READY_MAX_ATTEMPTS = 2
        with self.assertRaises(TimeoutError):
            main.handle_internal_event({"action": main.START_APP_AFTER_NAT_ACTION})
        self.assertEqual(main.ec2_client.start_calls, [])
        self.assertEqual(main.asg_client.set_calls, [])

    def test_ssm_pending_is_polled_without_duplicate_commands(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running")
        results = iter([{"Status": "InProgress"}, {"Status": "Success", "ResponseCode": 0}])
        main.ssm_client.get_command_invocation = lambda **kwargs: next(results)
        self.assertEqual(main.wait_for_fck_nat_ready(), "i-fcknat")
        self.assertEqual(len(main.ssm_client.calls), 1)

    def test_ssm_eventual_consistency_reuses_command(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running")
        from botocore.exceptions import ClientError
        error = ClientError({"Error": {"Code": "InvocationDoesNotExist"}}, "GetCommandInvocation")
        results = iter([error, {"Status": "Success", "ResponseCode": 0}])
        def invoke(**kwargs):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result
        main.ssm_client.get_command_invocation = invoke
        self.assertEqual(main.wait_for_fck_nat_ready(), "i-fcknat")
        self.assertEqual(len(main.ssm_client.calls), 1)

    def test_ssm_access_denied_fails_without_starting_app(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running")
        from botocore.exceptions import ClientError
        def denied(**kwargs):
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "SendCommand")
        main.ssm_client.send_command = denied
        with self.assertRaises(ClientError):
            main.handle_internal_event({"action": main.START_APP_AFTER_NAT_ACTION})
        self.assertEqual(main.asg_client.set_calls, [])

    def test_disconnected_warp_prevents_app_start(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running")
        main.NETWORK_READY_MAX_ATTEMPTS = 3
        def result(CommandId, InstanceId):
            return {"Status": "Success" if InstanceId == "i-fcknat" else "Failed", "ResponseCode": 0}
        main.ssm_client.get_command_invocation = result
        with self.assertRaises(TimeoutError):
            main.handle_internal_event({"action": main.START_APP_AFTER_NAT_ACTION})
        self.assertEqual(main.asg_client.set_calls, [])

    def test_cancelled_start_does_not_stop_nat_while_app_is_terminating(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running")
        main.s3_client = FakeS3Client([])
        main.asg_client = FakeAsgClient(instances=[{"InstanceId": "i-app", "LifecycleState": "Terminating"}])
        main.APP_STOP_MAX_ATTEMPTS = 1
        result = main.handle_internal_event({"action": main.START_APP_AFTER_NAT_ACTION})
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(main.lambda_client.invocations[0]["Payload"]["action"], main.STOP_NAT_AFTER_APP_ACTION)
        with self.assertRaises(TimeoutError):
            main.handle_internal_event({"action": main.STOP_NAT_AFTER_APP_ACTION})
        self.assertEqual(main.ec2_client.stop_calls, [])

    def test_warp_stop_timeout_keeps_nat_running(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running")
        main.ec2_client.warp_state = "stopping"
        main.s3_client = FakeS3Client([])
        main.WARP_STOP_MAX_ATTEMPTS = 1
        with self.assertRaises(TimeoutError):
            main.handle_internal_event({"action": main.STOP_NAT_AFTER_APP_ACTION})
        self.assertEqual(main.ec2_client.stop_calls, [])

    def test_new_start_during_warp_stop_keeps_nat_running(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running")
        main.ec2_client.warp_state = "stopping"
        main.s3_client = FakeS3Client([])
        main.time.sleep = lambda seconds: setattr(main.s3_client, "teams", ["인프라"])
        result = main.handle_internal_event({"action": main.STOP_NAT_AFTER_APP_ACTION})
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(main.ec2_client.stop_calls, [])

    def test_duplicate_name_fails_closed(self):
        main = import_main()
        main.ec2_client.describe_instances = lambda **kwargs: {
            "Reservations": [{"Instances": [{"InstanceId": "i-a"}, {"InstanceId": "i-b"}]}]
        }
        with self.assertRaises(main.FckNatLookupError):
            main.get_fck_nat_instance()
        self.assertEqual(main.ec2_client.start_calls, [])

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
        self.assertIn("RDS와 네트워크 준비가 끝나면", message)

    def test_start_worker_waits_for_nat_then_starts_app_asg(self):
        main = import_main()
        main.ec2_client = FakeEc2Client(state="running", ready=True)
        main.asg_client = FakeAsgClient(desired=0)
        main.s3_client = FakeS3Client(["인프라"])

        result = main.handle_internal_event(
            {"source": main.INTERNAL_EVENT_SOURCE, "action": main.START_APP_AFTER_NAT_ACTION}
        )

        self.assertEqual(main.asg_client.set_calls, [1])
        self.assertEqual(main.ec2_client.start_calls, [["i-warp"]])
        self.assertEqual([c["DocumentName"] for c in main.ssm_client.calls],
                         [main.NAT_READINESS_DOCUMENT, main.WARP_READINESS_DOCUMENT])
        self.assertEqual(result["status"], "completed")

    def test_wait_for_nat_restarts_instance_that_is_stopped(self):
        main = import_main()
        fake_ec2 = FakeEc2Client(state="stopped", ready=True)
        main.ec2_client = fake_ec2
        main.time.sleep = lambda seconds: None

        instance_id = main.wait_for_fck_nat_ready()

        self.assertEqual(instance_id, "i-fcknat")
        self.assertEqual(fake_ec2.start_calls, [["i-fcknat"]])

    def test_start_worker_cancels_and_schedules_ordered_stop_when_no_team_remains(self):
        main = import_main()
        fake_ec2 = FakeEc2Client(state="running", ready=True)
        main.ec2_client = fake_ec2
        main.asg_client = FakeAsgClient(desired=0)
        main.s3_client = FakeS3Client([])

        result = main.handle_internal_event(
            {"source": main.INTERNAL_EVENT_SOURCE, "action": main.START_APP_AFTER_NAT_ACTION}
        )

        self.assertEqual(main.asg_client.set_calls, [])
        self.assertEqual(fake_ec2.stop_calls, [])
        self.assertEqual(main.lambda_client.invocations[0]["Payload"]["action"], main.STOP_NAT_AFTER_APP_ACTION)
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
        fake_ec2.warp_state = "running"
        main.asg_client = FakeAsgClient(desired=0, instances=[])
        main.s3_client = FakeS3Client([])

        result = main.handle_internal_event(
            {"source": main.INTERNAL_EVENT_SOURCE, "action": main.STOP_NAT_AFTER_APP_ACTION}
        )

        self.assertEqual(fake_ec2.stop_calls, [["i-warp"], ["i-fcknat"]])
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
