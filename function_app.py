import json
import logging
import os
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import azure.functions as func
import requests
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

app = func.FunctionApp()

KST = timezone(timedelta(hours=9))


# -----------------------------
# 공통 유틸
# -----------------------------
def get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"환경 변수 누락: {name}")
    return value


def format_number(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return f"{value:,}"


def calculate_change(current_value: float | None, previous_value: float | None) -> dict[str, float | None]:
    if current_value is None or previous_value is None:
        return {"difference": None, "rate_percent": None}

    diff = current_value - previous_value
    rate = None
    if previous_value != 0:
        rate = (diff / previous_value) * 100

    return {"difference": diff, "rate_percent": rate}


def normalize_dimension_value(value: str | None, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    value = str(value).strip()
    if not value:
        return fallback
    return value


def get_kst_day_range_to_utc(days_ago: int) -> tuple[datetime, datetime, str]:
    now_kst = datetime.now(KST)
    target_date_kst = (now_kst - timedelta(days=days_ago)).date()

    start_kst = datetime.combine(target_date_kst, datetime.min.time(), tzinfo=KST)
    end_kst = start_kst + timedelta(days=1)

    start_utc = start_kst.astimezone(timezone.utc)
    end_utc = end_kst.astimezone(timezone.utc)

    return start_utc, end_utc, str(target_date_kst)


def get_kst_month_range_to_utc() -> tuple[datetime, datetime, str, str]:
    now_kst = datetime.now(KST)
    month_start_kst = datetime(now_kst.year, now_kst.month, 1, 0, 0, 0, tzinfo=KST)

    if now_kst.month == 12:
        next_month_kst = datetime(now_kst.year + 1, 1, 1, 0, 0, 0, tzinfo=KST)
    else:
        next_month_kst = datetime(now_kst.year, now_kst.month + 1, 1, 0, 0, 0, tzinfo=KST)

    return (
        month_start_kst.astimezone(timezone.utc),
        next_month_kst.astimezone(timezone.utc),
        month_start_kst.strftime("%Y-%m-%d"),
        (next_month_kst - timedelta(days=1)).strftime("%Y-%m-%d"),
    )


def to_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_line_breaks(text: str) -> str:
    if not text:
        return text

    separators = [". ", "다. ", "니다. ", "! ", "? "]
    result = text

    for sep in separators:
        result = result.replace(sep, sep.strip() + "\n")

    return result.strip()


# -----------------------------
# 리소스 설정
# -----------------------------
def load_resources() -> list[dict[str, str]]:
    raw = get_env("AOAI_RESOURCE_IDS")
    resources = json.loads(raw)

    if not isinstance(resources, list) or not resources:
        raise ValueError("AOAI_RESOURCE_IDS는 비어있지 않은 JSON 배열이어야 합니다.")

    normalized = []
    for item in resources:
        if not isinstance(item, dict):
            raise ValueError("AOAI_RESOURCE_IDS의 각 항목은 객체여야 합니다.")

        resource_id = str(item.get("resource_id", "")).strip()
        region = str(item.get("region", "")).strip().lower()

        if not resource_id:
            raise ValueError("AOAI_RESOURCE_IDS의 각 항목에는 resource_id가 필요합니다.")
        if not region:
            raise ValueError("AOAI_RESOURCE_IDS의 각 항목에는 region이 필요합니다.")

        normalized.append({
            "resource_id": resource_id,
            "region": region
        })

    return normalized


# -----------------------------
# 메트릭 조회
# -----------------------------
def get_azure_management_token(credential: DefaultAzureCredential) -> str:
    return credential.get_token("https://management.azure.com/.default").token


def extract_metadata_values(ts: dict[str, Any]) -> dict[str, str]:
    result = {}
    for item in ts.get("metadatavalues", []) or []:
        name = (((item.get("name") or {}).get("value")) or "").strip()
        value = str(item.get("value", "")).strip()
        if name:
            result[name] = value
    return result


def query_metric_split_by_deployment(
    credential: DefaultAzureCredential,
    resource_id: str,
    metric_name: str,
    start_time_utc: datetime,
    end_time_utc: datetime,
) -> list[dict[str, Any]]:
    token = get_azure_management_token(credential)
    url = f"https://management.azure.com{resource_id}/providers/microsoft.insights/metrics"

    def call_metrics_api(filter_expr: str | None = None) -> requests.Response:
        params = {
            "api-version": "2018-01-01",
            "metricnames": metric_name,
            "timespan": f"{to_utc_z(start_time_utc)}/{to_utc_z(end_time_utc)}",
            "interval": "P1D",
            "aggregation": "Total",
        }
        if filter_expr:
            params["$filter"] = filter_expr

        return requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=60,
        )

    response = call_metrics_api("ModelDeploymentName eq '*'")

    if response.status_code != 200:
        logging.warning(
            "Metrics split query failed. fallback to aggregate. metric=%s status=%s body=%s",
            metric_name,
            response.status_code,
            response.text,
        )
        response = call_metrics_api()

    if response.status_code != 200:
        raise RuntimeError(f"Metrics API 호출 실패: {response.status_code} / {response.text}")

    payload = response.json()
    values = payload.get("value", []) or []
    rows: list[dict[str, Any]] = []

    for metric in values:
        metric_name_value = (((metric.get("name") or {}).get("value")) or metric_name)

        for ts in metric.get("timeseries", []) or []:
            metadata = extract_metadata_values(ts)

            total_value = 0
            for point in ts.get("data", []) or []:
                point_total = point.get("total")
                if point_total is not None:
                    total_value += point_total

            deployment = normalize_dimension_value(
                metadata.get("ModelDeploymentName")
                or metadata.get("modeldeploymentname"),
                fallback="unknown"
            )

            model_name = normalize_dimension_value(
                metadata.get("ModelName")
                or metadata.get("modelname")
                or deployment,
                fallback=deployment
            )

            rows.append({
                "metric_name": metric_name_value,
                "model_deployment_name": deployment,
                "model_name": model_name,
                "raw_dimensions": metadata,
                "total": total_value,
            })

    return rows


def query_all_metrics_for_resource(
    credential: DefaultAzureCredential,
    resource: dict[str, str],
    start_time_utc: datetime,
    end_time_utc: datetime,
) -> list[dict[str, Any]]:
    metric_names = [
        "ProcessedPromptTokens",
        "GeneratedTokens",
        "TokenTransaction",
    ]

    all_rows: list[dict[str, Any]] = []

    for metric_name in metric_names:
        metric_rows = query_metric_split_by_deployment(
            credential=credential,
            resource_id=resource["resource_id"],
            metric_name=metric_name,
            start_time_utc=start_time_utc,
            end_time_utc=end_time_utc,
        )

        for row in metric_rows:
            all_rows.append({
                "resource_id": resource["resource_id"],
                "region": resource["region"],
                "metric_name": row["metric_name"],
                "model_deployment_name": row["model_deployment_name"],
                "model_name": row["model_name"],
                "raw_dimensions": row["raw_dimensions"],
                "total": row["total"],
            })

    return all_rows


def metric_name_to_field(metric_name: str) -> str:
    mapping = {
        "ProcessedPromptTokens": "prompt_tokens",
        "GeneratedTokens": "completion_tokens",
        "TokenTransaction": "total_tokens",
    }
    return mapping.get(metric_name, metric_name)


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple, dict[str, Any]] = {}

    for row in rows:
        key = (
            row["resource_id"],
            row["region"],
            row["model_deployment_name"],
            row["model_name"],
        )

        if key not in grouped:
            grouped[key] = {
                "resource_id": row["resource_id"],
                "region": row["region"],
                "model_deployment_name": row["model_deployment_name"],
                "model_name": row["model_name"],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "raw_dimensions": row["raw_dimensions"],
            }

        field_name = metric_name_to_field(row["metric_name"])
        if field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            grouped[key][field_name] += row["total"]

    return list(grouped.values())


