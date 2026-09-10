# IAM

`lambda-execution-policy.json`은 Lambda 실행 역할
`snorose-dev-manager-bot-lambda-execution-role`의 인라인 정책
`dev-manager-bot-lambda-execution-policy` 내용입니다.

정책의 기준 정의는 `snorose-infra`의 `terraform/modules/iam` 및 dev 전용
`terraform/envs/dev/local_redis_status.tf`에 있습니다. 이 파일은 봇 저장소에서
필요 권한을 함께 검토하거나 IaC 반영 전 임시로 적용할 때 사용합니다.

```bash
export AWS_PROFILE=snorose-dev-sso

aws iam put-role-policy \
  --role-name snorose-dev-manager-bot-lambda-execution-role \
  --policy-name dev-manager-bot-lambda-execution-policy \
  --policy-document file://iam/lambda-execution-policy.json
```

EC2·ASG·ELB의 Describe 계열 API는 `Resource: "*"`입니다.
RDS의 `DescribeDBInstances`, `StartDBInstance`, `StopDBInstance`는
`arn:aws:rds:ap-northeast-2:621962613485:db:snorose-dev` 한 개로 제한합니다.
DB 삭제·변경·비밀번호 조회 권한은 추가하지 않습니다.
쓰기 권한은 DEV 앱 ASG와 `Name=snorose-dev-an2-fck-nat` / `Name=WARPConnector-dev`인 EC2 인스턴스로 한정되어 있습니다.
NAT 준비와 앱 종료를 Discord 응답과 분리하기 위해 봇 Lambda가 자기 자신을 비동기로
호출할 수 있는 권한도 포함합니다.

기존에 붙어 있던 `AmazonEC2FullAccess`는 사용하지 않습니다. 앱 서버는 ASG desired
capacity로 제어하고, `StartInstances` / `StopInstances`는 fck-nat 및 WARP 인스턴스에만 허용합니다.


준비 상태 검사에는 `snorose-dev-fck-nat-ready`와 `snorose-dev-warp-ready` 두 문서만
실행할 수 있는 `ssm:SendCommand` 및 `ssm:GetCommandInvocation`이 필요합니다.
임의 셸 명령을 받는 `AWS-RunShellScript` 실행 권한은 없습니다. Terraform 정책은
인스턴스 ARN으로 제한하며, 이 참조 JSON은 배포 전에 아직 모르는 인스턴스 ID를 Name 태그로 제한합니다.

로컬 Redis 상태에는 추가로 `snorose-dev-redis-ready` 문서와 `Name=snorose-dev`인 앱 EC2의
`ssm:SendCommand` 권한을 사용합니다. ASG 교체에 따라 EC2 ID가 바뀌므로 태그로 범위를 제한합니다.
전체 정책에서 허용되는 명령은 이 세 가지 고정 진단 문서뿐이며 임의 명령 파라미터는 없습니다.
Redis 진단은 서비스를 재시작하지 않고 임시 키의 읽기·쓰기·TTL을 검사한 뒤 삭제합니다.
인프라 #31의 문서·IAM을 먼저 적용한 뒤 봇을 배포합니다. 권한이나 문서가 없으면 `/status_dev`는
Redis를 정상으로 표시하지 않고 확인 불가로 안내합니다.
[AWS Run Command 태그 기반 권한](https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command-setting-up.html)

배포 종료 보호에는 `dev-manager/deployments/*`의 `s3:GetObject`와 해당 prefix의 `s3:ListBucket`이 필요합니다.
봇은 CD가 작성한 만료 시각을 읽기만 합니다. CD 역할의 dev RDS Describe/Start와 배포 기록 Put/Delete 권한은
`snorose-infra`의 dev 전용 `app_deploy_rds` 정책에서 관리합니다. 두 역할의 권한을 먼저 적용한 뒤 코드를 배포합니다.

기동 작업의 중복 방지에는 `dev-manager/startup-lock.json` 한 개의 Get/Put만 추가합니다.
`If-None-Match: *`로 최초 잠금을 생성하고, 만료된 잠금의 교체와 해제에는 `If-Match`를 사용합니다.
잠금 삭제나 버킷 전체 조회 권한은 필요하지 않습니다.
[AWS S3 조건부 쓰기](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
