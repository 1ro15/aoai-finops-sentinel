# AOAI FinOps Sentinel - Environment Variables

아래 값들은 Azure Function App의 **환경 변수(App Settings)** 에 등록하는 것을 기준으로 정리했습니다.

---

## 1. Azure OpenAI
### `AZURE_OPENAI_ENDPOINT`
예시:
```text
https://<aoai-resource-name>.openai.azure.com/
```
설명:
- Azure OpenAI 엔드포인트
- 포털의 **Keys and Endpoint** 에서 확인

### `AZURE_OPENAI_DEPLOYMENT_NAME`
예시:
```text
gpt-4o-mini
```
설명:
- Azure OpenAI **배포명**
- 모델명이 아니라 포털 Deployments 화면의 실제 배포 이름

### `AZURE_CLIENT_ID`
설명:
- **User Assigned Managed Identity** 를 쓰는 경우 필요
- Managed Identity 리소스의 **Client ID**
- System Assigned만 쓰면 보통 불필요

---

## 2. Azure OpenAI 리소스 목록
### `AOAI_RESOURCE_IDS`
형식:
- **한 줄 JSON 문자열**
- 여러 AOAI 리소스를 배열로 등록

예시:
```json
[{"resource_id":"/subscriptions/xxxx/resourceGroups/rg-aoai/providers/Microsoft.CognitiveServices/accounts/aoai-eastus","region":"eastus"}]
```

여러 개 예시:
```json
[{"resource_id":"/subscriptions/xxxx/resourceGroups/rg-aoai/providers/Microsoft.CognitiveServices/accounts/aoai-eastus","region":"eastus"},{"resource_id":"/subscriptions/xxxx/resourceGroups/rg-aoai/providers/Microsoft.CognitiveServices/accounts/aoai-koreacentral","region":"koreacentral"}]
```

주의:
- **줄바꿈 없이 한 줄**로 넣기
- `region` 값은 소문자 권장
  - `eastus`
  - `koreacentral`
  - `japaneast`

---

## 3. 비용 조회
### `SUBSCRIPTION_ID`
예시:
```text
56631d8c-2e3c-4d28-be06-f5cfe5dd5fcf
```
설명:
- Azure 구독 ID
- Cost Management Query API 조회에 사용

---

## 4. 메일 발송(SMTP)
### `SMTP_HOST`
예시:
```text
smtp.gmail.com
```

### `SMTP_PORT`
예시:
```text
587
```

### `SMTP_USERNAME`
예시:
```text
mytestsender@gmail.com
```
설명:
- SMTP 로그인 계정
- Gmail 테스트 시 본인 Gmail 주소

### `SMTP_PASSWORD`
예시:
```text
abcdefghijklmnop
```
설명:
- Gmail 테스트 시 **앱 비밀번호(16자리)**
- 일반 Gmail 로그인 비밀번호 아님
- 앱 비밀번호는 2단계 인증이 켜진 계정에서만 생성 가능. Google 도움말도 2단계 인증이 켜진 계정에서 앱 비밀번호를 생성할 수 있다고 안내합니다. citeturn654677view1

### `MAIL_FROM`
예시:
```text
mytestsender@gmail.com
```
설명:
- 발신자 주소
- 테스트 단계에서는 `SMTP_USERNAME` 과 같은 값 권장

### `MAIL_TO`
예시:
```text
mytarget@naver.com
```
또는 여러 명:
```text
user1@naver.com,user2@gmail.com
```

---

## 5. 자동 실행 스케줄
### `DAILY_REPORT_SCHEDULE`
설명:
- Azure Functions Timer Trigger 스케줄
- **UTC 기준 NCRONTAB 6필드 형식**

운영 예시 (KST 오전 9시):
```text
0 0 0 * * *
```

테스트 예시 (5분마다):
```text
0 */5 * * * *
```

테스트 예시 (10분마다):
```text
0 */10 * * * *
```

---

## 6. 현재 사용 중인 대표 환경 변수 목록 요약
```text
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_DEPLOYMENT_NAME
AZURE_CLIENT_ID
AOAI_RESOURCE_IDS
SUBSCRIPTION_ID
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
MAIL_FROM
MAIL_TO
DAILY_REPORT_SCHEDULE
```

---

## 7. GitHub에 올리면 안 되는 값
다음 값들은 저장소에 올리면 안 됩니다.
- `SMTP_PASSWORD`
- `AZURE_CLIENT_ID` 자체는 올려도 큰 비밀은 아니지만, 보통 환경 변수 파일에 하드코딩하지 않는 것이 좋음
- `local.settings.json`
- 기타 비밀키/암호

환경 변수 값은 **Azure Portal의 Function App 환경 변수**에서 관리하는 것을 권장합니다.
