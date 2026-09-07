import os
import json
import time
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.parse import quote
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
FCK_NAT_NAME = os.environ.get("FCK_NAT_NAME", "snorose-dev-an2-fck-nat")
WARP_NAME = os.environ.get("WARP_NAME", "WARPConnector-dev")
NAT_READINESS_DOCUMENT = os.environ.get("NAT_READINESS_DOCUMENT", "snorose-dev-fck-nat-ready")
WARP_READINESS_DOCUMENT = os.environ.get("WARP_READINESS_DOCUMENT", "snorose-dev-warp-ready")
RDS_INSTANCE_IDENTIFIER = os.environ.get("RDS_INSTANCE_IDENTIFIER", "snorose-dev")
LAMBDA_FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")
ACTIVE_TEAMS_BUCKET = os.environ.get("ACTIVE_TEAMS_BUCKET", "snorose-dev-bucket")
ACTIVE_TEAMS_KEY = os.environ.get("ACTIVE_TEAMS_KEY", "dev-manager/active-teams.json")
DEPLOYMENTS_PREFIX = "dev-manager/deployments/"

# 인스턴스가 살아있다고 볼 EC2 상태. terminated/shutting-down은 제외해야 한다 —
# 태그 조회는 종료된 지 얼마 안 된 인스턴스까지 함께 돌려주기 때문이다.
LIVE_INSTANCE_STATES = ["pending", "running"]
FCK_NAT_INSTANCE_STATES = ["pending", "running", "stopping", "stopped"]

INTERNAL_EVENT_SOURCE = "snorose.dev-manager-bot"
START_APP_AFTER_NAT_ACTION = "start_app_after_nat"
STOP_NAT_AFTER_APP_ACTION = "stop_nat_after_app"
RECONCILE_RDS_ACTION = "reconcile_rds"
WAIT_INTERVAL_SECONDS = 5
NETWORK_READY_MAX_ATTEMPTS = 48
NETWORK_READY_TIMEOUT_SECONDS = 240
WARP_STOP_MAX_ATTEMPTS = 36
APP_STOP_MAX_ATTEMPTS = 30

app = Flask(__name__)
asgi_app = WsgiToAsgi(app)
web_handler = Mangum(asgi_app, lifespan="off")

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
lambda_client = None
ssm_client = None
rds_client = None


class ActiveTeamsStateError(Exception):
    pass


class AsgLookupError(Exception):
    pass


class FckNatLookupError(Exception):
    pass


class StartCancelled(Exception):
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


def get_lambda_client():
    global lambda_client

    if lambda_client is None:
        import boto3

        lambda_client = boto3.client("lambda", region_name=AWS_REGION)
    return lambda_client


def get_ssm_client():
    global ssm_client

    if ssm_client is None:
        import boto3
        from botocore.config import Config

        ssm_client = boto3.client(
            "ssm", region_name=AWS_REGION,
            config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}),
        )
    return ssm_client


def get_rds_client():
    global rds_client

    if rds_client is None:
        import boto3
        from botocore.config import Config

        rds_client = boto3.client(
            "rds", region_name=AWS_REGION,
            config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}),
        )
    return rds_client


def normalize_active_teams(teams):
    normalized = []
    for team in teams:
        if team and team not in normalized:
            normalized.append(team)
    return normalized


def is_missing_s3_object_error(error):
    error_code = getattr(error, "response", {}).get("Error", {}).get("Code")
    return (
        error_code in {"NoSuchKey", "NoSuchBucket"}
        or error.__class__.__name__ == "NoSuchKey"
    )


def load_active_teams(require_existing=False):
    try:
        response = get_s3_client().get_object(
            Bucket=ACTIVE_TEAMS_BUCKET,
            Key=ACTIVE_TEAMS_KEY,
        )
        body = response["Body"].read().decode("utf-8")
        if not body.strip():
            if require_existing:
                raise ValueError("활성 팀 상태 파일이 비어 있습니다.")
            return []
        payload = json.loads(body)
        if require_existing and (
            not isinstance(payload, dict)
            or not isinstance(payload.get("active_teams"), list)
            or any(not isinstance(team, str) or not team for team in payload["active_teams"])
        ):
            raise ValueError("활성 팀 상태 파일 형식이 올바르지 않습니다.")
        return normalize_active_teams(payload.get("active_teams", []))
    except Exception as e:
        if is_missing_s3_object_error(e) and not require_existing:
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


