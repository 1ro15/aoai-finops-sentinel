# AOAI FinOps Sentinel

Azure OpenAI 사용량과 비용을 분석하여,\
AI 기반 요약 리포트를 자동 생성하고 이메일로 발송하는 FinOps AI
Agent입니다.

------------------------------------------------------------------------

## 📌 Overview

AOAI FinOps Sentinel은 Azure OpenAI 환경에서 발생하는\
토큰 사용량 및 비용 데이터를 분석하여\
일일 리포트를 자동으로 생성하고 전달하는 시스템입니다.

운영 환경에서의 비용 가시성 확보와 사용 패턴 분석을 목적으로
설계되었습니다.

------------------------------------------------------------------------

## 🎯 Key Features

### 1. Azure OpenAI 비용 분석

-   Azure Cost Management API를 활용한 일별 비용 수집
-   D-5 / D-4 기준 비교 분석 (정산 지연 고려)
-   전일 대비 비용 증감 및 변화율 계산

### 2. 토큰 사용량 분석

-   Azure Monitor Metrics 기반 토큰 데이터 수집
-   Input / Output / Total Token 집계
-   리전 및 리소스 단위 통합 분석

### 3. AI 기반 리포트 생성

-   Azure OpenAI (gpt-4o-mini) 사용
-   구조화된 데이터를 기반으로 자연어 요약 생성
-   비용 변화 및 사용 패턴 자동 해석

### 4. 자동 이메일 발송

-   SMTP 기반 메일 발송
-   HTML 리포트 생성
-   Timer Trigger 기반 자동 실행

------------------------------------------------------------------------

## 🏗️ Architecture

Azure Monitor / Cost API → Azure Function → 데이터 분석 → LLM → 리포트 →
이메일 발송

------------------------------------------------------------------------

## ⚙️ Core Components

-   Azure Functions (Python)
-   Azure Monitor Metrics
-   Azure Cost Management API
-   Azure OpenAI
-   SMTP

------------------------------------------------------------------------

## 📊 Data Processing Logic

-   KST 기준 분석
-   D-5 vs D-4 비교
-   Token & Cost 증감 계산
-   LLM 기반 자연어 리포트 생성

------------------------------------------------------------------------

## 🔐 Security

-   Managed Identity 사용
-   환경 변수 기반 비밀 관리
-   코드 내 민감정보 미포함

------------------------------------------------------------------------

## ⚠️ Considerations

-   비용 데이터 지연 존재
-   Metrics 차원 제한 가능
-   SMTP 정책 영향 가능