def sum_items(items: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "prompt_tokens": sum(x.get("prompt_tokens", 0) for x in items),
        "completion_tokens": sum(x.get("completion_tokens", 0) for x in items),
        "total_tokens": sum(x.get("total_tokens", 0) for x in items),
    }


def fetch_day_metrics(
    credential: DefaultAzureCredential,
    resources: list[dict[str, str]],
    days_ago: int,
) -> dict[str, Any]:
    start_utc, end_utc, target_date_kst = get_kst_day_range_to_utc(days_ago)

    all_rows: list[dict[str, Any]] = []

    for resource in resources:
        rows = query_all_metrics_for_resource(
            credential=credential,
            resource=resource,
            start_time_utc=start_utc,
            end_time_utc=end_utc,
        )
        all_rows.extend(rows)

    normalized = normalize_rows(all_rows)
    summary = sum_items(normalized)

    return {
        "target_date_kst": target_date_kst,
        "start_time_utc": start_utc.isoformat(),
        "end_time_utc": end_utc.isoformat(),
        "items": normalized,
        "summary": summary,
    }


# -----------------------------
# 비용 조회
# -----------------------------
def parse_cost_response(data: dict[str, Any], target_ids: list[str], fallback_date: str) -> dict[str, Any]:
    properties = data.get("properties", {})
    rows = properties.get("rows", [])
    columns = properties.get("columns", [])

    col_index = {}
    for idx, col in enumerate(columns):
        col_index[col.get("name")] = idx

    resource_id_idx = col_index.get("ResourceId")
    total_cost_idx = col_index.get("totalCost")
    currency_idx = col_index.get("Currency")
    usage_date_idx = col_index.get("UsageDate")

    resource_costs = []
    total_cost = 0.0
    currency = None

    normalized_resource_ids = {x.lower(): x for x in target_ids}

    for row in rows:
        row_resource_id = str(row[resource_id_idx]).lower() if resource_id_idx is not None else ""
        if target_ids and row_resource_id not in normalized_resource_ids:
            continue

        cost_value = float(row[total_cost_idx]) if total_cost_idx is not None else 0.0
        currency = row[currency_idx] if currency_idx is not None else currency

        resource_costs.append({
            "resource_id": normalized_resource_ids.get(row_resource_id, row_resource_id),
            "usage_date": str(row[usage_date_idx]) if usage_date_idx is not None else fallback_date,
            "cost": cost_value,
            "currency": currency
        })
        total_cost += cost_value

    return {
        "currency": currency,
        "total_cost": total_cost,
        "resource_costs": resource_costs
    }


