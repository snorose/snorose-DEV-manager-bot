import importlib
import os
import pathlib
import sys
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "src" / "app"
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


class FakeAsgClient:
    def __init__(self, min_size=0, max_size=1, desired=0, instances=None, target_groups=None):
        self.group = {
            "AutoScalingGroupName": "snorose-dev-application-asg",
            "MinSize": min_size,
            "MaxSize": max_size,
            "DesiredCapacity": desired,
            "Instances": instances if instances is not None else [],
            "TargetGroupARNs": target_groups if target_groups is not None else [],
        }
        self.set_calls = []

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {"AutoScalingGroups": [self.group]}

    def set_desired_capacity(self, AutoScalingGroupName, DesiredCapacity, HonorCooldown):
        self.set_calls.append(DesiredCapacity)
        self.group["DesiredCapacity"] = DesiredCapacity


class FakeEc2Client:
    """describe_instances가 InstanceIds로 조회되고, 상태 필터가 적용되는지 검증한다."""

    def __init__(self, states):
        self.states = states  # {instance_id: state}
        self.describe_calls = []

    def describe_instances(self, InstanceIds=None, Filters=None):
        self.describe_calls.append({"InstanceIds": InstanceIds, "Filters": Filters})
        allowed = None
        for f in Filters or []:
            if f["Name"] == "instance-state-name":
                allowed = f["Values"]

        instances = []
        for instance_id in InstanceIds or []:
            state = self.states.get(instance_id)
            if state is None:
                continue
            if allowed is not None and state not in allowed:
                continue
            instances.append({"InstanceId": instance_id, "State": {"Name": state}})
        return {"Reservations": [{"Instances": instances}] if instances else []}


class FakeElbv2Client:
    def __init__(self, descriptions):
        self.descriptions = descriptions

    def describe_target_health(self, TargetGroupArn):
        return {"TargetHealthDescriptions": self.descriptions}


def import_main():
    sys.modules.pop("main", None)
    return importlib.import_module("main")


class AsgControlTest(unittest.TestCase):
    def test_start_scales_asg_to_one_when_stopped(self):
        main = import_main()
        fake_asg = FakeAsgClient(desired=0)
        main.asg_client = fake_asg

        message = main.start_instance()

        self.assertEqual(fake_asg.set_calls, [1])
        self.assertIn("서버를 시작 중입니다", message)

    def test_start_is_noop_when_already_running(self):
        main = import_main()
        fake_asg = FakeAsgClient(desired=1)
        main.asg_client = fake_asg

        message = main.start_instance()

        self.assertEqual(fake_asg.set_calls, [])
        self.assertIn("이미 실행 중입니다", message)

    def test_stop_scales_asg_to_zero(self):
        main = import_main()
        fake_asg = FakeAsgClient(min_size=0, desired=1)
        main.asg_client = fake_asg

        message = main.stop_instance()

        self.assertEqual(fake_asg.set_calls, [0])
        self.assertIn("서버를 중지 중입니다", message)

    def test_stop_reports_min_size_drift_instead_of_failing_silently(self):
        """Terraform asg_min_size가 1로 되돌아가면 desired 0이 불가능하다."""
        main = import_main()
        fake_asg = FakeAsgClient(min_size=1, desired=1)
        main.asg_client = fake_asg

        message = main.stop_instance()

        self.assertEqual(fake_asg.set_calls, [])
        self.assertIn("min_size", message)

    def test_terminated_instance_is_never_selected(self):
        """스팟 회수 직후 죽은 인스턴스를 붙잡지 않는지 확인한다."""
        main = import_main()
        main.asg_client = FakeAsgClient(
            desired=0, instances=[{"InstanceId": "i-dead", "LifecycleState": "Terminating"}]
        )
        main.ec2_client = FakeEc2Client({"i-dead": "terminated"})

        self.assertIsNone(main.get_active_instance_id())

    def test_running_instance_is_selected_from_asg_membership(self):
        main = import_main()
        main.asg_client = FakeAsgClient(
            desired=1, instances=[{"InstanceId": "i-live", "LifecycleState": "InService"}]
        )
        main.ec2_client = FakeEc2Client({"i-live": "running"})

        self.assertEqual(main.get_active_instance_id(), "i-live")

    def test_state_is_stopping_while_asg_terminates(self):
        main = import_main()
        main.asg_client = FakeAsgClient(
            desired=0, instances=[{"InstanceId": "i-dead", "LifecycleState": "Terminating:Wait"}]
        )
        main.ec2_client = FakeEc2Client({"i-dead": "shutting-down"})

        self.assertEqual(main.get_instance_state(), "stopping")

    def test_state_is_pending_while_scaling_up_before_instance_appears(self):
        main = import_main()
        main.asg_client = FakeAsgClient(desired=1, instances=[])
        main.ec2_client = FakeEc2Client({})

        self.assertEqual(main.get_instance_state(), "pending")

    def test_app_health_reads_target_group_instead_of_public_ip(self):
        main = import_main()
        main.asg_client = FakeAsgClient(desired=1, target_groups=["arn:aws:elasticloadbalancing:tg"])
        main.elbv2_client = FakeElbv2Client(
            [{"TargetHealth": {"State": "healthy"}}]
        )

        self.assertEqual(main.check_app_health(), "✅ 애플리케이션 응답 정상")

    def test_app_health_reports_startup_while_target_is_initial(self):
        main = import_main()
        main.asg_client = FakeAsgClient(desired=1, target_groups=["arn:aws:elasticloadbalancing:tg"])
        main.elbv2_client = FakeElbv2Client([{"TargetHealth": {"State": "initial"}}])

        self.assertIn("기동 중", main.check_app_health())

    def test_app_health_reports_unhealthy_reason(self):
        main = import_main()
        main.asg_client = FakeAsgClient(desired=1, target_groups=["arn:aws:elasticloadbalancing:tg"])
        main.elbv2_client = FakeElbv2Client(
            [{"TargetHealth": {"State": "unhealthy", "Description": "Health checks failed"}}]
        )

        message = main.check_app_health()
        self.assertIn("❌", message)
        self.assertIn("Health checks failed", message)


if __name__ == "__main__":
    unittest.main()
