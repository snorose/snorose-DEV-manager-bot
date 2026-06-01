import pathlib
import importlib
import json
import os
import sys
import time
import unittest

from nacl.signing import SigningKey


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "src" / "app"
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


def import_main_with_public_key(public_key):
    os.environ["DISCORD_PUBLIC_KEY"] = public_key
    sys.modules.pop("main", None)
    return importlib.import_module("main")


class RuntimeConfigTest(unittest.TestCase):
    def test_mangum_lifespan_is_disabled_for_wsgi_adapter(self):
        import main

        self.assertEqual(main.handler.lifespan, "off")

    def test_interactions_endpoint_handles_hello_command(self):
        signing_key = SigningKey.generate()
        main = import_main_with_public_key(signing_key.verify_key.encode().hex())
        payload = {
            "type": 2,
            "data": {"name": "hello"},
            "member": {"roles": []},
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = signing_key.sign(timestamp.encode() + body).signature.hex()

        response = main.app.test_client().post(
            "/interactions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-Ed25519": signature,
                "X-Signature-Timestamp": timestamp,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["content"], "DEV 관리자 업무 중입니다. version 0.1")

    def test_main_import_does_not_initialize_boto3(self):
        signing_key = SigningKey.generate()
        sys.modules.pop("boto3", None)

        import_main_with_public_key(signing_key.verify_key.encode().hex())

        self.assertNotIn("boto3", sys.modules)

    def test_register_commands_does_not_hardcode_discord_bot_token(self):
        register_commands = PROJECT_ROOT / "commands" / "register_commands.py"
        source = register_commands.read_text()

        self.assertNotIn("TOKEN =", source)
        self.assertNotIn("MTM0MjcyMzg4ODM2NjQ4OTY3MA", source)
        self.assertIn("DISCORD_BOT_TOKEN", source)

    def test_python_runtime_dependencies_are_pinned(self):
        requirements = PROJECT_ROOT / "src" / "requirements.txt"

        unpinned = [
            line
            for line in requirements.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#") and "==" not in line
        ]

        self.assertEqual(unpinned, [])


if __name__ == "__main__":
    unittest.main()