def fetch_day_costs(
    credential: DefaultAzureCredential,
    subscription_id: str,
    resource_ids: list[str],
    days_ago: int,
) -> dict[str, Any]:
    start_utc, end_utc, target_date_kst = get_kst_day_range_to_utc(days_ago)

    token = credential.get_token("https://management.azure.com/.default").token

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CostManagement/query?api-version=2025-03-01"
    )

    body = {
        "type": "Usage",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start_utc.isoformat(),
            "to": end_utc.isoformat()
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {
                "totalCost": {
                    "name": "PreTaxCost",
                    "function": "Sum"
                }
            },
            "grouping": [
                {
                    "type": "Dimension",
                    "name": "ResourceId"
                }
            ]
        }
    }

    retry_delays = [60, 300, 600]
    last_response = None

    for attempt in range(len(retry_delays) + 1):
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json=body,
            timeout=60
        )

        if response.status_code == 200:
            parsed = parse_cost_response(response.json(), resource_ids, target_date_kst)
            return {
                "target_date_kst": target_date_kst,
                "start_time_utc": start_utc.isoformat(),
                "end_time_utc": end_utc.isoformat(),
                "currency": parsed["currency"],
                "total_cost": parsed["total_cost"],
                "resource_costs": parsed["resource_costs"],
                "cost_data_available": True
            }

        last_response = response

        if response.status_code == 429 and attempt < len(retry_delays):
            delay = retry_delays[attempt]
            logging.warning(
                "Cost API throttled (429). retry=%s/%s wait=%ss",
                attempt + 1,
                len(retry_delays),
                delay
            )
            time.sleep(delay)
            continue

        break

    raise RuntimeError(
        f"Cost API 호출 실패: {last_response.status_code} / {last_response.text}"
    )


