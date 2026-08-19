import os
import json
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from mangum import Mangum
from asgiref.wsgi import WsgiToAsgi
from discord_interactions import verify_key_decorator

AWS_REGION = "ap-northeast-2"

# DEV 서버는 ASG(min 0 / max 1)로 운용한다. on/off는 desired capacity 1↔0으로 제어하며,
# 스팟 인스턴스라 EC2 Start/StopInstances는 쓸 수 없다.
# (one-time 스팟 요청은 stop 불가, ASG 소속 인스턴스는 stop해도 ASG가 교체한다)
ASG_NAME = os.environ.get("ASG_NAME", "snorose-dev-application-asg")

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")
ACTIVE_TEAMS_BUCKET = os.environ.get("ACTIVE_TEAMS_BUCKET", "snorose-dev-bucket")
ACTIVE_TEAMS_KEY = os.environ.get("ACTIVE_TEAMS_KEY", "dev-manager/active-teams.json")

# 인스턴스가 살아있다고 볼 EC2 상태. terminated/shutting-down은 제외해야 한다 —
# 태그 조회는 종료된 지 얼마 안 된 인스턴스까지 함께 돌려주기 때문이다.
LIVE_INSTANCE_STATES = ["pending", "running"]

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
asg_client = None
elbv2_client = None


class ActiveTeamsStateError(Exception):
    pass


class AsgLookupError(Exception):
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


def get_asg_client():
    global asg_client

    if asg_client is None:
        import boto3

        asg_client = boto3.client("autoscaling", region_name=AWS_REGION)
    return asg_client


def get_elbv2_client():
    global elbv2_client

    if elbv2_client is None:
        import boto3

        elbv2_client = boto3.client("elbv2", region_name=AWS_REGION)
    return elbv2_client


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


def get_asg():
    try:
        groups = get_asg_client().describe_auto_scaling_groups(
            AutoScalingGroupNames=[ASG_NAME]
        ).get("AutoScalingGroups", [])
    except Exception as e:
        raise AsgLookupError(f"ASG 조회 오류: {e}") from e

    if not groups:
        raise AsgLookupError(f"❌ ASG '{ASG_NAME}'를 찾을 수 없습니다.")
    return groups[0]


def get_active_instance_id():
    """ASG가 붙들고 있는 인스턴스 중 pending/running 상태인 것 하나를 반환한다.

    Name 태그로 찾지 않는다. describe_instances의 태그 필터는 종료된 인스턴스까지
    돌려주기 때문에, 스팟 회수 직후에 죽은 인스턴스를 붙잡는 사고가 난다.
    """
    try:
        asg = get_asg()
    except AsgLookupError:
        return None

    instance_ids = [i["InstanceId"] for i in asg.get("Instances", [])]
    if not instance_ids:
        return None

    try:
        response = get_ec2_client().describe_instances(
            InstanceIds=instance_ids,
            Filters=[{"Name": "instance-state-name", "Values": LIVE_INSTANCE_STATES}],
        )
    except Exception:
        return None

    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            return instance["InstanceId"]
    return None


# 인스턴스 상태 조회
def get_instance_state():
    try:
        asg = get_asg()
    except AsgLookupError as e:
        return str(e)

    instance_id = get_active_instance_id()
    if instance_id:
        try:
            response = get_ec2_client().describe_instances(InstanceIds=[instance_id])
            return response["Reservations"][0]["Instances"][0]["State"]["Name"]
        except Exception as e:
            return f"오류 발생: {str(e)}"

    lifecycle_states = [i.get("LifecycleState", "") for i in asg.get("Instances", [])]
    if any(state.startswith("Terminating") for state in lifecycle_states):
        return "stopping"

    return "pending" if asg["DesiredCapacity"] >= 1 else "stopped"


# 인스턴스 상태 검사 결과 조회
def get_instance_status():
    instance_id = get_active_instance_id()
    if not instance_id:
        return "⚠️ 실행 중인 인스턴스가 없습니다."

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

    start_msg = start_instance()

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


