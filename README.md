# snorose-DEV-manager-bot

## 설명

DEV 서버 비용을 줄이고자 도입된 DEV 관리자 봇

- python 3.13 - flask 사용
- AWS CDK 배포
[노션 DEV 관리자 사용 가이드](https://www.notion.so/snorose/DEV-1a67ef0aa3bf8028bafff38536852086?pvs=4)

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

1. ```npm install``` 명령어를 실행합니다.
2. ```pip install -r requirements.txt```을 실행해 라이브러리를 설치합니다. (commands, src/app 폴더에 각각 존재)
3. [AWS Lambda로 Serverless 디스코드 봇 생성하기 (with CDK)](https://ctrl-shit-esc.tistory.com/185)에 작성된 대로 디스코드 봇을 생성하고 테스트를 진행할 서버에 초대합니다.
4. 생성한 디스코드 봇의 Application ID, Public Key, Token을 메모해 둡니다.
5. ```commands/register_commands.py``` 파일에 Token과 Application ID 값을 메모한 값으로 변경합니다.
6. ```src/app/main.py``` 파일에 ```DISCORD_PUBLIC_KEY``` 변수의 값을 메모한 Public Key 값으로 변경합니다.
7. ```src/app/main.py``` 파일의 ```@app.route``` 부분을 보면 주석이 있습니다. ```@app.route("/interactions", methods=["POST"])```의 주석을 해제하고 ```@app.route("/", methods=["POST"])``` 부분을 주석 처리해주세요.
8. [ngrok 설치 페이지](https://ngrok.com/downloads/windows)로 이동하여 운영 체제에 맞는 ngrok를 설치한 뒤 로그인을 진행합니다.
9. 로그인이 완료되면 Getting Started > Your Authtoken에서 Authtoken을 복사합니다.
10. ```ngrok config add-authtoken <Authtoken 입력>``` 명령어를 입력해 인증을 진행합니다.
11. root 경로에서 ```ngrok http 5000```을 입력해 5000번 포트로 포워딩된 https://000-000-0000.ngrok-free.app 형태의 엔드포인트를 [디스코드 개발자 포털](https://discord.com/developers/applications)에서 봇을 선택하여 들어간 뒤, General Information > Interactions Endpoint URL에 입력합니다. 이때 엔드포인트는 ```https://000-000-0000.ngrok-free.app/interactions``` 처럼 엔드포인트 뒤에 ```/interactions```를 추가해야 합니다.
12. ```commands``` 폴더로 이동해 ```python register_commands.py``` 명령어로 해당 파일을 실행해 명령어를 생성합니다.
13. ```src/app``` 폴더로 이동해 ```python main.py``` 명령어로 해당 파일을 실행해 디스코드 봇을 로컬에서 테스트합니다.

## 배포 방법

1. ```lib/discord-bot-lambda-stack.ts``` 파일의 ```DISCORD_PUBLIC_KEY```를 메모한 값으로 변경합니다. 이전에 로컬 테스트를 진행했다면 ```src/app/main.py``` 파일의 ```DISCORD_PUBLIC_KEY``` 부분을 ```DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")```로 다시 변경해주세요.
2. ```npm install -g aws-cdk```를 입력하여 aws-cdk를 설치합니다.
3. ```aws configure```를 입력하여 AWS 계정 정보를 등록합니다.
4. ```cdk bootstrap --region ap-northeast-2```를 입력해 CDK 부트스트랩 명령어를 실행합니다.
5. ```cdk deploy```를 입력하여 lambda로 배포합니다.
6. 배포가 성공했다면 출력된 url을 [디스코드 개발자 포털](https://discord.com/developers/applications)에서 봇을 선택하여 들어간 뒤, General Information > Interactions Endpoint URL에 입력합니다.