def has_active_deployment():
    # Each CD attempt owns one object; overlapping runs cannot release each other.
    # Lookup/parse errors propagate so an unknown deployment state never allows stop.
    continuation = {}
    while True:
        page = get_s3_client().list_objects_v2(
            Bucket=ACTIVE_TEAMS_BUCKET, Prefix=DEPLOYMENTS_PREFIX, **continuation,
        )
        for item in page.get("Contents", []):
            try:
                response = get_s3_client().get_object(Bucket=ACTIVE_TEAMS_BUCKET, Key=item["Key"])
            except Exception as error:
                if getattr(error, "response", {}).get("Error", {}).get("Code") == "NoSuchKey":
                    continue  # The corresponding CD attempt just finished.
                raise
            lease = json.loads(response["Body"].read())
            expires_at = lease["expires_at"]
            if type(expires_at) is not int:
                raise ValueError("배포 보호 기록의 만료 시각이 올바르지 않습니다.")
            if expires_at > datetime.now(timezone.utc).timestamp():
                return True
        if not page.get("IsTruncated"):
            return False
        continuation = {"ContinuationToken": page["NextContinuationToken"]}


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


def get_network_instance(name):
    """정지된 인스턴스도 포함하되 중복 태그가 있으면 제어 대상을 추측하지 않는다."""
    try:
        response = get_ec2_client().describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [name]},
                {"Name": "instance-state-name", "Values": FCK_NAT_INSTANCE_STATES},
            ]
        )
    except Exception as e:
        raise FckNatLookupError(f"{name} 조회 오류: {e}") from e

    instances = [
        instance
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    if len(instances) != 1:
        raise FckNatLookupError(f"인스턴스 '{name}'를 하나로 식별할 수 없습니다: {len(instances)}개")
    return instances[0]


def get_fck_nat_instance():
    return get_network_instance(FCK_NAT_NAME)


def get_warp_state():
    try:
        return get_network_instance(WARP_NAME)["State"]["Name"]
    except FckNatLookupError as e:
        return f"error: {e}"


def is_instance_ready(instance_id):
    try:
        statuses = get_ec2_client().describe_instance_status(
            InstanceIds=[instance_id],
            IncludeAllInstances=True,
        ).get("InstanceStatuses", [])
    except Exception as e:
        raise FckNatLookupError(f"EC2 상태 검사 오류: {e}") from e

    if not statuses:
        return False

    status = statuses[0]
    return (
        status["InstanceStatus"]["Status"] == "ok"
        and status["SystemStatus"]["Status"] == "ok"
    )


def start_fck_nat_instance():
    instance = get_fck_nat_instance()
    instance_id = instance["InstanceId"]
    state = instance["State"]["Name"]

    if state == "stopped":
        get_ec2_client().start_instances(InstanceIds=[instance_id])
        return "🚀 fck-nat를 시작 중입니다."
    if state == "stopping":
        return "⏳ fck-nat가 정지되는 대로 다시 시작합니다."
    if state == "pending":
        return "⏳ fck-nat가 이미 시작 중입니다."
    return "✅ fck-nat가 이미 실행 중입니다."


def stop_fck_nat_instance():
    instance = get_fck_nat_instance()
    instance_id = instance["InstanceId"]
    state = instance["State"]["Name"]

    if state == "stopped":
        return "✅ fck-nat가 이미 중지되어 있습니다."
    if state == "stopping":
        return "⏳ fck-nat가 이미 중지 중입니다."

    get_ec2_client().stop_instances(InstanceIds=[instance_id])
    return "🛑 fck-nat를 중지 중입니다."


def get_fck_nat_state():
    try:
        return get_fck_nat_instance()["State"]["Name"]
    except FckNatLookupError as e:
        return f"error: {e}"


def format_fck_nat_state(state):
    messages = {
        "running": "✅ fck-nat 실행 중",
        "pending": "⏳ fck-nat 시작 중",
        "stopping": "⏳ fck-nat 중지 중",
        "stopped": "🛑 fck-nat 중지됨",
    }
    return messages.get(state, f"⚠️ fck-nat 상태: {state}")


def invoke_background_action(action):
    if not LAMBDA_FUNCTION_NAME:
        raise RuntimeError("AWS_LAMBDA_FUNCTION_NAME 환경변수를 찾을 수 없습니다.")

    response = get_lambda_client().invoke(
        FunctionName=LAMBDA_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(
            {"source": INTERNAL_EVENT_SOURCE, "action": action}
        ).encode("utf-8"),
    )
    if response.get("StatusCode") != 202:
        raise RuntimeError(f"비동기 작업 호출 실패: status={response.get('StatusCode')}")


def get_rds_state():
    if not RDS_INSTANCE_IDENTIFIER:
        raise ValueError("RDS_INSTANCE_IDENTIFIER가 비어 있습니다.")
    instances = get_rds_client().describe_db_instances(
        DBInstanceIdentifier=RDS_INSTANCE_IDENTIFIER,
    )["DBInstances"]
    if len(instances) != 1:
        raise RuntimeError("DEV RDS 인스턴스를 하나로 확인할 수 없습니다.")
    return instances[0]["DBInstanceStatus"]


def change_rds_state(operation):
    try:
        response = getattr(get_rds_client(), operation)(
            DBInstanceIdentifier=RDS_INSTANCE_IDENTIFIER,
        )
        return response["DBInstance"]["DBInstanceStatus"]
    except Exception as error:
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        # Concurrent commands or an RDS transition can invalidate a preceding read.
        if code == "InvalidDBInstanceState":
            return get_rds_state()
        raise


def start_rds():
    state = get_rds_state()
    if state == "stopped":
        return change_rds_state("start_db_instance")
    # In particular, wait for stopping to finish before attempting another start.
    return state


def dev_is_idle():
    # Missing/unreadable state is not evidence that nobody is using the database.
    if load_active_teams(require_existing=True) or has_active_deployment():
        return False
    asg = get_asg()
    return asg["DesiredCapacity"] == 0 and not asg.get("Instances", [])


def stop_rds_if_idle():
    if not dev_is_idle():
        return {"status": "cancelled", "reason": "활성 팀 또는 앱 인스턴스 존재"}
    state = get_rds_state()
    if state == "available" and dev_is_idle():
        state = change_rds_state("stop_db_instance")
    return {"status": "completed" if state == "stopped" else "waiting", "rds": state}


def reconcile_rds():
    teams = load_active_teams(require_existing=True)
    asg = get_asg()
    if teams or asg["DesiredCapacity"] > 0 or has_active_deployment():
        state = start_rds()
        # A scheduled invocation resumes startup without waiting through RDS recovery
        # inside a single Lambda invocation. Keep manually started application ASGs up.
        if state == "available" and teams and asg["DesiredCapacity"] == 0:
            invoke_background_action(START_APP_AFTER_NAT_ACTION)
            return {"status": "scheduled", "rds": state}
        return {"status": "completed" if state == "available" else "waiting", "rds": state}
    # Includes the automatic RDS restart after seven stopped days. Never stop a DB
    # while any app instance remains, including a target still draining from the ALB.
    return stop_rds_if_idle()


def start_dev_stack():
    try:
        rds_state = start_rds()
        nat_message = start_fck_nat_instance()
        invoke_background_action(START_APP_AFTER_NAT_ACTION)
    except Exception as e:
        return f"❌ DEV 서버 시작 예약 실패: {e}"

    return (
        f"{nat_message}\nRDS: {rds_state}"
        "\n🚀 RDS와 네트워크 준비가 끝나면 앱 서버를 자동으로 시작합니다."
        " RDS 시작에는 수분 이상 걸릴 수 있습니다. `/status_dev`로 확인해주세요."
    )


def stop_dev_stack():
    if has_active_deployment():
        return "⏳ 자동 배포 중이므로 서버를 유지합니다. 배포 완료 후 `/stop_dev`를 다시 실행해주세요."
    app_message = stop_instance()
    if "실패" in app_message or app_message.startswith("❌"):
        return app_message

    try:
        invoke_background_action(STOP_NAT_AFTER_APP_ACTION)
    except Exception as e:
        return f"{app_message}\n❌ fck-nat 중지 예약 실패: {e}"

    return f"{app_message}\n🛑 앱 서버가 종료되면 WARP·fck-nat와 RDS를 중지합니다."


def wait_for_network_ready(name, document_name):
    deadline = time.monotonic() + NETWORK_READY_TIMEOUT_SECONDS
    command_id = None
    command_instance_id = None
    last_status = "EC2 준비 중"
    for attempt in range(NETWORK_READY_MAX_ATTEMPTS):
        if not load_active_teams():
            raise StartCancelled()
        if time.monotonic() >= deadline:
            break
        instance = get_network_instance(name)
        instance_id = instance["InstanceId"]
        state = instance["State"]["Name"]
        if instance_id != command_instance_id or state != "running":
            command_id = None
        if state == "stopped":
            get_ec2_client().start_instances(InstanceIds=[instance_id])
        elif state == "running" and is_instance_ready(instance_id):
            try:
                if command_id is None:
                    response = get_ssm_client().send_command(
                        InstanceIds=[instance_id], DocumentName=document_name,
                        DocumentVersion="$DEFAULT", TimeoutSeconds=30,
                    )
                    command_id = response["Command"]["CommandId"]
                    command_instance_id = instance_id
                result = get_ssm_client().get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id,
                )
                last_status = result["Status"]
                if last_status == "Success" and result.get("ResponseCode") == 0:
                    return instance_id
                if last_status not in {"Pending", "InProgress", "Delayed"}:
                    # cloud-init/SSM/터널 연결이 아직 끝나지 않았으면 고정 진단을 재시도한다.
                    command_id = None
            except Exception as e:
                code = getattr(e, "response", {}).get("Error", {}).get("Code")
                if code not in {"InvalidInstanceId", "InvocationDoesNotExist"}:
                    raise
                last_status = code
        if attempt < NETWORK_READY_MAX_ATTEMPTS - 1:
            time.sleep(WAIT_INTERVAL_SECONDS)
    raise TimeoutError(f"{name} 준비 상태 검사 시간 초과: {last_status}")