def fetch_current_month_costs(
    credential: DefaultAzureCredential,
    subscription_id: str,
    resource_ids: list[str],
) -> dict[str, Any]:
    start_utc, end_utc, start_kst_str, end_kst_str = get_kst_month_range_to_utc()
    token = credential.get_token("https://management.azure.com/.default").token

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CostManagement/query?api-version=2025-03-01"
    )

    body = {
        "type": "Usage",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start_utc.isoformat(),
            "to": end_utc.isoformat()
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {
                "totalCost": {
                    "name": "PreTaxCost",
                    "function": "Sum"
                }
            },
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"}
            ]
        }
    }

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        json=body,
        timeout=60
    )

    if response.status_code != 200:
        raise RuntimeError(f"월간 Cost API 호출 실패: {response.status_code} / {response.text}")

    data = response.json()
    properties = data.get("properties", {})
    rows = properties.get("rows", [])
    columns = properties.get("columns", [])

    col_index = {}
    for idx, col in enumerate(columns):
        col_index[col.get("name")] = idx

    resource_id_idx = col_index.get("ResourceId")
    total_cost_idx = col_index.get("totalCost")
    currency_idx = col_index.get("Currency")
    usage_date_idx = col_index.get("UsageDate")

    normalized_resource_ids = {x.lower(): x for x in resource_ids}

    daily_rows = []
    total_cost = 0.0
    currency = None

    for row in rows:
        row_resource_id = str(row[resource_id_idx]).lower() if resource_id_idx is not None else ""
        if resource_ids and row_resource_id not in normalized_resource_ids:
            continue

        cost_value = float(row[total_cost_idx]) if total_cost_idx is not None else 0.0
        currency = row[currency_idx] if currency_idx is not None else currency
        usage_date = str(row[usage_date_idx]) if usage_date_idx is not None else ""

        daily_rows.append({
            "usage_date": usage_date,
            "resource_id": normalized_resource_ids.get(row_resource_id, row_resource_id),
            "cost": cost_value,
            "currency": currency
        })

        total_cost += cost_value

    daily_rows.sort(key=lambda x: (x["usage_date"], x["resource_id"]))

    return {
        "period_kst": f"{start_kst_str} ~ {end_kst_str}",
        "currency": currency,
        "total_cost": total_cost,
        "daily_rows": daily_rows
    }


# -----------------------------
# 모델별 비교
# -----------------------------
def build_model_key(item: dict[str, Any]) -> tuple:
    return (
        item.get("resource_id", "unknown"),
        item.get("region", "unknown"),
        item.get("model_name", "unknown"),
        item.get("model_deployment_name", "unknown"),
    )


