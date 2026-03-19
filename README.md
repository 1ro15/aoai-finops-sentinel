# AOAI FinOps Sentinel

Azure OpenAI 비용과 토큰 사용량을 비교 분석해 일일 메일 리포트를 발송하는 Azure Functions 기반 에이전트입니다.

## 목적
- Azure OpenAI 비용을 일일 기준으로 비교 분석
- 여러 리전/여러 Azure OpenAI 리소스의 토큰 사용량 집계
- D-5와 D-4 데이터를 비교해 D-Day에 메일 발송
- Azure OpenAI `gpt-4o-mini`를 사용해 짧은 한국어 리포트 생성

## 현재 구조
- **비용 데이터**: Azure Cost Management Query API
- **토큰 데이터**: Azure Monitor Metrics API
- **리포트 생성**: Azure OpenAI `gpt-4o-mini`
- **메일 발송**: SMTP (테스트는 Gmail → Naver 가능)
- **실행 방식**: Azure Functions HTTP Trigger + Timer Trigger

## 권장 운영 흐름
1. Function App이 매일 정해진 시각(UTC 기준 스케줄)마다 실행됨
2. KST 기준 D-5 / D-4의 토큰 사용량을 조회함
3. KST 기준 D-5 / D-4의 비용을 조회함
4. 증감 데이터를 바탕으로 `gpt-4o-mini`가 짧은 한국어 리포트를 생성함
5. HTML 메일을 SMTP로 발송함

## 현재 구현된 주요 함수
- `daily_compare`
  - D-5 / D-4 토큰 및 비용 비교 JSON 반환
- `daily_report_preview`
  - 비교 데이터 기반 AI 요약문 미리보기
- `daily_report_send`
  - 비교 데이터 생성 + 요약 생성 + 메일 발송
- `daily_report_timer`
  - 스케줄 기반 자동 발송

## 권한(RBAC)
Managed Identity에 아래 권한이 필요합니다.
- `Cognitive Services OpenAI User`
- `Cost Management Reader`
- `Monitoring Reader`

## 주의사항
### 1. 민감 정보는 GitHub에 올리지 않기
아래 항목은 저장소에 커밋하면 안 됩니다.
- `local.settings.json`
- SMTP 비밀번호 / Gmail 앱 비밀번호
- Azure 키 / 기타 비밀값

### 2. `.gitignore`에 반드시 포함
추천 `.gitignore` 항목:
- `.venv/`
- `__pycache__/`
- `*.pyc`
- `local.settings.json`
- `.vscode/`

### 3. 시간 기준
- Azure Timer Trigger의 CRON은 **UTC 기준**입니다.
- KST 오전 9시 실행은 `0 0 0 * * *` 입니다.

### 4. 비용 기준
- 실 운영에서는 비용 데이터가 다음 날 바로 확정되지 않을 수 있으므로
  **D-5 / D-4 비교**로 리포트합니다.

### 5. 모델/버전 구분
- 현재 Metrics API 응답에서 모델/버전 차원이 비어 있을 수 있습니다.
- 단일 배포, 낮은 사용량, Azure Monitor 차원 응답 방식에 따라 `model_name`, `model_version`이 빈 값으로 내려올 수 있습니다.
- 이 경우에도 전체 토큰/비용 기준 리포트는 정상 생성됩니다.

### 6. SMTP 테스트
- 테스트는 Gmail SMTP를 사용하는 것이 가장 단순합니다.
- Gmail 사용 시 일반 계정 비밀번호가 아니라 **앱 비밀번호**를 사용해야 합니다. Google은 외부 앱 연결에 앱 비밀번호 사용을 안내하며, 앱 비밀번호는 2단계 인증이 켜진 계정에서만 생성할 수 있습니다. citeturn654677view1

### 7. 로그 확인
자동 발송 문제를 확인할 때는 아래를 우선 봅니다.
- Function App → 로그 스트림
- Function App → Functions → `daily_report_timer` → 모니터
- Application Insights 로그

## GitHub 운영 추천
### 1단계
- 로컬 코드 → GitHub 저장소 수동 push
- VS Code에서 수정 후 Azure로 직접 배포

### 2단계
- GitHub Actions로 자동 배포 도입
- `main` 브랜치 push 시 Function App 자동 배포

## Git 설치 관련
현재 `git init` 이 안 되는 원인은 **Git 자체가 Windows PC에 설치되어 있지 않거나 PATH에 잡히지 않았기 때문**입니다.
GitHub Docs도 Git을 로컬에서 쓰려면 먼저 Git을 다운로드·설치·구성해야 한다고 안내합니다. citeturn654677view1

### Windows에 Git 설치 순서
1. Git for Windows 설치 파일 다운로드
2. 기본 옵션으로 설치
3. PowerShell 재실행
4. `git --version` 확인

## 다음 권장 단계
1. Git 설치 및 GitHub 저장소 연결
2. 타이머 자동 발송 안정화 확인
3. 월간 리포트 추가
4. 모델/버전별 상세 분석 보강