def wait_for_fck_nat_ready():
    return wait_for_network_ready(FCK_NAT_NAME, NAT_READINESS_DOCUMENT)


def wait_for_warp_stopped():
    for attempt in range(WARP_STOP_MAX_ATTEMPTS):
        if load_active_teams() or has_active_deployment() or get_asg()["DesiredCapacity"] > 0:
            return False
        instance = get_network_instance(WARP_NAME)
        state = instance["State"]["Name"]
        if state == "stopped":
            return True
        if state == "running":
            get_ec2_client().stop_instances(InstanceIds=[instance["InstanceId"]])
        if attempt < WARP_STOP_MAX_ATTEMPTS - 1:
            time.sleep(WAIT_INTERVAL_SECONDS)
    raise TimeoutError("WARP가 제한 시간 안에 정지되지 않았습니다. NAT는 유지합니다.")


def stop_network_after_app():
    if not wait_for_app_stopped() or not wait_for_warp_stopped():
        return {"status": "cancelled", "reason": "새 시작 요청 존재"}
    # WARP 정지 대기 중 들어온 start_dev 요청도 다시 확인한다.
    if load_active_teams() or has_active_deployment() or get_asg()["DesiredCapacity"] > 0:
        return {"status": "cancelled", "reason": "새 시작 요청 존재"}
    return {"status": "completed", "nat": stop_fck_nat_instance()}