def build_model_breakdown(
    previous_items: list[dict[str, Any]],
    current_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    previous_map = {build_model_key(item): item for item in previous_items}
    current_map = {build_model_key(item): item for item in current_items}

    all_keys = set(previous_map.keys()) | set(current_map.keys())
    result = []

    for key in all_keys:
        prev = previous_map.get(key, {})
        curr = current_map.get(key, {})

        previous_day = {
            "prompt_tokens": prev.get("prompt_tokens", 0),
            "completion_tokens": prev.get("completion_tokens", 0),
            "total_tokens": prev.get("total_tokens", 0),
        }
        current_day = {
            "prompt_tokens": curr.get("prompt_tokens", 0),
            "completion_tokens": curr.get("completion_tokens", 0),
            "total_tokens": curr.get("total_tokens", 0),
        }

        result.append({
            "resource_id": key[0],
            "region": key[1],
            "model_name": key[2],
            "model_deployment_name": key[3],
            "previous_day": previous_day,
            "current_day": current_day,
            "change": {
                "prompt_tokens": calculate_change(
                    current_day["prompt_tokens"], previous_day["prompt_tokens"]
                ),
                "completion_tokens": calculate_change(
                    current_day["completion_tokens"], previous_day["completion_tokens"]
                ),
                "total_tokens": calculate_change(
                    current_day["total_tokens"], previous_day["total_tokens"]
                ),
            }
        })

    result.sort(
        key=lambda x: (
            -(x["current_day"]["total_tokens"] or 0),
            x["model_name"],
            x["model_deployment_name"],
            x["region"],
        )
    )

    return result


# -----------------------------
# 데이터 통합
# -----------------------------
def build_daily_compare_data() -> dict[str, Any]:
    resources = load_resources()
    credential = DefaultAzureCredential()
    subscription_id = get_env("SUBSCRIPTION_ID")

    resource_ids = [x["resource_id"] for x in resources]

    d5_metrics = fetch_day_metrics(credential, resources, days_ago=5)
    d4_metrics = fetch_day_metrics(credential, resources, days_ago=4)

    model_breakdown = build_model_breakdown(
        d5_metrics["items"],
        d4_metrics["items"]
    )

    cost_error = None

    try:
        d5_costs = fetch_day_costs(credential, subscription_id, resource_ids, days_ago=5)
        d4_costs = fetch_day_costs(credential, subscription_id, resource_ids, days_ago=4)
        cost_change = calculate_change(
            d4_costs["total_cost"],
            d5_costs["total_cost"]
        )
    except Exception as e:
        logging.exception("Cost data fetch failed")
        cost_error = str(e)

        d5_costs = {
            "target_date_kst": d5_metrics["target_date_kst"],
            "start_time_utc": d5_metrics["start_time_utc"],
            "end_time_utc": d5_metrics["end_time_utc"],
            "currency": None,
            "total_cost": None,
            "resource_costs": [],
            "cost_data_available": False
        }
        d4_costs = {
            "target_date_kst": d4_metrics["target_date_kst"],
            "start_time_utc": d4_metrics["start_time_utc"],
            "end_time_utc": d4_metrics["end_time_utc"],
            "currency": None,
            "total_cost": None,
            "resource_costs": [],
            "cost_data_available": False
        }
        cost_change = {
            "difference": None,
            "rate_percent": None
        }

    token_change = {
        "prompt_tokens": calculate_change(
            d4_metrics["summary"]["prompt_tokens"], d5_metrics["summary"]["prompt_tokens"]
        ),
        "completion_tokens": calculate_change(
            d4_metrics["summary"]["completion_tokens"], d5_metrics["summary"]["completion_tokens"]
        ),
        "total_tokens": calculate_change(
            d4_metrics["summary"]["total_tokens"], d5_metrics["summary"]["total_tokens"]
        ),
    }

    return {
        "timezone": "KST",
        "resource_count": len(resources),
        "cost_error": cost_error,
        "comparison": {
            "previous_day": {
                "date_kst": d5_metrics["target_date_kst"],
                "metrics": d5_metrics,
                "costs": d5_costs
            },
            "current_day": {
                "date_kst": d4_metrics["target_date_kst"],
                "metrics": d4_metrics,
                "costs": d4_costs
            },
            "summary_change": {
                "tokens": token_change,
                "cost": cost_change
            },
            "model_breakdown": model_breakdown
        }
    }


# -----------------------------
# LLM 리포트
# -----------------------------
def generate_report_text(compare_data: dict[str, Any]) -> str:
    endpoint = get_env("AZURE_OPENAI_ENDPOINT")
    deployment_name = get_env("AZURE_OPENAI_DEPLOYMENT_NAME")

    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential,
        "https://cognitiveservices.azure.com/.default"
    )

    client = AzureOpenAI(
        azure_endpoint=endpoint,
        api_version="2024-10-21",
        azure_ad_token_provider=token_provider
    )

    model_breakdown = compare_data["comparison"].get("model_breakdown", [])
    top_models = model_breakdown[:3]

    lightweight_data = {
        "timezone": compare_data["timezone"],
        "resource_count": compare_data["resource_count"],
        "cost_error": compare_data.get("cost_error"),
        "previous_day": {
            "date_kst": compare_data["comparison"]["previous_day"]["date_kst"],
            "summary": compare_data["comparison"]["previous_day"]["metrics"]["summary"],
            "cost_total": compare_data["comparison"]["previous_day"]["costs"]["total_cost"],
            "cost_available": compare_data["comparison"]["previous_day"]["costs"].get("cost_data_available", True),
        },
        "current_day": {
            "date_kst": compare_data["comparison"]["current_day"]["date_kst"],
            "summary": compare_data["comparison"]["current_day"]["metrics"]["summary"],
            "cost_total": compare_data["comparison"]["current_day"]["costs"]["total_cost"],
            "cost_available": compare_data["comparison"]["current_day"]["costs"].get("cost_data_available", True),
        },
        "summary_change": compare_data["comparison"]["summary_change"],
        "top_models": top_models
    }

    system_prompt = """
너는 Azure OpenAI 비용 분석 리포트를 작성하는 FinOps 분석가다.
사용자가 제공한 JSON 데이터를 바탕으로 짧고 명확한 한국어 일일 리포트를 작성한다.

규칙:
1. 과장하지 말고 데이터에 근거해서만 작성한다.
2. 값이 0이거나 변화가 없으면 담백하게 쓴다.
3. 6문장 이내로 작성한다.
4. 날짜는 KST 기준이라고 자연스럽게 반영한다.
5. 금액 단위는 원으로 표기하되, 값이 없으면 비용 데이터 조회에 실패했다고 쓴다.
6. 토큰은 input, output, total 순서로 언급한다.
7. 모델별 정보가 있으면 변화가 큰 상위 모델 1~3개를 자연스럽게 언급한다.
8. cost_data_available가 false이거나 cost_error가 있으면 비용 데이터는 일시적으로 조회되지 않았다고 안내하고 토큰 사용량 중심으로 작성한다.
9. 문장마다 줄바꿈하기 좋게, 핵심 문장을 1문장씩 자연스럽게 끊어서 작성한다.
"""

    user_prompt = f"""
다음 JSON 데이터를 기반으로 Azure OpenAI 일일 리포트를 한국어로 작성해줘.

데이터:
{json.dumps(lightweight_data, ensure_ascii=False, indent=2)}
"""

    response = client.chat.completions.create(
        model=deployment_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.2,
        max_tokens=500
    )

    return add_line_breaks(response.choices[0].message.content.strip())


