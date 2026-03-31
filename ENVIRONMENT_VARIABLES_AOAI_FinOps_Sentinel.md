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

## 3. 모델 분리 및 통합 집계
### `AOAI_DEPLOYMENT_MODEL_MAP`
형식:
- **한 줄 JSON 문자열**
- `ModelDeploymentName` 을 기준으로 **대표 모델명(canonical model name)** 으로 매핑
- 토큰 메트릭은 `ModelName` 차원 조회가 되지 않아, 리전별/배포별로 다른 배포명을 동일 모델로 합산하기 위해 사용

예시:
```json
{"gpt-4o-mini":"gpt-4o-mini","gpt-4o-mini-jp":"gpt-4o-mini","gpt-4o":"gpt-4o"}
```

설명:
- 동일 모델이 리전별로 다른 배포명으로 운영될 때, 보고서에서는 하나의 모델로 합산하기 위한 매핑
- 예:
  - East US 배포명 `gpt-4o-mini` → 대표 모델명 `gpt-4o-mini`
  - Japan East 배포명 `gpt-4o-mini-jp` → 대표 모델명 `gpt-4o-mini`
- 메일 보고서의 **모델별 토큰 비교(모델 기준 통합)**, API 응답의 `model_summary` 에 이 값이 반영됨

주의:
- **줄바꿈 없이 한 줄**로 넣기
- key 는 실제 배포명(`ModelDeploymentName`)
- value 는 최종 통합 기준이 되는 대표 모델명
- 신규 리전/신규 배포 추가 시 반드시 같이 업데이트
- 매핑이 없으면 코드에서 배포명을 그대로 모델명으로 사용하므로, 동일 모델 합산이 되지 않을 수 있음

운영 예시:
```json
{"gpt-4o-mini":"gpt-4o-mini","gpt-4o-mini-jp":"gpt-4o-mini","gpt-4o-mini-eu":"gpt-4o-mini","gpt-4o":"gpt-4o"}
```

---

## 4. 비용 조회
### `SUBSCRIPTION_ID`
예시:
```text
56631d8c-2e3c-4d28-be06-f5cfe5dd5fcf
```
설명:
- Azure 구독 ID
- Cost Management Query API 조회에 사용

---

## 5. 메일 발송(SMTP)
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
- 앱 비밀번호는 2단계 인증이 켜진 계정에서만 생성 가능

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

## 6. 자동 실행 스케줄
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

## 7. 현재 사용 중인 대표 환경 변수 목록 요약
```text
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_DEPLOYMENT_NAME
AZURE_CLIENT_ID
AOAI_RESOURCE_IDS
AOAI_DEPLOYMENT_MODEL_MAP
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

## 8. GitHub에 올리면 안 되는 값
다음 값들은 저장소에 올리면 안 됩니다.
- `SMTP_PASSWORD`
- `AZURE_CLIENT_ID` 자체는 올려도 큰 비밀은 아니지만, 보통 환경 변수 파일에 하드코딩하지 않는 것이 좋음
- `local.settings.json`
- 기타 비밀키/암호

환경 변수 값은 **Azure Portal의 Function App 환경 변수**에서 관리하는 것을 권장합니다.