def wait_for_app_stopped():
    for attempt in range(APP_STOP_MAX_ATTEMPTS):
        asg = get_asg()

        # 새 start_dev 요청이 들어오면 이전 stop 작업이 NAT를 내리지 않도록 취소한다.
        if asg["DesiredCapacity"] > 0 or has_active_deployment():
            return False
        if not asg.get("Instances", []):
            return True

        if attempt < APP_STOP_MAX_ATTEMPTS - 1:
            time.sleep(WAIT_INTERVAL_SECONDS)

    raise TimeoutError("앱 서버가 제한 시간 안에 ASG에서 제거되지 않았습니다.")


def handle_internal_event(event):
    action = event.get("action")
    if action == RECONCILE_RDS_ACTION:
        return {"action": action, **reconcile_rds()}
    if action == START_APP_AFTER_NAT_ACTION:
        try:
            if not load_active_teams():
                raise StartCancelled()
            rds_state = start_rds()
            if rds_state != "available":
                return {"action": action, "status": "waiting", "rds": rds_state}
            nat_instance_id = wait_for_fck_nat_ready()
            warp_instance_id = wait_for_network_ready(WARP_NAME, WARP_READINESS_DOCUMENT)
            # An old stop may have reached RDS while network readiness was pending.
            rds_state = get_rds_state()
            if rds_state != "available":
                return {"action": action, "status": "waiting", "rds": rds_state}
            if not load_active_teams():
                raise StartCancelled()
        except StartCancelled:
            # 취소된 시작 작업도 앱/WARP가 종료되기 전에 NAT를 정지하면 안 된다.
            # 시작 대기로 timeout을 소진했을 수 있어 종료 대기는 별도 호출에서 수행한다.
            invoke_background_action(STOP_NAT_AFTER_APP_ACTION)
            return {"action": action, "status": "cancelled", "reason": "종료 작업 예약"}
        app_message = start_instance()
        if "실패" in app_message or app_message.startswith("❌"):
            raise RuntimeError(app_message)
        return {
            "action": action, "status": "completed",
            "nat_instance_id": nat_instance_id, "warp_instance_id": warp_instance_id,
            "app": app_message,
        }
    if action == STOP_NAT_AFTER_APP_ACTION:
        result = stop_network_after_app()
        if result["status"] == "completed":
            result["database"] = stop_rds_if_idle()
        return {"action": action, **result}
    raise ValueError(f"지원하지 않는 내부 작업입니다: {action}")


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

    added_roles = [role for role in user_roles if role not in active_teams]
    if added_roles:
        active_teams = normalize_active_teams(active_teams + added_roles)
        try:
            save_active_teams(active_teams)
        except Exception as e:
            return f"❌ 활성 팀 상태 저장 오류: {str(e)}"

    start_msg = start_dev_stack()
    return f"{start_msg}\n테스트 중인 팀: {format_active_teams(active_teams)}"