# -----------------------------
# HTML 메일
# -----------------------------
def build_model_breakdown_html(compare_data: dict[str, Any]) -> str:
    model_breakdown = compare_data["comparison"].get("model_breakdown", [])
    if not model_breakdown:
        return """
        <h3 style="margin:24px 0 8px;">모델별 토큰 비교</h3>
        <p>모델별 토큰 데이터가 없습니다.</p>
        """

    prev_day = compare_data["comparison"]["previous_day"]["date_kst"]
    curr_day = compare_data["comparison"]["current_day"]["date_kst"]

    rows_html = ""
    for item in model_breakdown:
        rows_html += f"""
        <tr>
          <td style="border:1px solid #d1d5db; padding:8px;">{item["model_name"]}</td>
          <td style="border:1px solid #d1d5db; padding:8px;">{item["model_deployment_name"]}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["previous_day"]["prompt_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["current_day"]["prompt_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["previous_day"]["completion_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["current_day"]["completion_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["previous_day"]["total_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["current_day"]["total_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["total_tokens"]["difference"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["total_tokens"]["rate_percent"], 2)}%</td>
        </tr>
        """

    return f"""
    <h3 style="margin:24px 0 8px;">모델별 토큰 비교</h3>
    <table style="border-collapse:collapse; width:100%; max-width:1100px; font-size:13px;">
      <tr style="background:#f3f4f6;">
        <th style="border:1px solid #d1d5db; padding:8px;">모델명</th>
        <th style="border:1px solid #d1d5db; padding:8px;">배포명</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{prev_day} Input</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{curr_day} Input</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{prev_day} Output</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{curr_day} Output</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{prev_day} Total</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{curr_day} Total</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Total 증감</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Total 증감률</th>
      </tr>
      {rows_html}
    </table>
    """


