# AOAI FinOps Sentinel

Azure OpenAI 사용량과 비용을 분석하여, AI 기반 요약 리포트를 자동
생성하고 이메일로 발송하는 FinOps AI Agent입니다.

------------------------------------------------------------------------

## 📌 Overview

AOAI FinOps Sentinel은 Azure OpenAI 환경에서 발생하는 토큰 사용량 및
비용 데이터를 분석하여 일일 리포트를 자동으로 생성하고 전달하는
시스템입니다.

운영 환경에서의 비용 가시성 확보와 사용 패턴 분석을 목적으로
설계되었습니다.

------------------------------------------------------------------------

## 🎯 Key Features

### 1. Azure OpenAI 비용 분석

-   Azure Cost Management API 활용
-   D-5 / D-4 비교 분석
-   비용 증감 및 변화율 계산

### 2. 토큰 사용량 분석

-   Azure Monitor Metrics 기반 수집
-   Input / Output / Total Token 집계
-   Deployment → Model 매핑 기반 통합 분석

### 3. AI 기반 리포트 생성

-   Azure OpenAI 사용
-   자연어 요약 생성

### 4. 자동 이메일 발송

-   SMTP 기반
-   HTML 리포트 생성

------------------------------------------------------------------------

## 🏗️ Architecture

Azure Monitor / Cost API → Azure Function → 데이터 정규화 → LLM → 이메일

------------------------------------------------------------------------

## 📊 Data Processing Logic

-   KST 기준 분석
-   D-5 vs D-4 비교
-   Token & Cost 증감 계산
-   모델 통합 집계

------------------------------------------------------------------------

# 🔥 Model Aggregation (핵심)

## 문제

-   ModelName ❌ 지원 안됨
-   ModelDeploymentName ⭕

## 해결

Deployment → Canonical Model 매핑

## 처리 흐름

Metrics → Deployment → Map → Model → 집계

------------------------------------------------------------------------

## ⚙️ Environment Variable

AOAI_DEPLOYMENT_MODEL_MAP

예시:
{"gpt-4o-mini":"gpt-4o-mini","gpt-4o-mini-jp":"gpt-4o-mini","gpt-4o":"gpt-4o"}

------------------------------------------------------------------------

## 🚀 Model Deployment 가이드

권장: - gpt-4o-mini - gpt-4o-mini-jp - gpt-4o-mini-eu

신규 배포 시: 1. Deployment 생성 2. Map 추가 3. API 확인 4. 모델 통합
확인

------------------------------------------------------------------------

## 📊 메트릭

-   Input Tokens
-   Output Tokens
-   Total Tokens

------------------------------------------------------------------------

## 🔮 향후 확장

-   Requests 분석
-   비용 분석
-   이상 탐지

------------------------------------------------------------------------

## 🔐 Security

-   Managed Identity
-   환경 변수 관리

------------------------------------------------------------------------

## ⚠️ Considerations

-   Cost 지연
-   Metrics 제한
-   SMTP 영향

------------------------------------------------------------------------

## 📌 핵심 요약

Metrics는 Deployment 기준, 분석은 Model 기준