def handle_stop_dev(user_roles):
    if not user_roles:
        return "❌ DEV 서버를 중지할 권한이 없습니다."

    try:
        active_teams = load_active_teams()
    except ActiveTeamsStateError as e:
        return f"❌ {str(e)}"

    removed_roles = [role for role in user_roles if role in active_teams]
    remaining_teams = [role for role in active_teams if role not in removed_roles]
    if not remaining_teams and has_active_deployment():
        return "⏳ 자동 배포 중이므로 종료하지 않았습니다. 배포 완료 후 `/stop_dev`를 다시 실행해주세요."
    if removed_roles:
        active_teams = remaining_teams
        try:
            save_active_teams(active_teams)
        except Exception as e:
            return f"❌ 활성 팀 상태 저장 오류: {str(e)}"
        stop_msg = f"🚫 {', '.join(removed_roles)} 팀이 테스트를 종료했습니다."
    else:
        stop_msg = "⚠️ 이미 해당 팀은 테스트 중이 아닙니다."

    if not active_teams:
        stop_msg += "\n" + stop_dev_stack()
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
    nat_state = get_fck_nat_state()
    warp_state = get_warp_state()
    try:
        rds_state = get_rds_state()
    except Exception:
        rds_state = "조회 실패"
    try:
        active_teams = load_active_teams()
    except ActiveTeamsStateError as e:
        return f"❌ {str(e)}"

    if instance_state == "running":
        app_health = check_app_health()
        is_initializing = "진행 중" in instance_status
        is_app_starting = bool(app_health) and "⏳" in app_health
        has_app_error = bool(app_health) and "❌" in app_health

        if has_app_error or nat_state != "running" or warp_state != "running" or rds_state != "available":
            prefix = "⚠️"
        elif is_initializing or is_app_starting:
            prefix = "⏳"
        else:
            prefix = "✅"

        msg = f"{prefix} DEV 서버가 실행 중입니다.\n{instance_status}"
        if app_health:
            msg += f"\n{app_health}"
        msg += f"\n{format_fck_nat_state(nat_state)}\nWARP: {warp_state}\nRDS: {rds_state}"
        msg += f"\n테스트 중인 팀: {format_active_teams(active_teams)}"
        return msg

    if instance_state == "stopped" and (active_teams or rds_state != "stopped" or nat_state in {"pending", "running", "stopping"} or warp_state in {"pending", "running", "stopping"}):
        if active_teams or has_active_deployment():
            return (
                "⏳ DEV 서버가 시작 중입니다. (RDS와 네트워크 준비 확인 후 앱 서버 시작)"
                f"\n{format_fck_nat_state(nat_state)}\nWARP: {warp_state}\nRDS: {rds_state}"
                f"\n테스트 중인 팀: {format_active_teams(active_teams)}"
            )
        return (
            "⏳ DEV 서버가 중지 중입니다."
            f"\n{format_fck_nat_state(nat_state)}\nWARP: {warp_state}\nRDS: {rds_state}"
            f"\n테스트 중인 팀: {format_active_teams(active_teams)}"
        )

    status_messages = {
        "stopped": "❌ DEV 서버가 중지되었습니다.",
        "pending": "⏳ DEV 서버가 시작 중입니다... (재배포 포함 8~10분)",
        "stopping": "⏳ DEV 서버가 중지 중입니다...",
    }

    message = status_messages.get(instance_state, f"⚠️ 서버 상태: {instance_state}")
    return f"{message}\n{format_fck_nat_state(nat_state)}\nWARP: {warp_state}\nRDS: {rds_state}"


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


