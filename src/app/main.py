import os
import boto3
from flask import Flask, jsonify, request
from mangum import Mangum
from asgiref.wsgi import WsgiToAsgi
from discord_interactions import verify_key_decorator

AWS_REGION = "ap-northeast-2"
INSTANCE_NAME = "snorose-dev"

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")

app = Flask(__name__)
asgi_app = WsgiToAsgi(app)
handler = Mangum(asgi_app)

ROLE_MAPPING = {
    "프론트엔드": "1223647596728553602",
    "백엔드": "1223317890887974932",
    "인프라": "1317082060745211978"
}

# DEV 서버 사용 중인 팀 목록
active_teams = set()
ec2_client = boto3.client("ec2", region_name=AWS_REGION)


def get_instance_id_by_name(instance_name):
    try:
        response = ec2_client.describe_instances(
            Filters=[{"Name": "tag:Name", "Values": [instance_name]}]
        )
        instances = response.get("Reservations", [])

        if not instances:
            return None

        return instances[0]["Instances"][0]["InstanceId"]

    except Exception as e:
        print(f"❌ 오류 발생: {e}")
        return None


# 인스턴스 상태 조회
def get_instance_state():
    instance_id = get_instance_id_by_name(INSTANCE_NAME)
    if not instance_id:
        return "❌ 해당 이름의 인스턴스를 찾을 수 없습니다."

    try:
        response = ec2_client.describe_instances(InstanceIds=[instance_id])
        state = response["Reservations"][0]["Instances"][0]["State"]["Name"]
        return state
    except Exception as e:
        return f"오류 발생: {str(e)}"


# 인스턴스 상태 검사 결과 조회
def get_instance_status():
    instance_id = get_instance_id_by_name(INSTANCE_NAME)
    if not instance_id:
        return "❌ 해당 이름의 인스턴스를 찾을 수 없습니다."

    try:
        response = ec2_client.describe_instance_status(InstanceIds=[instance_id])
        if not response["InstanceStatuses"]:
            return "⚠️ 상태 검사 정보를 가져올 수 없습니다."

        status = response["InstanceStatuses"][0]["InstanceStatus"]["Status"]
        system_status = response["InstanceStatuses"][0]["SystemStatus"]["Status"]

        if status == "initializing" or system_status == "initializing":
            return "⏳ 상태 검사 진행 중..."
        elif status == "ok" and system_status == "ok":
            return "✅ 상태 검사 통과!"
        else:
            return "❌ 상태 검사 실패!"

    except Exception as e:
        return f"오류 발생: {str(e)}"
    

def handle_start_dev(user_roles):
    global active_teams

    if not user_roles:
        return "❌ DEV 서버를 시작할 권한이 없습니다."

    if not active_teams:
        start_msg = start_instance()
    else:
        start_msg = "✅ 서버가 이미 실행 중입니다."

    added_roles = [role for role in user_roles if role not in active_teams]
    if added_roles:
        active_teams.update(added_roles)
        return f"{start_msg}\n테스트 중인 팀: {', '.join(active_teams)}"
    return start_msg


def handle_stop_dev(user_roles):
    global active_teams

    if not user_roles:
        return "❌ DEV 서버를 중지할 권한이 없습니다."

    removed_roles = [role for role in user_roles if role in active_teams]
    if removed_roles:
        for role in removed_roles:
            active_teams.remove(role)
        stop_msg = f"🚫 {', '.join(removed_roles)} 팀이 테스트를 종료했습니다."
    else:
        stop_msg = "⚠️ 이미 해당 팀은 테스트 중이 아닙니다."

    if not active_teams:
        stop_msg += "\n" + stop_instance()
    return stop_msg


def handle_status_dev():
    instance_state = get_instance_state()
    instance_status = get_instance_status()

    status_messages = {
        "running": f"✅ DEV 서버가 실행 중입니다.\n{instance_status}\n테스트 중인 팀: {', '.join(active_teams) if active_teams else '없음'}",
        "stopped": "❌ DEV 서버가 중지되었습니다.",
        "pending": "⏳ DEV 서버가 시작 중입니다...",
        "stopping": "⏳ DEV 서버가 중지 중입니다...",
    }

    return status_messages.get(instance_state, f"⚠️ 서버 상태: {instance_state}")


def start_instance():
    instance_id = get_instance_id_by_name(INSTANCE_NAME)
    if not instance_id:
        return "❌ 해당 이름의 인스턴스를 찾을 수 없습니다."

    try:
        ec2_client.start_instances(InstanceIds=[instance_id])
        return "🚀 서버를 시작 중입니다... (잠시 후 `status_dev`로 확인하세요)"
    except Exception as e:
        return f"서버 시작 실패: {str(e)}"


def stop_instance():
    instance_id = get_instance_id_by_name(INSTANCE_NAME)
    if not instance_id:
        return "❌ 해당 이름의 인스턴스를 찾을 수 없습니다."

    try:
        ec2_client.stop_instances(InstanceIds=[instance_id])
        return "🛑 서버를 중지 중입니다..."
    except Exception as e:
        return f"서버 중지 실패: {str(e)}"

# @app.route("/interactions", methods=["POST"])
@app.route("/", methods=["POST"])
async def interactions():
    print(f"👉 Request: {request.json}")
    raw_request = request.json
    return interact(raw_request)


@verify_key_decorator(DISCORD_PUBLIC_KEY)
def interact(raw_request):
    if raw_request["type"] == 1:  # PING
        response_data = {"type": 1}  # PONG
    else:
        data = raw_request["data"]
        command_name = data["name"]
        member_roles = raw_request["member"]["roles"]

        user_roles = [role_name for role_name, role_id in ROLE_MAPPING.items() if role_id in member_roles]

        if command_name == "hello":
            message_content = "DEV 관리자 업무 중입니다. version 0.1"

        elif command_name == "start_dev":
            message_content = handle_start_dev(user_roles)

        elif command_name == "stop_dev":
            message_content = handle_stop_dev(user_roles)

        elif command_name == "status_dev":
            message_content = handle_status_dev()

        response_data = {
            "type": 4,
            "data": {"content": message_content},
        }

    return jsonify(response_data)


if __name__ == "__main__":
    app.run(debug=True)
