import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "app"))

import main  # noqa: E402


class FakeSsmClient:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


class DiscordBotTokenTests(unittest.TestCase):
    def setUp(self):
        import boto3

        self.original_client = boto3.client
        self.original_token = main.DISCORD_BOT_TOKEN
        main.DISCORD_BOT_TOKEN = None

    def tearDown(self):
        import boto3

        boto3.client = self.original_client
        main.DISCORD_BOT_TOKEN = self.original_token

    def _install(self, fake):
        import boto3

        def client(service, **kwargs):
            self.assertEqual(service, "ssm")
            return fake

        boto3.client = client

    def test_reads_token_from_ssm_once(self):
        fake = FakeSsmClient(value="ssm-token")
        self._install(fake)

        self.assertEqual(main.discord_bot_token(), "ssm-token")
        self.assertEqual(main.discord_bot_token(), "ssm-token")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["Name"], main.DISCORD_BOT_TOKEN_PARAMETER)
        self.assertTrue(fake.calls[0]["WithDecryption"])

    def test_missing_parameter_is_reported_without_token_value(self):
        self._install(FakeSsmClient(error=RuntimeError("ParameterNotFound")))

        with self.assertRaises(RuntimeError) as raised:
            main.discord_bot_token()
        self.assertIn(main.DISCORD_BOT_TOKEN_PARAMETER, str(raised.exception))
        self.assertIsNone(main.DISCORD_BOT_TOKEN)

    def test_empty_parameter_is_rejected(self):
        self._install(FakeSsmClient(value=""))

        with self.assertRaises(RuntimeError):
            main.discord_bot_token()

    def test_runtime_does_not_read_token_from_environment(self):
        source = (Path(__file__).resolve().parents[1] / "src" / "app" / "main.py").read_text()
        self.assertNotIn('os.environ.get("DISCORD_BOT_TOKEN")', source)

    def test_deploy_workflow_does_not_write_lambda_environment(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy-lambda.yaml").read_text()
        self.assertNotIn("update-function-configuration", workflow)
        self.assertNotIn("DISCORD_BOT_TOKEN=$DISCORD_BOT_TOKEN", workflow)


if __name__ == "__main__":
    unittest.main()
