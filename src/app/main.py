import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from mangum import Mangum
from asgiref.wsgi import WsgiToAsgi
from discord_interactions import verify_key_decorator

AWS_REGION = "ap-northeast-2"
INSTANCE_NAME = "snorose-dev"

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")
DEV_SERVER_HEALTH_PORT = os.environ.get("DEV_SERVER_HEALTH_PORT", "8081")
DEV_SERVER_HEALTH_PATH = os.environ.get("DEV_SERVER_HEALTH_PATH", "/actuator/health")
ACTIVE_TEAMS_BUCKET = os.environ.get("ACTIVE_TEAMS_BUCKET", "snorose-bucket")
ACTIVE_TEAMS_KEY = os.environ.get("ACTIVE_TEAMS_KEY", "dev-manager/active-teams.json")

app = Flask(__name__)
asgi_app = WsgiToAsgi(app)
handler = Mangum(asgi_app, lifespan="off")

ROLE_MAPPING = {
    "프론트엔드": "1223647596728553602",
    "백엔드": "1223317890887974932",
    "인프라": "1317082060745211978",
    "운영기획팀": "1223317409587531950",
    "이벤트기획": "1344975452078346321",
    "회계": "1317081963282173954",
    "디자인팀": "1259752818936385556",
}

ec2_client = None
s3_client = None


class ActiveTeamsStateError(Exception):
    pass


def get_ec2_client():
    global ec2_client

    if ec2_client is None:
        import boto3

        ec2_client = boto3.client("ec2", region_name=AWS_REGION)
    return ec2_client


def get_s3_client():
    global s3_client

    if s3_client is None:
        import boto3

        s3_client = boto3.client("s3", region_name=AWS_REGION)
    return s3_client


def normalize_active_teams(teams):
    normalized = []
    for team in teams:
        if team and team not in normalized:
            normalized.append(team)
    return normalized


def is_missing_s3_object_error(error):
    error_code = getattr(error, "response", {}).get("Error", {}).get("Code")
    return error_code in {"NoSuchKey", "NoSuchBucket"} or error.__class__.__name__ == "NoSuchKey"


def load_active_teams():
    try:
        response = get_s3_client().get_object(
            Bucket=ACTIVE_TEAMS_BUCKET,
            Key=ACTIVE_TEAMS_KEY,
        )
        body = response["Body"].read().decode("utf-8")
        if not body.strip():
            return []
        payload = json.loads(body)
        return normalize_active_teams(payload.get("active_teams", []))
    except Exception as e:
        if is_missing_s3_object_error(e):
            return []
        raise ActiveTeamsStateError(f"활성 팀 상태 조회 오류: {e}") from e


def save_active_teams(teams):
    payload = {
        "active_teams": normalize_active_teams(teams),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    get_s3_client().put_object(
        Bucket=ACTIVE_TEAMS_BUCKET,
        Key=ACTIVE_TEAMS_KEY,
        Body=json.dumps(payload, ensure_ascii=False),
        ContentType="application/json",
    )


def format_active_teams(teams):
    return ", ".join(teams) if teams else "없음"


def get_instance_id_by_name(instance_name):
    try:
        response = get_ec2_client().describe_instances(
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
        response = get_ec2_client().describe_instances(InstanceIds=[instance_id])
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
        response = get_ec2_client().describe_instance_status(InstanceIds=[instance_id])
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
    if not user_roles:
        return "❌ DEV 서버를 시작할 권한이 없습니다."

    try:
        active_teams = load_active_teams()
    except ActiveTeamsStateError as e:
        return f"❌ {str(e)}"

    if not active_teams:
        start_msg = start_instance()
    else:
        start_msg = "✅ 서버가 이미 실행 중입니다."

    added_roles = [role for role in user_roles if role not in active_teams]
    if added_roles:
        active_teams = normalize_active_teams(active_teams + added_roles)
        try:
            save_active_teams(active_teams)
        except Exception as e:
            return f"❌ 활성 팀 상태 저장 오류: {str(e)}"
        return f"{start_msg}\n테스트 중인 팀: {format_active_teams(active_teams)}"
    return start_msg


def handle_stop_dev(user_roles):
    if not user_roles:
        return "❌ DEV 서버를 중지할 권한이 없습니다."

    try:
        active_teams = load_active_teams()
    except ActiveTeamsStateError as e:
        return f"❌ {str(e)}"

    removed_roles = [role for role in user_roles if role in active_teams]
    if removed_roles:
        active_teams = [role for role in active_teams if role not in removed_roles]
        try:
            save_active_teams(active_teams)
        except Exception as e:
            return f"❌ 활성 팀 상태 저장 오류: {str(e)}"
        stop_msg = f"🚫 {', '.join(removed_roles)} 팀이 테스트를 종료했습니다."
    else:
        stop_msg = "⚠️ 이미 해당 팀은 테스트 중이 아닙니다."

    if not active_teams:
        stop_msg += "\n" + stop_instance()
    return stop_msg


def get_instance_public_ip():
    instance_id = get_instance_id_by_name(INSTANCE_NAME)
    if not instance_id:
        return None
    try:
        response = get_ec2_client().describe_instances(InstanceIds=[instance_id])
        return response["Reservations"][0]["Instances"][0].get("PublicIpAddress")
    except Exception:
        return None


def check_app_health():
    public_ip = get_instance_public_ip()
    if not public_ip:
        return None
    url = f"http://{public_ip}:{DEV_SERVER_HEALTH_PORT}{DEV_SERVER_HEALTH_PATH}"
    try:
        req = urllib.request.urlopen(url, timeout=2)
        if req.status == 200:
            return "✅ 애플리케이션 응답 정상"
        return f"⚠️ 애플리케이션 응답 이상 (HTTP {req.status})"
    except urllib.error.URLError as e:
        return f"❌ 애플리케이션 네트워크 오류: {e.reason}"
    except Exception as e:
        return f"❌ 애플리케이션 상태 확인 실패: {str(e)}"


def handle_status_dev():
    instance_state = get_instance_state()
    instance_status = get_instance_status()
    try:
        active_teams = load_active_teams()
    except ActiveTeamsStateError as e:
        return f"❌ {str(e)}"

    if instance_state == "running":
        app_health = check_app_health()
        is_initializing = "진행 중" in instance_status
        has_app_error = app_health and "❌" in app_health

        prefix = "⚠️" if (is_initializing or has_app_error) else "✅"
        msg = f"{prefix} DEV 서버가 실행 중입니다.\n{instance_status}"
        if app_health:
            msg += f"\n{app_health}"
        msg += f"\n테스트 중인 팀: {format_active_teams(active_teams)}"
        return msg

    status_messages = {
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
        get_ec2_client().start_instances(InstanceIds=[instance_id])
        return "🚀 서버를 시작 중입니다... (잠시 후 `status_dev`로 확인하세요)"
    except Exception as e:
        return f"서버 시작 실패: {str(e)}"


def stop_instance():
    instance_id = get_instance_id_by_name(INSTANCE_NAME)
    if not instance_id:
        return "❌ 해당 이름의 인스턴스를 찾을 수 없습니다."

    try:
        get_ec2_client().stop_instances(InstanceIds=[instance_id])
        return "🛑 서버를 중지 중입니다..."
    except Exception as e:
        return f"서버 중지 실패: {str(e)}"

@app.route("/interactions", methods=["POST"])
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