def build_email_html(report_text: str, compare_data: dict[str, Any]) -> str:
    prev_day = compare_data["comparison"]["previous_day"]
    curr_day = compare_data["comparison"]["current_day"]
    token_change = compare_data["comparison"]["summary_change"]["tokens"]
    cost_change = compare_data["comparison"]["summary_change"]["cost"]

    currency = curr_day["costs"].get("currency") or prev_day["costs"].get("currency") or "KRW"

    if curr_day["costs"].get("cost_data_available") is False:
        cost_section = """
        <h3 style="margin:24px 0 8px;">비용 요약</h3>
        <p style="margin:8px 0 0;">비용 데이터는 일시적으로 조회되지 않아 이번 리포트에는 포함되지 않았습니다.</p>
        """
    else:
        cost_section = f"""
        <h3 style="margin:24px 0 8px;">비용 요약</h3>
        <table style="border-collapse:collapse; width:100%; max-width:700px; font-size:13px;">
          <tr style="background:#f3f4f6;">
            <th style="border:1px solid #d1d5db; padding:8px;">항목</th>
            <th style="border:1px solid #d1d5db; padding:8px;">{prev_day["date_kst"]}</th>
            <th style="border:1px solid #d1d5db; padding:8px;">{curr_day["date_kst"]}</th>
            <th style="border:1px solid #d1d5db; padding:8px;">증감</th>
            <th style="border:1px solid #d1d5db; padding:8px;">증감률</th>
          </tr>
          <tr>
            <td style="border:1px solid #d1d5db; padding:8px;">Total Cost ({currency})</td>
            <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(prev_day["costs"]["total_cost"], 4)}</td>
            <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(curr_day["costs"]["total_cost"], 4)}</td>
            <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(cost_change["difference"], 4)}</td>
            <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(cost_change["rate_percent"], 2)}%</td>
          </tr>
        </table>
        """

    model_breakdown_section = build_model_breakdown_html(compare_data)

    html = f"""
    <html>
      <body style="font-family: Arial, 'Malgun Gothic', sans-serif; line-height:1.6; color:#222; margin:0; padding:24px; background:#ffffff;">
        <div style="max-width:1200px; margin:0 auto;">
          <h2 style="margin:0 0 12px;">[AOAI FinOps Sentinel] Azure OpenAI 일일 비용 리포트</h2>

          <p style="margin:0 0 20px;">
            <strong>비교 기준:</strong><br>
            {prev_day["date_kst"]} → {curr_day["date_kst"]} (KST)
          </p>

          <h3 style="margin:24px 0 8px;">요약</h3>
          <div style="white-space:pre-line; background:#f9fafb; border:1px solid #e5e7eb; padding:14px; border-radius:8px;">
{report_text}
          </div>

          <h3 style="margin:24px 0 8px;">전체 토큰 요약</h3>
          <table style="border-collapse:collapse; width:100%; max-width:700px; font-size:13px;">
            <tr style="background:#f3f4f6;">
              <th style="border:1px solid #d1d5db; padding:8px;">항목</th>
              <th style="border:1px solid #d1d5db; padding:8px;">{prev_day["date_kst"]}</th>
              <th style="border:1px solid #d1d5db; padding:8px;">{curr_day["date_kst"]}</th>
              <th style="border:1px solid #d1d5db; padding:8px;">증감</th>
              <th style="border:1px solid #d1d5db; padding:8px;">증감률</th>
            </tr>
            <tr>
              <td style="border:1px solid #d1d5db; padding:8px;">Input Tokens</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(prev_day["metrics"]["summary"]["prompt_tokens"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(curr_day["metrics"]["summary"]["prompt_tokens"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(token_change["prompt_tokens"]["difference"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(token_change["prompt_tokens"]["rate_percent"], 2)}%</td>
            </tr>
            <tr>
              <td style="border:1px solid #d1d5db; padding:8px;">Output Tokens</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(prev_day["metrics"]["summary"]["completion_tokens"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(curr_day["metrics"]["summary"]["completion_tokens"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(token_change["completion_tokens"]["difference"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(token_change["completion_tokens"]["rate_percent"], 2)}%</td>
            </tr>
            <tr>
              <td style="border:1px solid #d1d5db; padding:8px;">Total Tokens</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(prev_day["metrics"]["summary"]["total_tokens"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(curr_day["metrics"]["summary"]["total_tokens"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(token_change["total_tokens"]["difference"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(token_change["total_tokens"]["rate_percent"], 2)}%</td>
            </tr>
          </table>

          {model_breakdown_section}

          {cost_section}

          <p style="margin-top:28px; color:#666; font-size:12px;">
            This report was generated by AOAI FinOps Sentinel.
          </p>
        </div>
      </body>
    </html>
    """
    return html