def get_target_group_arn():
    try:
        asg = get_asg()
    except AsgLookupError:
        return None

    target_group_arns = asg.get("TargetGroupARNs", [])
    return target_group_arns[0] if target_group_arns else None


def check_app_health():
    """ALB Target Group 헬스체크 결과로 앱 상태를 판단한다.

    앱 서버는 private subnet에 있어 public IP가 없고 Lambda도 VPC 밖이라,
    인스턴스로 직접 HTTP를 찌를 수 없다. TG가 이미 /health/check를 보고 있으므로
    그 판정을 그대로 읽는다.
    """
    target_group_arn = get_target_group_arn()
    if not target_group_arn:
        return None

    try:
        descriptions = get_elbv2_client().describe_target_health(
            TargetGroupArn=target_group_arn
        )["TargetHealthDescriptions"]
    except Exception as e:
        return f"❌ 애플리케이션 상태 확인 실패: {str(e)}"

    if not descriptions:
        return "⏳ ALB에 등록된 대상이 없습니다. (기동 또는 배포 진행 중)"

    states = [d["TargetHealth"]["State"] for d in descriptions]
    if "healthy" in states:
        return "✅ 애플리케이션 응답 정상"
    if "initial" in states:
        return "⏳ 애플리케이션 기동 중... (CodeDeploy 재배포 포함 8~10분)"

    detail = descriptions[0]["TargetHealth"].get("Description") or states[0]
    return f"❌ 애플리케이션 응답 이상 ({detail})"


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
        is_app_starting = bool(app_health) and "⏳" in app_health
        has_app_error = bool(app_health) and "❌" in app_health

        if has_app_error:
            prefix = "⚠️"
        elif is_initializing or is_app_starting:
            prefix = "⏳"
        else:
            prefix = "✅"

        msg = f"{prefix} DEV 서버가 실행 중입니다.\n{instance_status}"
        if app_health:
            msg += f"\n{app_health}"
        msg += f"\n테스트 중인 팀: {format_active_teams(active_teams)}"
        return msg

    status_messages = {
        "stopped": "❌ DEV 서버가 중지되었습니다.",
        "pending": "⏳ DEV 서버가 시작 중입니다... (재배포 포함 8~10분)",
        "stopping": "⏳ DEV 서버가 중지 중입니다...",
    }

    return status_messages.get(instance_state, f"⚠️ 서버 상태: {instance_state}")


def start_instance():
    try:
        asg = get_asg()
    except AsgLookupError as e:
        return str(e)

    if asg["DesiredCapacity"] >= 1:
        return "✅ 서버가 이미 실행 중입니다."

    try:
        get_asg_client().set_desired_capacity(
            AutoScalingGroupName=ASG_NAME,
            DesiredCapacity=1,
            HonorCooldown=False,
        )
    except Exception as e:
        return f"서버 시작 실패: {str(e)}"

    return "🚀 서버를 시작 중입니다... (재배포 포함 8~10분, `status_dev`로 확인하세요)"


def stop_instance():
    try:
        asg = get_asg()
    except AsgLookupError as e:
        return str(e)

    # min_size가 0이 아니면 desired를 0으로 내릴 수 없다.
    # Terraform에서 asg_min_size가 되돌아간 경우를 여기서 바로 드러낸다.
    if asg["MinSize"] > 0:
        return (
            f"서버 중지 실패: ASG min_size가 {asg['MinSize']}라 0으로 내릴 수 없습니다. "
            "(Terraform의 asg_min_size 설정을 확인해주세요)"
        )

    if asg["DesiredCapacity"] == 0:
        return "✅ 서버가 이미 중지되어 있습니다."

    try:
        get_asg_client().set_desired_capacity(
            AutoScalingGroupName=ASG_NAME,
            DesiredCapacity=0,
            HonorCooldown=False,
        )
    except Exception as e:
        return f"서버 중지 실패: {str(e)}"

    return "🛑 서버를 중지 중입니다..."


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
