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
4. 각 Environment secret에 ```DISCORD_BOT_TOKEN```을 설정합니다.
5. ```develop``` 또는 ```main``` 브랜치에 push하면 GitHub Actions가 이미지를 배포하고 Discord slash command를 등록합니다.
6. Lambda Function URL에 ```/interactions```를 붙여 [디스코드 개발자 포털](https://discord.com/developers/applications)의 General Information > Interactions Endpoint URL에 입력합니다.
7. 배포 뒤 Lambda에 EC2 인스턴스 조회 및 시작, 종료 권한을 부여합니다.