# -----------------------------
# 메일 발송
# -----------------------------
def send_email(subject: str, html_body: str) -> dict[str, Any]:
    smtp_host = get_env("SMTP_HOST")
    smtp_port = int(get_env("SMTP_PORT"))
    smtp_username = get_env("SMTP_USERNAME")
    smtp_password = get_env("SMTP_PASSWORD")
    mail_from = get_env("MAIL_FROM")
    mail_to_raw = get_env("MAIL_TO")

    recipients = [x.strip() for x in mail_to_raw.split(",") if x.strip()]
    if not recipients:
        raise ValueError("MAIL_TO에 수신자가 없습니다.")

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = ", ".join(recipients)

    message.attach(MIMEText(html_body, "html", "utf-8"))

    logging.info("SMTP send start. host=%s port=%s to=%s", smtp_host, smtp_port, recipients)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(mail_from, recipients, message.as_string())

    logging.info("SMTP send success. subject=%s", subject)

    return {
        "success": True,
        "recipients": recipients,
        "subject": subject
    }


# -----------------------------
# 실행
# -----------------------------
def execute_daily_report_send() -> dict[str, Any]:
    compare_data = build_daily_compare_data()
    report_text = generate_report_text(compare_data)
    html_body = build_email_html(report_text, compare_data)

    current_day = compare_data["comparison"]["current_day"]["date_kst"]
    subject = f"[AOAI FinOps Sentinel] Azure OpenAI 일일 리포트 - {current_day}"

    send_result = send_email(subject, html_body)

    return {
        "message": "메일 발송 완료",
        "send_result": send_result,
        "report_text": report_text,
        "compare_data": compare_data
    }


# -----------------------------
# 테스트/운영용 함수
# -----------------------------
@app.route(route="daily_compare", methods=["GET"])
def daily_compare(req: func.HttpRequest) -> func.HttpResponse:
    try:
        result = build_daily_compare_data()
        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        logging.exception("daily_compare failed")
        return func.HttpResponse(str(e), status_code=500, mimetype="text/plain")


@app.route(route="daily_report_preview", methods=["GET"])
def daily_report_preview(req: func.HttpRequest) -> func.HttpResponse:
    try:
        compare_data = build_daily_compare_data()
        report_text = generate_report_text(compare_data)
        html_body = build_email_html(report_text, compare_data)

        result = {
            "report_text": report_text,
            "source_data": compare_data,
            "html_preview": html_body
        }

        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        logging.exception("daily_report_preview failed")
        return func.HttpResponse(str(e), status_code=500, mimetype="text/plain")


@app.route(route="daily_report_send", methods=["GET"])
def daily_report_send(req: func.HttpRequest) -> func.HttpResponse:
    try:
        result = execute_daily_report_send()
        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        logging.exception("daily_report_send failed")
        return func.HttpResponse(str(e), status_code=500, mimetype="text/plain")


@app.route(route="monthly_cost_test", methods=["GET"])
def monthly_cost_test(req: func.HttpRequest) -> func.HttpResponse:
    try:
        resources = load_resources()
        credential = DefaultAzureCredential()
        subscription_id = get_env("SUBSCRIPTION_ID")
        resource_ids = [x["resource_id"] for x in resources]

        result = fetch_current_month_costs(
            credential=credential,
            subscription_id=subscription_id,
            resource_ids=resource_ids
        )

        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        logging.exception("monthly_cost_test failed")
        return func.HttpResponse(str(e), status_code=500, mimetype="text/plain")


@app.function_name(name="daily_report_timer")
@app.schedule(
    schedule="%DAILY_REPORT_SCHEDULE%",
    arg_name="mytimer",
    run_on_startup=False,
    use_monitor=True
)
def daily_report_timer(mytimer: func.TimerRequest) -> None:
    logging.info("daily_report_timer started. past_due=%s", mytimer.past_due if mytimer else None)

    try:
        result = execute_daily_report_send()
        logging.info(
            "daily_report_timer success: %s",
            json.dumps(
                {
                    "subject": result["send_result"]["subject"],
                    "recipients": result["send_result"]["recipients"]
                },
                ensure_ascii=False
            )
        )
    except Exception:
        logging.exception("daily_report_timer failed")
        raise
    