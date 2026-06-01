import os
from pathlib import Path

import requests
import yaml


API_VERSION = "v9"
COMMANDS_FILE = Path(__file__).with_name("discord_commands.yaml")


def get_required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is required.")
    return value


def load_commands(path=COMMANDS_FILE):
    with path.open("r") as file:
        return yaml.safe_load(file)


def register_commands(token, application_id, commands):
    url = f"https://discord.com/api/{API_VERSION}/applications/{application_id}/commands"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

    for command in commands:
        response = requests.post(url, json=command, headers=headers)
        command_name = command["name"]
        print(f"Command {command_name} created: {response.status_code}")
        response.raise_for_status()


def main():
    token = get_required_env("DISCORD_BOT_TOKEN")
    application_id = get_required_env("DISCORD_APPLICATION_ID")
    register_commands(token, application_id, load_commands())


if __name__ == "__main__":
    main()