def send_discord_response(method, path, payload, timeout):
    # Only the fixed Discord API host receives interaction tokens.
    req = urllib.request.Request(
        f"https://discord.com/api/v10/{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "SnoroseDevManagerBot/1.0"},
        method=method,
    )
    attempts = 3 if method == "PATCH" else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return
        except (HTTPError, URLError, TimeoutError) as error:
            status = getattr(error, "code", None)
            retryable = status is None or status == 429 or status >= 500
            if not retryable or attempt == attempts - 1:
                # URLs contain interaction tokens; do not include the original exception.
                raise RuntimeError(f"Discord {method} failed (status={status})") from None
            time.sleep(attempt + 1)


def respond_to_deferred_command(raw_request, command_name, user_roles):
    token = quote(raw_request["token"], safe="")
    interaction_id = quote(raw_request["id"], safe="")
    application_id = quote(raw_request["application_id"], safe="")
    # Acknowledge before boto3 initialization or any AWS calls. Returning type 5
    # after scheduling a Lambda invocation would still include SDK cold-start time.
    try:
        send_discord_response(
            "POST", f"interactions/{interaction_id}/{token}/callback", {"type": 5}, timeout=2,
        )
    except Exception as error:
        # Never execute a command when acknowledgement could not be confirmed.
        print(f"Discord acknowledgement failed: {command_name} ({type(error).__name__})")
        return jsonify({"type": 4, "data": {"content": "❌ 요청 접수를 확인하지 못해 작업을 실행하지 않았습니다. 잠시 후 다시 시도해주세요."}})
    print(f"Discord command acknowledged: {command_name}")
    try:
        if command_name == "start_dev":
            message_content = handle_start_dev(user_roles)
        elif command_name == "stop_dev":
            message_content = handle_stop_dev(user_roles)
        else:
            message_content = handle_status_dev()
    except Exception as error:
        print(f"Discord command failed: {command_name} ({type(error).__name__})")
        message_content = "❌ 명령 처리 중 오류가 발생했습니다. `/status_dev`로 현재 상태를 확인해주세요."
    try:
        send_discord_response(
            "PATCH", f"webhooks/{application_id}/{token}/messages/@original",
            {"content": message_content[:2000], "allowed_mentions": {"parse": []}}, timeout=5,
        )
        print(f"Discord command response updated: {command_name}")
    except RuntimeError as error:
        # The command has already run. Only retry the message, never the AWS action.
        print(f"Discord response update failed: {command_name} ({error})")
    # Discord already received the callback; do not send a second interaction response.
    return "", 202


@app.route("/interactions", methods=["POST"])
@app.route("/", methods=["POST"])
async def interactions():
    return interact(request.json)


@verify_key_decorator(DISCORD_PUBLIC_KEY)
def interact(raw_request):
    if raw_request["type"] == 1:  # PING
        response_data = {"type": 1}  # PONG
    else:
        command_name = raw_request["data"]["name"]
        member_roles = raw_request.get("member", {}).get("roles", [])
        user_roles = [
            role_name
            for role_name, role_id in ROLE_MAPPING.items()
            if role_id in member_roles
        ]
        print(f"Discord command received: {command_name}")
        if command_name == "hello":
            message_content = "DEV 관리자 업무 중입니다. version 1.0"
        elif command_name in {"start_dev", "stop_dev", "status_dev"}:
            return respond_to_deferred_command(raw_request, command_name, user_roles)
        else:
            message_content = "❌ 지원하지 않는 명령입니다."
        response_data = {"type": 4, "data": {"content": message_content}}
    return jsonify(response_data)


def handler(event, context):
    if isinstance(event, dict) and event.get("source") == INTERNAL_EVENT_SOURCE:
        return handle_internal_event(event)
    return web_handler(event, context)


if __name__ == "__main__":
    app.run(debug=True)
