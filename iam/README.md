# IAM

`lambda-execution-policy.json`은 Lambda 실행 역할
`snorose-dev-manager-bot-lambda-execution-role`의 인라인 정책
`dev-manager-bot-lambda-execution-policy` 내용입니다.

이 저장소는 아직 IaC로 관리되지 않으므로, 변경 시 아래 명령으로 반영합니다.

```bash
export AWS_PROFILE=snorose-dev-sso

aws iam put-role-policy \
  --role-name snorose-dev-manager-bot-lambda-execution-role \
  --policy-name dev-manager-bot-lambda-execution-policy \
  --policy-document file://iam/lambda-execution-policy.json
```

Describe 계열 API는 리소스 단위 권한을 지원하지 않아 `Resource: "*"`입니다.
쓰기 권한인 `autoscaling:SetDesiredCapacity`만 DEV ASG로 한정되어 있습니다.

기존에 붙어 있던 `AmazonEC2FullAccess`는 제거했습니다. ASG 방식으로 바꾸면서
`StartInstances` / `StopInstances`가 더 이상 필요 없어졌고, 남은 EC2 호출은
`DescribeInstances` / `DescribeInstanceStatus` 두 개뿐입니다.
