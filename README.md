# snorose-DEV-manager-bot

## 설명

DEV 서버 비용을 줄이고자 도입된 DEV 관리자 봇 <br/>
[DEV 관리자 사용 가이드](https://www.notion.so/snorose/DEV-1a67ef0aa3bf8028bafff38536852086?pvs=4)

- Python 3.13 + Flask
- GitHub Actions로 Docker 이미지를 ECR에 push하고 Lambda 함수를 업데이트합니다.


## 명령어 소개

### 1️⃣ `hello`

DEV 관리자 봇의 상태를 묻는 명령어입니다.

### **2️⃣** `start_dev`

DEV 서버를 시작하는 명령어입니다.
이미 서버가 실행 중이더라도 꼭 입력해주세요!

### **3️⃣ `stop_dev`**

DEV 서버를 중지하는 명령어입니다.
타 팀이 테스트 진행 중이더라도 꼭 입력해주세요!

### 4️⃣ **`status_dev`**

DEV 서버의 상태를 조회하는 명령어입니다.

## 로컬 테스트 방법

1. ```pip install -r src/requirements.txt```와 ```pip install -r commands/requirements.txt```를 실행해 라이브러리를 설치합니다.
2. [Discord Developer Portal](https://discord.com/developers/applications)에서 봇을 생성하고 테스트할 서버에 초대합니다.
3. 생성한 디스코드 봇의 Application ID, Public Key, Token을 메모해 둡니다.
4. ```DISCORD_BOT_TOKEN```, ```DISCORD_APPLICATION_ID```, ```DISCORD_PUBLIC_KEY``` 환경변수를 메모한 값으로 설정합니다.
5. 로컬에서는 ```src/app/main.py``` 파일의 ```DISCORD_PUBLIC_KEY``` 값을 직접 바꾸지 말고 환경변수로 주입합니다.
6. [ngrok 설치 페이지](https://ngrok.com/downloads/windows)로 이동하여 운영 체제에 맞는 ngrok를 설치한 뒤 로그인을 진행합니다.
7. 로그인이 완료되면 Getting Started > Your Authtoken에서 Authtoken을 복사합니다.
8. ```ngrok config add-authtoken <Authtoken 입력>``` 명령어를 입력해 인증을 진행합니다.
9. root 경로에서 ```ngrok http 5000```을 입력해 5000번 포트로 포워딩된 https://000-000-0000.ngrok-free.app 형태의 엔드포인트를 [디스코드 개발자 포털](https://discord.com/developers/applications)에서 봇을 선택하여 들어간 뒤, General Information > Interactions Endpoint URL에 입력합니다. 이때 엔드포인트는 ```https://000-000-0000.ngrok-free.app/interactions``` 처럼 엔드포인트 뒤에 ```/interactions```를 추가해야 합니다.
10. root 경로에서 ```python commands/register_commands.py``` 명령어로 명령어를 생성합니다.
11. ```src/app``` 폴더로 이동해 ```python main.py``` 명령어로 해당 파일을 실행해 디스코드 봇을 로컬에서 테스트합니다.

## 배포 방법

GitHub Actions가 ```develop```, ```main``` 브랜치 push를 감지해 Docker 이미지를 빌드하고 ECR에 push한 뒤 Lambda 함수 이미지를 업데이트합니다.

1. AWS에 Lambda 함수, ECR Repository, GitHub OIDC용 IAM Role을 미리 준비합니다.
2. GitHub Environment를 설정합니다. ```develop``` 브랜치는 ```DEV```, ```main``` 브랜치는 ```PROD``` Environment를 사용합니다.
3. 각 Environment variable에 ```AWS_ROLE_ARN```, ```AWS_REGION```, ```ECR_REPOSITORY_NAME```, ```LAMBDA_FUNCTION_NAME```, ```DISCORD_PUBLIC_KEY```, ```DISCORD_APPLICATION_ID```를 설정합니다.
   ```ACTIVE_TEAMS_BUCKET```, ```ACTIVE_TEAMS_KEY```, ```ASG_NAME```, ```FCK_NAT_NAME```은 생략하면 DEV 기본값이 쓰입니다.
   Lambda 환경변수는 워크플로가 맵 전체를 덮어쓰므로, 콘솔에서 직접 추가하면 다음 배포 때 사라집니다.
4. 각 Environment secret에 ```DISCORD_BOT_TOKEN```을 설정합니다.
5. ```develop``` 또는 ```main``` 브랜치에 push하면 GitHub Actions가 이미지를 배포하고 Discord slash command를 등록합니다.
6. Lambda Function URL에 ```/interactions```를 붙여 [디스코드 개발자 포털](https://discord.com/developers/applications)의 General Information > Interactions Endpoint URL에 입력합니다.
7. 배포 뒤 Lambda 실행 역할에 아래 "필요 IAM 권한"의 정책을 부여합니다.

## 필요 IAM 권한

앱 서버는 EC2 인스턴스를 직접 start/stop하지 않고, ASG(`snorose-dev-application-asg`)의
desired capacity를 1↔0으로 조정합니다. 스팟 인스턴스는 one-time 요청이라
`StopInstances`가 불가능하고, ASG 소속 인스턴스는 stop해도 ASG가 다시 교체하기 때문입니다.

fck-nat는 non-HA On-Demand 단일 인스턴스로 구성하며 WARP 및 앱 서버와 함께 제어합니다.
`start_dev`는 NAT 준비 확인 → WARP 준비 확인 → 앱 ASG desired capacity 1 순서로 실행합니다.
`stop_dev`는 앱 ASG가 비워지고 WARP가 정지된 뒤 fck-nat를 정지합니다.

준비 확인은 EC2 상태 검사와 인프라 PR #26이 생성하는 전용 SSM 문서를 사용합니다.
NAT는 설정 실행 완료, ENI, forwarding, MASQUERADE 및 EIP를 통한 HTTPS 통신을 확인합니다.
WARP는 서비스, 터널 연결 및 Private-1의 HTTPS 통신을 확인합니다. 검사에 실패하면 앱을
시작하지 않습니다. fck-nat 서비스는 oneshot이므로 `is-active` 결과만으로 판단하지 않습니다.

NAT와 WARP는 각각 최대 240초 동안 준비 상태를 확인하며 Lambda timeout은 600초입니다.
SSM 결과는 eventual consistency를 고려해 재조회합니다. 실패 원인은 Lambda 로그와
SSM Run Command 실행 기록에서 확인합니다. 이 검사는 시작 시 수행하며 상시 알림은 없습니다.
시작 작업이 취소되어도 앱/WARP 종료를 기다린 뒤 NAT를 정지합니다. 새 시작 요청을 감지하면
종료 작업을 취소하지만 S3 팀 상태와 EC2 제어 사이에 분산 잠금은 없습니다.

DEV가 정지되어 있는 동안 WARP를 통한 DB 접근도 중단됩니다. 앱 외 DEV 작업도 해당 팀을
활성 상태로 유지한 뒤 수행하세요. WARP와 NAT 인스턴스의 Name 태그는 각각 하나여야 하며
중복되면 제어 대상을 임의로 선택하지 않습니다.
대기 작업은 같은 Lambda를 비동기로 호출해 처리하므로 Discord interaction 응답을 막지 않습니다.
Lambda timeout은 배포 워크플로에서 600초로 설정합니다.

Lambda 실행 역할에 필요한 권한은 `iam/lambda-execution-policy.json`에 정리되어 있습니다.

| 권한 | 용도 |
|---|---|
| `autoscaling:SetDesiredCapacity` | `start_dev` / `stop_dev` |
| `ec2:StartInstances`, `ec2:StopInstances` | fck-nat 및 WARP 시작 / 중지 |
| `autoscaling:DescribeAutoScalingGroups` | 인스턴스 목록, min/desired, Target Group ARN 조회 |
| `autoscaling:DescribeScalingActivities` | 스케일링 실패 원인 조회 |
| `ec2:DescribeInstances`, `ec2:DescribeInstanceStatus` | 인스턴스 상태 검사 |
| `elasticloadbalancing:DescribeTargetHealth` | `status_dev`의 앱 헬스체크 |
| `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` | 활성 팀 상태 파일 |
| `ssm:SendCommand`, `ssm:GetCommandInvocation` | 전용 NAT/WARP 문서를 실행하고 결과 확인 |
| `lambda:InvokeFunction` | NAT 및 앱의 순차 시작·중지 작업을 비동기로 실행 |

앱 헬스체크는 인스턴스에 직접 HTTP 요청을 보내지 않고 ALB Target Group의 판정을 읽습니다.
앱 서버가 private subnet에 있어 public IP가 없고, Lambda도 VPC 밖이라 직접 접근이 불가능합니다.

## 테스트 실행

```bash
python tests/test_active_teams_state.py
python tests/test_asg_control.py
python tests/test_fck_nat_control.py
python tests/test_runtime_config.py
```


추가 환경변수는 배포 워크플로에서도 유지합니다.

| 변수 | DEV 기본값 |
| --- | --- |
| `WARP_NAME` | `WARPConnector-dev` |
| `NAT_READINESS_DOCUMENT` | `snorose-dev-fck-nat-ready` |
| `WARP_READINESS_DOCUMENT` | `snorose-dev-warp-ready` |

인프라 PR #26의 SSM 문서와 IAM 권한을 먼저 적용한 뒤 이 봇을 배포해야 합니다.
SSM 진단 문서는 임의 명령 파라미터를 받지 않으며 다른 인스턴스에 대한 실행 권한도 부여하지 않습니다.
기존 NAT Gateway 상태에서는 이 봇의 새 시작 절차를 사용할 수 없습니다.

## Discord 응답 시간

Discord는 명령 접수 후 3초 이내의 최초 응답을 요구합니다.
`start_dev`, `stop_dev`, `status_dev`는 AWS SDK 초기화나 AWS 호출 전에
Discord 콜백 API에 `type: 5`(처리 중)를 전송합니다. 기존 Lambda 실행에서 작업을
수행한 뒤 원래 응답을 수정하고, 수신 HTTP 요청에는 빈 `202`를 반환합니다.
`hello`와 PING은 기존처럼 즉시 응답합니다.

접수 확인에 실패하면 AWS 작업을 실행하지 않습니다. 결과 전송의 일시적 오류는
최대 3회까지 재시도하며, 이 과정에서 AWS 작업을 다시 실행하지 않습니다.
콜드스타트 자체가 3초를 넘는 경우까지 보장하지는 않으므로, 그 경우에는
CloudWatch의 Init Duration과 명령 접수 로그를 별도로 확인해야 합니다.

```bash
python tests/test_deferred_interactions.py
```
