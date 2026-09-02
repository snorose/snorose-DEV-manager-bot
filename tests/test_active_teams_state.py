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
os.environ.setdefault("ACTIVE_TEAMS_BUCKET", "snorose-bucket")
os.environ.setdefault("ACTIVE_TEAMS_KEY", "dev-manager/active-teams.json")


class FakeS3Client:
    class NoSuchKey(Exception):
        pass

    def __init__(self, body=None):
        self.body = body
        self.exceptions = type("Exceptions", (), {"NoSuchKey": self.NoSuchKey})
        self.put_calls = []

    def get_object(self, Bucket, Key):
        if self.body is None:
            raise self.NoSuchKey()
        return {"Body": io.BytesIO(self.body.encode())}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put_calls.append(
            {
                "Bucket": Bucket,
                "Key": Key,
                "Body": Body,
                "ContentType": ContentType,
            }
        )
        self.body = Body

    @property
    def active_teams(self):
        return json.loads(self.body)["active_teams"]


def import_main():
    sys.modules.pop("main", None)
    return importlib.import_module("main")


class ActiveTeamsStateTest(unittest.TestCase):
    def test_start_dev_persists_active_team_to_s3(self):
        main = import_main()
        fake_s3 = FakeS3Client()
        main.s3_client = fake_s3
        main.start_dev_stack = lambda: "🚀 DEV 환경을 시작 중입니다..."

        message = main.handle_start_dev(["인프라"])

        self.assertIn("테스트 중인 팀: 인프라", message)
        self.assertEqual(fake_s3.active_teams, ["인프라"])
        self.assertEqual(fake_s3.put_calls[-1]["Bucket"], "snorose-bucket")
        self.assertEqual(fake_s3.put_calls[-1]["Key"], "dev-manager/active-teams.json")
        self.assertEqual(fake_s3.put_calls[-1]["ContentType"], "application/json")

    def test_empty_s3_state_file_is_treated_as_no_active_teams(self):
        main = import_main()
        main.s3_client = FakeS3Client("")

        self.assertEqual(main.load_active_teams(), [])

    def test_stop_dev_removes_active_team_from_s3_and_stops_when_empty(self):
        main = import_main()
        fake_s3 = FakeS3Client(json.dumps({"active_teams": ["인프라"]}))
        main.s3_client = fake_s3
        stop_calls = []
        main.stop_dev_stack = lambda: stop_calls.append(True) or "🛑 DEV 환경을 중지 중입니다..."

        message = main.handle_stop_dev(["인프라"])

        self.assertIn("인프라 팀이 테스트를 종료했습니다.", message)
        self.assertIn("🛑 DEV 환경을 중지 중입니다...", message)
        self.assertEqual(fake_s3.active_teams, [])
        self.assertEqual(stop_calls, [True])

    def test_stop_dev_does_not_stop_instance_when_s3_state_read_fails(self):
        class BrokenS3Client:
            def get_object(self, Bucket, Key):
                raise RuntimeError("boom")

        main = import_main()
        main.s3_client = BrokenS3Client()
        stop_calls = []
        main.stop_dev_stack = lambda: stop_calls.append(True) or "🛑 DEV 환경을 중지 중입니다..."

        message = main.handle_stop_dev(["인프라"])

        self.assertIn("활성 팀 상태 조회 오류", message)
        self.assertEqual(stop_calls, [])

    def test_status_dev_reads_active_teams_from_s3(self):
        main = import_main()
        fake_s3 = FakeS3Client(json.dumps({"active_teams": ["인프라", "백엔드"]}))
        main.s3_client = fake_s3
        main.get_instance_state = lambda: "running"
        main.get_instance_status = lambda: "✅ 상태 검사 통과!"
        main.get_fck_nat_state = lambda: "running"
        main.check_app_health = lambda: "✅ 애플리케이션 응답 정상"

        message = main.handle_status_dev()

        self.assertIn("테스트 중인 팀: 인프라, 백엔드", message)


if __name__ == "__main__":
    unittest.main()
