# AOAI FinOps Sentinel

Azure OpenAI 사용량과 비용을 분석하여, AI 기반 요약 리포트를 자동 생성하고 이메일로 발송하는 FinOps AI Agent입니다.

---

## 📌 Overview

AOAI FinOps Sentinel은 Azure OpenAI 환경에서 발생하는 **토큰 사용량, 요청 수, 비용 데이터**를 통합 분석하여
일일 및 월간 리포트를 자동 생성하고 이메일로 전달하는 시스템입니다.

운영 환경에서의 **비용 가시성 확보**, **모델별 사용량 분석**, **리전 통합 분석**을 목적으로 설계되었습니다.

---

## 🎯 Key Features

### 1. Azure OpenAI 사용량 분석

* Azure Monitor Metrics 기반 수집
* Input / Output / Total Token 집계
* Requests(요청 건수) 분석 추가
* Deployment 기준 데이터 → Model 기준 통합

---

### 2. 비용 분석

* Azure Cost Management API 활용
* 일별 / 월별 비용 집계
* 날짜 기준 비용 재합산 처리 (중요 개선사항)
* 리소스/리전 단위 비용 → 전체 통합

---

### 3. 모델 통합 분석 (핵심 기능)

* Deployment 기준 수집 데이터를
* **Canonical Model 기준으로 재구성**
* 다중 리전 / 다중 Deployment 환경에서도 통합 분석 가능

---

### 4. AI 기반 리포트 생성

* Azure OpenAI 활용
* 자연어 기반 요약 생성

---

### 5. 자동 이메일 발송

* SMTP 기반
* HTML 리포트 생성 (표 + 요약 포함)

---

## 🏗️ Architecture

```
Azure Monitor / Cost API
        ↓
Azure Function
        ↓
데이터 정규화 (Model / Region / Date 기준)
        ↓
LLM (요약 생성)
        ↓
HTML 리포트 생성
        ↓
SMTP 이메일 발송
```

---

## 📊 Data Processing Logic

### 공통 기준

* KST 기준 분석
* Metrics + Cost 데이터 통합
* Deployment → Model 변환

---

### 일일 리포트

* D-5 vs D-4 비교
* Token / Requests / Cost 증감 분석

---

### 월간 리포트

* 전월 1일 ~ 말일 기준 집계
* 비교 없이 총합 중심 리포트
* 일별 비용 포함

---

## 🔥 Model Aggregation (핵심 설계)

### ❗ 문제

* Azure Metrics에서

  * `ModelName` ❌ (토큰 기준 조회 불가)
  * `ModelDeploymentName` ⭕ (실제 기준)

---

### ✅ 해결 방식

**Deployment → Canonical Model 매핑 방식 사용**

```
Metrics
 → ModelDeploymentName
 → Mapping
 → Canonical Model
 → 집계
```

---

## ⚙️ Environment Variable

### 1. 모델 매핑 (핵심)

```
AOAI_DEPLOYMENT_MODEL_MAP
```

예시:

```json
{
  "gpt-4o-mini": "gpt-4o-mini",
  "gpt-4o-mini-jp": "gpt-4o-mini",
  "gpt-4o": "gpt-4o"
}
```

👉 서로 다른 리전/배포라도 동일 모델로 통합됨

---

### 2. 이메일 설정

```
MAIL_FROM
MAIL_TO
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
```

---

## 🚀 Model Deployment 가이드 (중요)

### 권장 방식

| Region    | Deployment Name | Model       |
| --------- | --------------- | ----------- |
| EastUS    | gpt-4o-mini     | gpt-4o-mini |
| JapanEast | gpt-4o-mini-jp  | gpt-4o-mini |

---

### 신규 모델 배포 시 필수 작업

1. Deployment 생성
2. 환경변수 Mapping 추가
3. API 호출 테스트
4. 리포트에서 모델 통합 확인

---

## 📊 Metrics 정의

### 토큰

* Input Tokens
* Output Tokens
* Total Tokens

### 요청

* AzureOpenAIRequests
* Deployment 기준 수집

---

## 🔥 Requests 처리 방식

* Metrics 기준: Deployment
* 분석 기준: Model

즉:

```
Requests (Deployment 기준 수집)
→ Model 매핑
→ 모델별 요청 수 집계
```

---

## 💰 비용 처리 로직 (중요 개선 사항)

### 문제

* Cost API는 **리소스 단위로 반환**
* 동일 날짜가 여러 줄로 내려옴

---

### 해결

✔ 날짜 기준 재집계 (핵심 로직)

```
(resource + date) rows
→ date 기준 group by
→ 일별 총 비용 생성
```

---

### 최종 결과

| 날짜    | 비용   |
| ----- | ---- |
| 03-26 | 8.81 |
| 03-27 | 1.35 |

👉 리전/리소스 관계없이 날짜당 1행 유지

---

## 📧 리포트 구성

### 일일 리포트

* 요약
* 토큰 비교
* 모델별 분석
* 리전/배포별 분석
* 비용 비교

---

### 월간 리포트

* 요약
* 월간 총 토큰 / 요청 / 비용
* 모델별 집계
* 리전/배포별 집계
* **일별 비용 표 (핵심 개선)**

---

## 🔐 Security

* Managed Identity 사용
* API Key 미사용 구조
* 환경변수 기반 설정

---

## ⚠️ Considerations

### 1. Cost 데이터 지연

* 최대 수 시간 ~ 24시간

### 2. Metrics 제한

* 일부 Dimension 제한 존재

### 3. 모델 기준 조회 불가

* 반드시 Deployment 기준 → 변환 필요

---

## 🔮 향후 확장

* 이상 탐지 (비용 급증)
* Budget Alert 연동
* 모델별 단가 분석
* 요청당 토큰 분석 (옵션 기능)

---

## 📌 핵심 요약

✔ Metrics는 Deployment 기준
✔ 분석은 Model 기준
✔ Cost는 날짜 기준 재집계 필수
✔ 다중 리전 환경에서도 통합 분석 가능

---

## 🧠 핵심 인사이트

이 시스템의 본질은 단순 리포팅이 아니라:

👉 **"Deployment 기반 데이터를 Model 기준으로 재해석하는 FinOps 시스템"**

---
