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

앱이 실행 중이면 로컬 Redis의 읽기·쓰기·TTL 준비 상태도 조회합니다. Redis 검사 실패나 조회 불가는
ALB가 정상이어도 경고로 표시합니다. 앱이 꺼져 있을 때는 검사 때문에 서버를 켜지 않습니다.

## 로컬 Redis 상태와 종료 안내

인프라 [#31](https://github.com/snorose/snorose-infra/pull/31)의 SSM 문서·IAM을 먼저 적용한 뒤
이 봇을 배포합니다. `REDIS_READINESS_DOCUMENT`의 기본값은 `snorose-dev-redis-ready`입니다.
Redis를 설치한 AMI와 서버 [#943](https://github.com/snorose/Snorose-Server/pull/943)의 연결 전환은
인프라의 `terraform/envs/dev/LOCAL_REDIS.md` 순서를 따릅니다.

`status_dev`는 현재 ASG에 앱 인스턴스가 한 대일 때 고정 문서를 한 번 실행하고 최대 6번 결과를 조회합니다.
조회 사이 대기는 총 10초이며 AWS API 응답 시간은 별도입니다. 기존 Discord 비동기 응답을 사용합니다.
서비스가 active이고 기존 임시 키 읽기·쓰기·TTL 검사가 성공해야 정상입니다. Redis의 키나 명령 출력은
Discord로 전달하지 않으며, 검사 때문에 서비스를 시작·재시작하거나 배포를 실행하지 않습니다.

- 이전 AMI에 로컬 Redis 표지가 없으면 `미설정`으로 표시합니다. ElastiCache의 정상 여부를 추정하지 않습니다.
- IAM·문서·SSM 연결 오류와 시간 초과는 `확인 불가`로 처리합니다. 서버 교체 중 결과도 정상으로 재사용하지 않습니다.
- 이 검사는 EC2의 로컬 Redis 준비 상태이며 앱의 실제 연결 대상이나 로그인 성공을 증명하지 않습니다.
  로그인·갱신·로그아웃은 서버 CD의 smoke로 검증합니다.

마지막 팀이 사용을 종료하고 ASG를 0으로 변경하는 데 성공하면, 로컬 Redis 사용 시
로그인 유지·이메일 인증 상태가 초기화되므로 다시 로그인·인증해야 한다고 안내합니다.
다른 팀이 남아 있거나 배포 보호로 종료가 보류되면 이 안내를 보내지 않습니다.
같은 EC2의 앱 재배포는 Redis를 종료하지 않습니다.

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

기본 브랜치는 `develop`이며, 작업 브랜치에서 PR을 만들어 `develop`에 머지합니다. GitHub Actions는 `develop` push만 감지해 AWS dev 계정의 ECR에 이미지를 올리고 Lambda 함수를 업데이트합니다. `main`은 사용하지 않으며 배포를 실행하지 않습니다.

1. AWS에 Lambda 함수, ECR Repository, GitHub OIDC용 IAM Role을 미리 준비합니다.
2. GitHub Environment는 `DEV`만 사용합니다. 배포 워크플로의 환경도 `DEV`로 고정합니다.
3. 각 Environment variable에 ```AWS_ROLE_ARN```, ```AWS_REGION```, ```ECR_REPOSITORY_NAME```, ```LAMBDA_FUNCTION_NAME```, ```DISCORD_PUBLIC_KEY```, ```DISCORD_APPLICATION_ID```를 설정합니다.
   ```ACTIVE_TEAMS_BUCKET```, ```ACTIVE_TEAMS_KEY```, ```ASG_NAME```, ```FCK_NAT_NAME```, ```RDS_INSTANCE_IDENTIFIER```, ```REDIS_READINESS_DOCUMENT```는 생략하면 DEV 기본값이 쓰입니다.
   Lambda 환경변수는 워크플로가 맵 전체를 덮어쓰므로, 콘솔에서 직접 추가하면 다음 배포 때 사라집니다.
4. 각 Environment secret에 ```DISCORD_BOT_TOKEN```을 설정합니다.
5. `develop` 브랜치에 push하면 GitHub Actions가 이미지를 배포하고 Discord slash command를 등록합니다.
6. Lambda Function URL에 ```/interactions```를 붙여 [디스코드 개발자 포털](https://discord.com/developers/applications)의 General Information > Interactions Endpoint URL에 입력합니다.
7. 배포 뒤 Lambda 실행 역할에 아래 "필요 IAM 권한"의 정책을 부여합니다.

## 필요 IAM 권한

앱 서버는 EC2 인스턴스를 직접 start/stop하지 않고, ASG(`snorose-dev-application-asg`)의
desired capacity를 1↔0으로 조정합니다. 스팟 인스턴스는 one-time 요청이라
`StopInstances`가 불가능하고, ASG 소속 인스턴스는 stop해도 ASG가 다시 교체하기 때문입니다.

fck-nat는 non-HA On-Demand 단일 인스턴스로 구성하며 WARP 및 앱 서버와 함께 제어합니다.
`start_dev`는 RDS 시작을 요청한 직후 NAT를 켭니다. RDS 복구가 진행되는 동안 NAT → WARP 준비 검사를 수행하고,
네트워크와 RDS `available`을 모두 확인하면 앱 ASG desired capacity를 1로 올립니다.
`stop_dev`는 마지막 팀이 종료할 때 앱 ASG를 0으로 내리고, 앱이 모두 사라진 뒤 WARP·fck-nat와 RDS를 정지합니다.

RDS 시작·정지는 수분 이상 걸릴 수 있어 한 Lambda 안에서 계속 기다리지 않습니다.
`snorose-infra`의 EventBridge 규칙이 1분마다 `reconcile_rds`를 호출해 준비된 RDS 뒤의 앱 기동을 재개합니다.
RDS 준비 완료부터 후속 작업까지 통상 다음 1분 주기에 수초의 스케줄 지연이 더해질 수 있으며, 이후 기존 앱 배포 시간이 필요합니다.
기동 작업은 S3 `dev-manager/startup-lock.json`을 조건부로 작성해 중복 실행을 막습니다.
잠금은 작업 종료 시 해제하며, Lambda 강제 종료 시에는 11분 후 다시 획득할 수 있습니다.
이 잠금은 Lambda의 10분 실행 제한보다 길게 유지됩니다.
`status_dev`는 RDS 상태도 표시합니다. RDS가 `available`이 아니면 앱을 새로 시작하지 않습니다.

RDS는 7일간 정지하면 AWS가 자동으로 다시 시작합니다. 같은 규칙이 활성 팀이 없고 ASG desired=0이며
앱 인스턴스도 없는 것을 확인한 뒤 RDS를 다시 정지합니다. 팀 상태 파일 누락·손상·조회 실패 시에는
DB를 정지하지 않습니다. ASG가 수동으로 켜진 경우에도 DB를 유지합니다.
데이터·DB 식별자·접속 주소는 보존되며 스토리지와 백업 비용은 계속 발생합니다.
[AWS RDS 정지 문서](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_StopInstance.html)

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
| `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` | 활성 팀 상태 파일, 기동 잠금 및 배포 보호 기록 조회 |
| `ssm:SendCommand`, `ssm:GetCommandInvocation` | 전용 NAT/WARP/로컬 Redis 문서를 실행하고 결과 확인 |
| `lambda:InvokeFunction` | NAT 및 앱의 순차 시작·중지 작업을 비동기로 실행 |
| `rds:DescribeDBInstances`, `rds:StartDBInstance`, `rds:StopDBInstance` | dev RDS `snorose-dev` 한 개의 준비 확인·시작·정지 |

앱 헬스체크는 인스턴스에 직접 HTTP 요청을 보내지 않고 ALB Target Group의 판정을 읽습니다.
앱 서버가 private subnet에 있어 public IP가 없고, Lambda도 VPC 밖이라 직접 접근이 불가능합니다.

## 테스트 실행

```bash
python -m unittest discover -s tests -v
```


추가 환경변수는 배포 워크플로에서도 유지합니다.

| 변수 | DEV 기본값 |
| --- | --- |
| `WARP_NAME` | `WARPConnector-dev` |
| `NAT_READINESS_DOCUMENT` | `snorose-dev-fck-nat-ready` |
| `WARP_READINESS_DOCUMENT` | `snorose-dev-warp-ready` |
| `RDS_INSTANCE_IDENTIFIER` | `snorose-dev` |

인프라 PR #26의 SSM 문서와 IAM 권한을 먼저 적용한 뒤 이 봇을 배포해야 합니다.
SSM 진단 문서는 임의 명령 파라미터를 받지 않으며 다른 인스턴스에 대한 실행 권한도 부여하지 않습니다.
기존 NAT Gateway 상태에서는 이 봇의 새 시작 절차를 사용할 수 없습니다.

## 자동 배포와 종료 보호

`Snorose-Server`의 기존 머지 후 자동 기동을 유지합니다. CD는 S3의
`dev-manager/deployments/{run_id}-{attempt}.json`에 배포 보호 기록을 남긴 뒤 RDS를 시작합니다.
CD는 빌드와 RDS·NAT 준비를 동시에 진행하고, 모두 성공하면 앱 ASG 기동, CodeDeploy, 스모크 테스트를 진행합니다.
배포 작업 진입 시 보호 기록을 갱신하고 DB·네트워크 준비 상태를 다시 확인합니다.
봇으로 먼저 dev를 켤 필요는 없습니다. CD는 활성 팀 목록을 수정하지 않습니다.

ASG가 기동하면 CodeDeploy launch hook이 앱을 자동 배포하므로 ASG는 RDS와 병렬로 시작하지 않습니다.
DB가 준비되기 전에 앱이 시작되면 현재 약 2분의 서비스 검증 제한에 걸릴 수 있습니다. ASG까지 병렬화하려면
앱 시작 훅에 DB 대기를 먼저 추가해야 합니다. 봇의 준비 시간은 `max(RDS 복구, NAT/WARP 준비) + 앱 기동`입니다.
CD에서는 빌드 시간도 겹쳐 `max(빌드, RDS 복구, NAT 준비) + 앱 기동` 순서로 진행합니다.

유효한 배포 기록이 있으면 봇은 앱·네트워크·RDS를 종료하지 않습니다. 마지막 팀의 `/stop_dev`는
팀 등록을 유지한 채 보류하고, 배포 후 다시 실행하도록 안내합니다. 배포와 스모크 테스트가 끝나면
별도 정리 작업이 해당 실행의 기록만 삭제합니다. 중복 배포는 서로의 보호 기록을 삭제하지 않습니다.
실행 취소나 불확실한 CodeDeploy 상태로 기록이 남아도 3시간 후 효력이 사라집니다.
DEV 빌드는 최대 30분, 자원 준비는 25분, 배포는 90분, 스모크는 30분으로 제한합니다.
기록을 읽거나 해석하지 못하면 종료하지 않습니다.

빌드나 준비 단계가 실패해 앱 기동을 건너뛰면 CD는 해당 실행의 보호 기록을 삭제합니다.
다음 주기에서 팀·다른 배포·앱이 모두 없는지 확인한 뒤 RDS를 정지하고 WARP → NAT 순서로 정리합니다.
RDS에 별도의 15~30분 종료 유예를 두지 않습니다.

CD만으로 켠 환경은 기존처럼 배포 후에도 켜져 있습니다. 앱 ASG desired가 1 이상이면 RDS도 유지합니다.
사용 종료 시 `/stop_dev`로 환경을 정지하며, DB만 사용하는 작업은 `/start_dev`로 팀을 등록하세요.

## RDS 연동 배포 순서

1. 연결된 인프라 PR에서 봇의 dev RDS 제어·배포 기록 조회·기동 잠금 Get/Put 권한과 CD의 RDS 시작·배포 기록 생성/삭제 권한을 먼저 적용합니다.
2. 연결된 `Snorose-Server` PR을 배포해 RDS 자동 기동과 배포 종료 보호를 활성화합니다.
3. 이 봇 PR을 `develop`에 머지해 Lambda에 배포합니다. 이 시점까지 dev를 켜 둡니다.
4. 인프라 PR의 EventBridge 규칙·대상·Lambda 호출 권한을 적용합니다. 이전 봇에는 `reconcile_rds`가 없으므로 코드 배포보다 먼저 규칙을 활성화하지 않습니다.
5. 팀 사용이 끝난 뒤 `/stop_dev`와 `/start_dev`, 정지된 dev에 대한 자동 배포로 실제 RDS 정지·재기동을 검증합니다.

권한을 먼저 적용할 때는 인프라 저장소의 적용 안내를 따릅니다. 장시간 준비 작업을 재개하려면
EventBridge 규칙이 필요하므로 봇 코드만 배포한 상태를 최종 구성으로 두면 안 됩니다.

복구할 때는 먼저 EventBridge 규칙을 비활성화하고 RDS를 available로 만든 뒤 이전 봇 이미지를 배포합니다.
RDS 데이터베이스를 삭제하거나 재생성할 필요는 없습니다. 기동 작업의 중복은 S3 잠금으로 막지만,
팀·배포 상태 조회와 모든 AWS 시작·정지를 하나의 트랜잭션으로 묶지는 않으므로,
마지막 종료와 새 시작이 동시에 발생하면 RDS stopping 완료 후 다음 주기에 재시작될 수 있습니다.

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
