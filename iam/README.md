# IAM

`lambda-execution-policy.json`은 Lambda 실행 역할
`snorose-dev-manager-bot-lambda-execution-role`의 인라인 정책
`dev-manager-bot-lambda-execution-policy` 내용입니다.

정책의 기준 정의는 `snorose-infra`의 `modules/iam`에 있습니다. 이 파일은 봇 저장소에서
필요 권한을 함께 검토하거나 IaC 반영 전 임시로 적용할 때 사용합니다.

```bash
export AWS_PROFILE=snorose-dev-sso

aws iam put-role-policy \
  --role-name snorose-dev-manager-bot-lambda-execution-role \
  --policy-name dev-manager-bot-lambda-execution-policy \
  --policy-document file://iam/lambda-execution-policy.json
```

Describe 계열 API는 리소스 단위 권한을 지원하지 않아 `Resource: "*"`입니다.
쓰기 권한은 DEV 앱 ASG와 `Name=snorose-dev-an2-fck-nat` / `Name=WARPConnector-dev`인 EC2 인스턴스로 한정되어 있습니다.
NAT 준비와 앱 종료를 Discord 응답과 분리하기 위해 봇 Lambda가 자기 자신을 비동기로
호출할 수 있는 권한도 포함합니다.

기존에 붙어 있던 `AmazonEC2FullAccess`는 사용하지 않습니다. 앱 서버는 ASG desired
capacity로 제어하고, `StartInstances` / `StopInstances`는 fck-nat 및 WARP 인스턴스에만 허용합니다.


준비 상태 검사에는 `snorose-dev-fck-nat-ready`와 `snorose-dev-warp-ready` 두 문서만
실행할 수 있는 `ssm:SendCommand` 및 `ssm:GetCommandInvocation`이 필요합니다.
임의 셸 명령을 받는 `AWS-RunShellScript` 실행 권한은 없습니다. Terraform 정책은
인스턴스 ARN으로 제한하며, 이 참조 JSON은 배포 전에 아직 모르는 인스턴스 ID를 Name 태그로 제한합니다.
