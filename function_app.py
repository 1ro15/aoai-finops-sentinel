import json
import logging
import os
import re
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
def get_env(name: str, required: bool = True) -> str | None:
    value = os.getenv(name)
    if required and not value:
        raise ValueError(f"환경 변수 누락: {name}")
    return value


def format_number(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return f"{value:,}"


def format_cost_text(value: float | None, currency: str | None = "KRW") -> str:
    if value is None:
        return "-"
    unit = currency or "KRW"
    return f"{value:,.4f} {unit}"


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


def find_column_index(columns: list[dict[str, Any]], *candidates: str) -> int | None:
    normalized: dict[str, int] = {}
    for idx, col in enumerate(columns):
        name = str(col.get("name", "")).strip().lower()
        normalized[name] = idx

    for candidate in candidates:
        idx = normalized.get(candidate.lower())
        if idx is not None:
            return idx
    return None


# -----------------------------
# 모델명 매핑
# -----------------------------
def load_deployment_model_map() -> dict[str, str]:
    raw = get_env("AOAI_DEPLOYMENT_MODEL_MAP", required=False)
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"AOAI_DEPLOYMENT_MODEL_MAP JSON 파싱 실패: {e}")

    if not isinstance(data, dict):
        raise ValueError("AOAI_DEPLOYMENT_MODEL_MAP는 JSON 객체여야 합니다.")

    result: dict[str, str] = {}
    for k, v in data.items():
        deployment = str(k).strip()
        model_name = str(v).strip()
        if deployment:
            result[deployment] = model_name
    return result


def resolve_model_name(deployment_name: str, deployment_model_map: dict[str, str]) -> tuple[str, str]:
    normalized_deployment = normalize_dimension_value(deployment_name)

    mapped = deployment_model_map.get(normalized_deployment)
    if mapped:
        return mapped, "deployment_model_map"

    return normalized_deployment, "deployment_name_fallback"


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
    deployment_model_map: dict[str, str],
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
        raise RuntimeError(
            f"Metrics API 호출 실패: metric={metric_name}, status={response.status_code}, body={response.text}"
        )

    data = response.json()
    rows: list[dict[str, Any]] = []

    for metric in data.get("value", []) or []:
        metric_name_value = ((metric.get("name") or {}).get("value")) or metric_name

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

            model_name, resolution_source = resolve_model_name(deployment, deployment_model_map)

            rows.append({
                "metric_name": metric_name_value,
                "model_deployment_name": deployment,
                "model_name": model_name,
                "raw_dimensions": metadata,
                "model_resolution_source": resolution_source,
                "total": total_value,
            })

    return rows


def query_all_metrics_for_resource(
    credential: DefaultAzureCredential,
    resource: dict[str, str],
    start_time_utc: datetime,
    end_time_utc: datetime,
    deployment_model_map: dict[str, str],
) -> list[dict[str, Any]]:
    metric_names = [
        "ProcessedPromptTokens",
        "GeneratedTokens",
        "TokenTransaction",
        "AzureOpenAIRequests",
    ]

    all_rows: list[dict[str, Any]] = []

    for metric_name in metric_names:
        metric_rows = query_metric_split_by_deployment(
            credential=credential,
            resource_id=resource["resource_id"],
            metric_name=metric_name,
            start_time_utc=start_time_utc,
            end_time_utc=end_time_utc,
            deployment_model_map=deployment_model_map,
        )

        for row in metric_rows:
            all_rows.append({
                "resource_id": resource["resource_id"],
                "region": resource["region"],
                "metric_name": row["metric_name"],
                "model_deployment_name": row["model_deployment_name"],
                "model_name": row["model_name"],
                "raw_dimensions": row["raw_dimensions"],
                "model_resolution_source": row["model_resolution_source"],
                "total": row["total"],
            })

    return all_rows


def metric_name_to_field(metric_name: str) -> str:
    mapping = {
        "ProcessedPromptTokens": "prompt_tokens",
        "GeneratedTokens": "completion_tokens",
        "TokenTransaction": "total_tokens",
        "AzureOpenAIRequests": "request_count",
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
                "request_count": 0,
                "raw_dimensions": {"deployment_dimension": {}},
                "model_resolution_source": row.get("model_resolution_source", "deployment_name_fallback"),
            }

        field_name = metric_name_to_field(row["metric_name"])
        if field_name in ("prompt_tokens", "completion_tokens", "total_tokens", "request_count"):
            grouped[key][field_name] += row["total"]

        grouped[key]["raw_dimensions"]["deployment_dimension"][row["metric_name"]] = row.get("raw_dimensions", {})

        if grouped[key]["model_resolution_source"] != "deployment_model_map":
            grouped[key]["model_resolution_source"] = row.get("model_resolution_source", grouped[key]["model_resolution_source"])

    return list(grouped.values())


def aggregate_by_model(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for item in items:
        model_name = item.get("model_name", "unknown")

        if model_name not in grouped:
            grouped[model_name] = {
                "model_name": model_name,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "request_count": 0,
                "resource_count": 0,
                "regions": set(),
                "deployments": set(),
                "resources": set(),
                "resolution_sources": set(),
            }

        grouped_model = grouped[model_name]
        grouped_model["prompt_tokens"] += item.get("prompt_tokens", 0)
        grouped_model["completion_tokens"] += item.get("completion_tokens", 0)
        grouped_model["total_tokens"] += item.get("total_tokens", 0)
        grouped_model["request_count"] += item.get("request_count", 0)
        grouped_model["regions"].add(item.get("region", "unknown"))
        grouped_model["deployments"].add(item.get("model_deployment_name", "unknown"))
        grouped_model["resources"].add(item.get("resource_id", "unknown"))
        grouped_model["resolution_sources"].add(item.get("model_resolution_source", "deployment_name_fallback"))

    results: list[dict[str, Any]] = []
    for value in grouped.values():
        value["regions"] = sorted(value["regions"])
        value["deployments"] = sorted(value["deployments"])
        value["resources"] = sorted(value["resources"])
        value["resource_count"] = len(value["resources"])
        value["resolution_sources"] = sorted(value["resolution_sources"])
        results.append(value)

    results.sort(key=lambda x: (-(x.get("total_tokens", 0) or 0), x["model_name"]))
    return results


def sum_items(items: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "prompt_tokens": sum(x.get("prompt_tokens", 0) for x in items),
        "completion_tokens": sum(x.get("completion_tokens", 0) for x in items),
        "total_tokens": sum(x.get("total_tokens", 0) for x in items),
        "request_count": sum(x.get("request_count", 0) for x in items),
    }


def fetch_day_metrics(
    credential: DefaultAzureCredential,
    resources: list[dict[str, str]],
    days_ago: int,
    deployment_model_map: dict[str, str],
) -> dict[str, Any]:
    start_utc, end_utc, target_date_kst = get_kst_day_range_to_utc(days_ago)

    all_rows: list[dict[str, Any]] = []

    for resource in resources:
        rows = query_all_metrics_for_resource(
            credential=credential,
            resource=resource,
            start_time_utc=start_utc,
            end_time_utc=end_utc,
            deployment_model_map=deployment_model_map,
        )
        all_rows.extend(rows)

    normalized = normalize_rows(all_rows)
    model_summary = aggregate_by_model(normalized)
    summary = sum_items(normalized)

    return {
        "target_date_kst": target_date_kst,
        "start_time_utc": start_utc.isoformat(),
        "end_time_utc": end_utc.isoformat(),
        "items": normalized,
        "model_summary": model_summary,
        "summary": summary,
    }
def parse_cost_response(data: dict[str, Any], target_ids: list[str], fallback_date: str) -> dict[str, Any]:
    properties = data.get("properties", {})
    rows = properties.get("rows", [])
    columns = properties.get("columns", [])

    resource_id_idx = find_column_index(columns, "ResourceId")
    total_cost_idx = find_column_index(columns, "totalCost", "PreTaxCost")
    currency_idx = find_column_index(columns, "Currency")
    usage_date_idx = find_column_index(columns, "UsageDate")

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



def normalize_cost_usage_date(value: Any) -> str:
    """
    Cost Management API의 UsageDate 값을 YYYY-MM-DD 문자열로 정규화합니다.
    예: 20260501 -> 2026-05-01
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 8 and text.isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return text


def map_cost_usage_date_to_kst_period(
    usage_date: str,
    first_usage_date: str,
    start_kst_str: str,
    end_kst_str: str,
) -> str:
    """
    Cost Management Daily 결과는 UTC 일자 기준 UsageDate로 내려올 수 있습니다.

    예를 들어 KST 기준 2026-05-01 ~ 2026-05-31 기간은 UTC 기준으로
    2026-04-30 15:00 ~ 2026-05-31 15:00 이므로 Cost API daily 결과에
    2026-04-30 행이 포함될 수 있습니다.

    월간/기간 리포트 표시는 사용자가 요청한 KST 기간 기준이어야 하므로,
    첫 번째 Cost UsageDate를 KST 시작일에 맞추고 이후 날짜는 순서대로 보정합니다.
    마지막에 UTC 부분일 때문에 범위를 초과하는 행은 KST 종료일에 합산합니다.
    """
    try:
        if not usage_date:
            return start_kst_str

        start_kst_date = datetime.fromisoformat(start_kst_str).date()
        end_kst_date = datetime.fromisoformat(end_kst_str).date()
        first_date = datetime.fromisoformat(first_usage_date).date()
        current_date = datetime.fromisoformat(usage_date).date()

        offset_days = (current_date - first_date).days
        mapped_date = start_kst_date + timedelta(days=offset_days)

        if mapped_date < start_kst_date:
            mapped_date = start_kst_date
        if mapped_date > end_kst_date:
            mapped_date = end_kst_date

        return mapped_date.isoformat()
    except Exception:
        return usage_date or start_kst_str


def fetch_day_costs(
    credential: DefaultAzureCredential,
    subscription_id: str,
    resource_ids: list[str],
    days_ago: int,
    retry_delays: list[int] | None = None,
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

    # Daily report는 메일 발송이 우선입니다.
    # Cost API가 429로 제한되면 긴 대기 대신 짧게 1회만 재시도하고,
    # 실패 시 build_daily_compare_data()에서 비용 제외 리포트로 전환합니다.
    if retry_delays is None:
        retry_delays = [5]

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
            # 일부 환경에서는 Daily granularity여도 resource/date 조합으로 여러 행이 내려올 수 있어
            # 렌더링 전에 날짜 기준으로 다시 합산한다.
            "grouping": [
                {
                    "type": "Dimension",
                    "name": "ResourceId"
                }
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

    total_cost_idx = find_column_index(columns, "totalCost", "PreTaxCost")
    currency_idx = find_column_index(columns, "Currency")
    usage_date_idx = find_column_index(columns, "UsageDate", "BillingMonth", "Date")

    total_cost = 0.0
    currency = None

    raw_cost_rows: list[dict[str, Any]] = []

    for row in rows:
        cost_value = float(row[total_cost_idx]) if total_cost_idx is not None else 0.0
        currency = row[currency_idx] if currency_idx is not None else currency
        usage_date = normalize_cost_usage_date(row[usage_date_idx]) if usage_date_idx is not None else ""
        raw_cost_rows.append({"usage_date": usage_date, "cost": cost_value, "currency": currency})
        total_cost += cost_value

    usage_dates = sorted({r.get("usage_date") for r in raw_cost_rows if r.get("usage_date")})
    first_usage_date = usage_dates[0] if usage_dates else start_kst_str

    daily_cost_map: dict[str, float] = {}
    for row in raw_cost_rows:
        date_kst = map_cost_usage_date_to_kst_period(
            usage_date=row.get("usage_date", ""),
            first_usage_date=first_usage_date,
            start_kst_str=start_kst_str,
            end_kst_str=end_kst_str,
        )
        daily_cost_map[date_kst] = daily_cost_map.get(date_kst, 0.0) + float(row.get("cost", 0.0) or 0.0)

    daily_rows = [
        {
            "date": date_key,
            "date_kst": date_key,
            "cost": cost_value,
            "currency": currency
        }
        for date_key, cost_value in sorted(daily_cost_map.items(), key=lambda x: x[0] or "")
    ]

    return {
        "period_kst": f"{start_kst_str} ~ {end_kst_str}",
        "currency": currency,
        "total_cost": total_cost,
        "daily_rows": daily_rows
    }


# -----------------------------
# 모델별 비교
# -----------------------------
# -----------------------------
# 모델별 비교
# -----------------------------
def build_model_key(item: dict[str, Any]) -> tuple:
    return (item.get("model_name", "unknown"),)


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
            "request_count": prev.get("request_count", 0),
        }
        current_day = {
            "prompt_tokens": curr.get("prompt_tokens", 0),
            "completion_tokens": curr.get("completion_tokens", 0),
            "total_tokens": curr.get("total_tokens", 0),
            "request_count": curr.get("request_count", 0),
        }

        result.append({
            "model_name": key[0],
            "previous_day": previous_day,
            "current_day": current_day,
            "regions": sorted(set(prev.get("regions", [])) | set(curr.get("regions", []))),
            "deployments": sorted(set(prev.get("deployments", [])) | set(curr.get("deployments", []))),
            "resource_count": max(prev.get("resource_count", 0), curr.get("resource_count", 0)),
            "resolution_sources": sorted(set(prev.get("resolution_sources", [])) | set(curr.get("resolution_sources", []))),
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
                "request_count": calculate_change(
                    current_day["request_count"], previous_day["request_count"]
                ),
            }
        })

    result.sort(
        key=lambda x: (
            -(x["current_day"]["total_tokens"] or 0),
            x["model_name"],
        )
    )

    return result


def build_deployment_breakdown(
    previous_items: list[dict[str, Any]],
    current_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    def deployment_key(item: dict[str, Any]) -> tuple:
        return (
            item.get("resource_id", "unknown"),
            item.get("region", "unknown"),
            item.get("model_name", "unknown"),
            item.get("model_deployment_name", "unknown"),
        )

    previous_map = {deployment_key(item): item for item in previous_items}
    current_map = {deployment_key(item): item for item in current_items}

    all_keys = set(previous_map.keys()) | set(current_map.keys())
    result = []

    for key in all_keys:
        prev = previous_map.get(key, {})
        curr = current_map.get(key, {})

        previous_day = {
            "prompt_tokens": prev.get("prompt_tokens", 0),
            "completion_tokens": prev.get("completion_tokens", 0),
            "total_tokens": prev.get("total_tokens", 0),
            "request_count": prev.get("request_count", 0),
        }
        current_day = {
            "prompt_tokens": curr.get("prompt_tokens", 0),
            "completion_tokens": curr.get("completion_tokens", 0),
            "total_tokens": curr.get("total_tokens", 0),
            "request_count": curr.get("request_count", 0),
        }

        result.append({
            "resource_id": key[0],
            "region": key[1],
            "model_name": key[2],
            "model_deployment_name": key[3],
            "model_resolution_source": curr.get("model_resolution_source") or prev.get("model_resolution_source"),
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
                "request_count": calculate_change(
                    current_day["request_count"], previous_day["request_count"]
                ),
            }
        })

    result.sort(
        key=lambda x: (
            x["region"],
            -(x["current_day"]["total_tokens"] or 0),
            x["model_name"],
            x["model_deployment_name"],
        )
    )

    return result


# -----------------------------
# 데이터 통합
# -----------------------------
def build_daily_compare_data() -> dict[str, Any]:
    resources = load_resources()
    deployment_model_map = load_deployment_model_map()
    credential = DefaultAzureCredential()
    subscription_id = get_env("SUBSCRIPTION_ID")

    resource_ids = [x["resource_id"] for x in resources]

    d5_metrics = fetch_day_metrics(
        credential=credential,
        resources=resources,
        days_ago=5,
        deployment_model_map=deployment_model_map,
    )
    d4_metrics = fetch_day_metrics(
        credential=credential,
        resources=resources,
        days_ago=4,
        deployment_model_map=deployment_model_map,
    )

    model_breakdown = build_model_breakdown(
        d5_metrics["model_summary"],
        d4_metrics["model_summary"]
    )
    deployment_breakdown = build_deployment_breakdown(
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
        "request_count": calculate_change(
            d4_metrics["summary"]["request_count"], d5_metrics["summary"]["request_count"]
        ),
    }

    return {
        "timezone": "KST",
        "resource_count": len(resources),
        "deployment_model_map_count": len(deployment_model_map),
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
            "model_breakdown": model_breakdown,
            "deployment_breakdown": deployment_breakdown
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

    prev_costs = compare_data["comparison"]["previous_day"]["costs"]
    curr_costs = compare_data["comparison"]["current_day"]["costs"]
    cost_change = compare_data["comparison"]["summary_change"]["cost"]

    cost_currency = curr_costs.get("currency") or prev_costs.get("currency") or "KRW"

    lightweight_data = {
        "timezone": compare_data["timezone"],
        "resource_count": compare_data["resource_count"],
        "deployment_model_map_count": compare_data.get("deployment_model_map_count", 0),
        "cost_error": compare_data.get("cost_error"),
        "previous_day": {
            "date_kst": compare_data["comparison"]["previous_day"]["date_kst"],
            "summary": compare_data["comparison"]["previous_day"]["metrics"]["summary"],
            "model_summary": compare_data["comparison"]["previous_day"]["metrics"].get("model_summary", [])[:10],
            "cost_total_text": format_cost_text(prev_costs.get("total_cost"), cost_currency),
            "cost_available": prev_costs.get("cost_data_available", True),
        },
        "current_day": {
            "date_kst": compare_data["comparison"]["current_day"]["date_kst"],
            "summary": compare_data["comparison"]["current_day"]["metrics"]["summary"],
            "model_summary": compare_data["comparison"]["current_day"]["metrics"].get("model_summary", [])[:10],
            "cost_total_text": format_cost_text(curr_costs.get("total_cost"), cost_currency),
            "cost_available": curr_costs.get("cost_data_available", True),
        },
        "summary_change": {
            "tokens": compare_data["comparison"]["summary_change"]["tokens"],
            "cost": {
                "difference_text": format_cost_text(cost_change.get("difference"), cost_currency),
                "rate_percent": cost_change.get("rate_percent")
            }
        },
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
5. 비용은 반드시 사용자가 준 문자열(cost_total_text, difference_text)을 그대로 사용한다.
6. 비용 문자열을 원 단위 정수로 다시 변환하거나 천 단위로 재해석하지 않는다.
7. 토큰은 input, output, total 순서로 언급하고, 요청 수가 있으면 함께 간단히 언급한다.
8. 모델별 정보가 있으면 canonical model 기준 상위 모델 1~3개를 자연스럽게 언급한다.
9. cost_data_available가 false이거나 cost_error가 있으면 비용 데이터는 일시적으로 조회되지 않았다고 안내하고 토큰/요청 사용량 중심으로 작성한다.
10. 문장마다 줄바꿈하기 좋게 핵심 문장을 1문장씩 자연스럽게 끊어서 작성한다.
11. 같은 모델이 여러 리전이나 여러 deployment에서 합산되었을 수 있음을 모델 요약에 자연스럽게 반영할 수 있다.
"""

    user_prompt = f"""
다음 JSON 데이터를 기반으로 Azure OpenAI 일일 리포트를 한국어로 작성해줘.

중요:
- 비용은 cost_total_text 와 difference_text 값을 그대로 사용해.
- 예: "2.1503 KRW" 를 "2,150원" 으로 바꾸면 안 돼.
- model_summary와 top_models는 deployment가 아니라 canonical model 기준 집계다.
- summary 안의 request_count는 Azure OpenAI Requests 메트릭 기반 요청 수다.

데이터:
{json.dumps(lightweight_data, ensure_ascii=False, indent=2)}
"""

    response = client.chat.completions.create(
        model=deployment_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.1,
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
        <h3 style="margin:24px 0 8px;">모델별 토큰 및 요청 비교 (모델 기준 통합)</h3>
        <p>모델별 토큰 데이터가 없습니다.</p>
        """

    prev_day = compare_data["comparison"]["previous_day"]["date_kst"]
    curr_day = compare_data["comparison"]["current_day"]["date_kst"]

    rows_html = ""
    for item in model_breakdown:
        regions = ", ".join(item.get("regions", [])) or "-"
        deployments = ", ".join(item.get("deployments", [])) or "-"
        rows_html += f"""
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb; width:140px;">모델명</th>
          <td style="border:1px solid #d1d5db; padding:8px; font-weight:600;">{item["model_name"]}</td>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb; width:120px;">리전</th>
          <td style="border:1px solid #d1d5db; padding:8px;">{regions}</td>
        </tr>
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">포함된 배포명</th>
          <td colspan="3" style="border:1px solid #d1d5db; padding:8px;">{deployments}</td>
        </tr>
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">{prev_day} Input</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["previous_day"]["prompt_tokens"])}</td>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">{curr_day} Input</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["current_day"]["prompt_tokens"])}</td>
        </tr>
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">Input 증감</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["prompt_tokens"]["difference"])}</td>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">Input 증감률</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["prompt_tokens"]["rate_percent"], 2)}%</td>
        </tr>
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">{prev_day} Output</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["previous_day"]["completion_tokens"])}</td>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">{curr_day} Output</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["current_day"]["completion_tokens"])}</td>
        </tr>
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">Output 증감</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["completion_tokens"]["difference"])}</td>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">Output 증감률</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["completion_tokens"]["rate_percent"], 2)}%</td>
        </tr>
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">{prev_day} Total</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["previous_day"]["total_tokens"])}</td>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">{curr_day} Total</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["current_day"]["total_tokens"])}</td>
        </tr>
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">Total 증감</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["total_tokens"]["difference"])}</td>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">Total 증감률</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["total_tokens"]["rate_percent"], 2)}%</td>
        </tr>
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">{prev_day} Requests</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["previous_day"]["request_count"])}</td>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">{curr_day} Requests</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["current_day"]["request_count"])}</td>
        </tr>
        <tr>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">Requests 증감</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["request_count"]["difference"])}</td>
          <th style="border:1px solid #d1d5db; padding:8px; background:#f9fafb;">Requests 증감률</th>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["request_count"]["rate_percent"], 2)}%</td>
        </tr>
        <tr><td colspan="4" style="border:none; height:12px;"></td></tr>
        """

    return f"""
    <h3 style="margin:24px 0 8px;">모델별 토큰 및 요청 비교 (모델 기준 통합)</h3>
    <table style="border-collapse:collapse; width:auto; min-width:420px; font-size:13px; table-layout:auto;">
      {rows_html}
    </table>
    """


def build_deployment_breakdown_html(compare_data: dict[str, Any]) -> str:
    deployment_breakdown = compare_data["comparison"].get("deployment_breakdown", [])
    if not deployment_breakdown:
        return """
        <h3 style="margin:24px 0 8px;">리전/배포별 토큰 및 요청 비교</h3>
        <p>리전/배포별 데이터가 없습니다.</p>
        """

    prev_day = compare_data["comparison"]["previous_day"]["date_kst"]
    curr_day = compare_data["comparison"]["current_day"]["date_kst"]

    rows_html = ""
    for item in deployment_breakdown:
        rows_html += f"""
        <tr>
          <td style="border:1px solid #d1d5db; padding:8px;">{item["region"]}</td>
          <td style="border:1px solid #d1d5db; padding:8px;">{item["model_name"]}</td>
          <td style="border:1px solid #d1d5db; padding:8px;">{item["model_deployment_name"]}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["previous_day"]["total_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["current_day"]["total_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["total_tokens"]["difference"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["total_tokens"]["rate_percent"], 2)}%</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["previous_day"]["request_count"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["current_day"]["request_count"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["request_count"]["difference"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["change"]["request_count"]["rate_percent"], 2)}%</td>
        </tr>
        """

    return f"""
    <h3 style="margin:24px 0 8px;">리전/배포별 토큰 및 요청 비교</h3>
    <table style="border-collapse:collapse; width:100%; max-width:1400px; font-size:13px;">
      <tr style="background:#f3f4f6;">
        <th style="border:1px solid #d1d5db; padding:8px;">리전</th>
        <th style="border:1px solid #d1d5db; padding:8px;">모델명</th>
        <th style="border:1px solid #d1d5db; padding:8px;">배포명</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{prev_day} Total</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{curr_day} Total</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Total 증감</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Total 증감률</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{prev_day} Requests</th>
        <th style="border:1px solid #d1d5db; padding:8px;">{curr_day} Requests</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Requests 증감</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Requests 증감률</th>
      </tr>
      {rows_html}
    </table>
    """


def build_email_html(report_text: str, compare_data: dict[str, Any]) -> str:
    prev_day = compare_data["comparison"]["previous_day"]
    curr_day = compare_data["comparison"]["current_day"]
    token_change = compare_data["comparison"]["summary_change"]["tokens"]
    request_change = token_change["request_count"]
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
            <th style="border:1px solid #d1d5db; padding:8px; min-width:180px; white-space:nowrap;">항목</th>
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
    deployment_breakdown_section = build_deployment_breakdown_html(compare_data)

    html = f"""
    <html>
      <body style="font-family:Arial, sans-serif; color:#111; line-height:1.6;">
        <div style="max-width:1200px; margin:0 auto; padding:24px;">
          <h2 style="margin:0 0 12px;">AOAI FinOps Sentinel 일일 리포트</h2>
          <p style="margin:0 0 24px; color:#555;">
            기준 일자: {curr_day["date_kst"]} (KST)
          </p>

          <h3 style="margin:0 0 8px;">요약</h3>
          <p style="white-space:pre-wrap; margin:0 0 20px;">{report_text}</p>

          <h3 style="margin:24px 0 8px;">토큰 요약</h3>
          <table style="border-collapse:collapse; width:100%; max-width:700px; font-size:13px;">
            <tr style="background:#f3f4f6;">
              <th style="border:1px solid #d1d5db; padding:8px; min-width:180px; white-space:nowrap;">항목</th>
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

          <h3 style="margin:24px 0 8px;">요청 수 요약</h3>
          <table style="border-collapse:collapse; width:100%; max-width:700px; font-size:13px;">
            <tr style="background:#f3f4f6;">
              <th style="border:1px solid #d1d5db; padding:8px; min-width:180px; white-space:nowrap;">항목</th>
              <th style="border:1px solid #d1d5db; padding:8px;">{prev_day["date_kst"]}</th>
              <th style="border:1px solid #d1d5db; padding:8px;">{curr_day["date_kst"]}</th>
              <th style="border:1px solid #d1d5db; padding:8px;">증감</th>
              <th style="border:1px solid #d1d5db; padding:8px;">증감률</th>
            </tr>
            <tr>
              <td style="border:1px solid #d1d5db; padding:8px;">Requests</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(prev_day["metrics"]["summary"]["request_count"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(curr_day["metrics"]["summary"]["request_count"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(request_change["difference"])}</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(request_change["rate_percent"], 2)}%</td>
            </tr>
          </table>

          {model_breakdown_section}
          {deployment_breakdown_section}
          {cost_section}

          <p style="margin-top:28px; color:#666; font-size:12px;">
            This report was generated by AOAI FinOps Sentinel.
          </p>
        </div>
      </body>
    </html>
    """
    return html


def resolve_mail_recipients(target_keyword: str | None = None) -> list[str]:
    """
    MAIL_TO 환경 변수에 등록된 수신자 목록에서 선택 발송 대상을 결정합니다.

    - target_keyword가 없으면 MAIL_TO 전체에게 발송
    - target_keyword가 있으면 이메일 주소 또는 @ 앞 local-part에 해당 문자열이 포함된 수신자에게만 발송
      예: MAIL_TO = aaaa@gmail.com,aaaa@naver.com,bbbb@naver.com
          사용자 질의 = "aaaa로 메일 보내줘"
          결과 = aaaa@gmail.com, aaaa@naver.com
    """
    mail_to_raw = get_env("MAIL_TO")
    recipients = [addr.strip() for addr in mail_to_raw.split(",") if addr.strip()]

    if not recipients:
        raise ValueError("MAIL_TO에 유효한 수신자가 없습니다.")

    if not target_keyword:
        return recipients

    keyword = target_keyword.strip().lower()
    matched = []

    for addr in recipients:
        addr_lower = addr.lower()
        local_part = addr_lower.split("@")[0]
        if keyword in local_part or keyword in addr_lower:
            matched.append(addr)

    if not matched:
        raise ValueError(
            f"'{target_keyword}' 조건에 맞는 수신자를 MAIL_TO에서 찾지 못했습니다. "
            "등록된 메일 주소의 @ 앞 계정명 또는 전체 주소에 포함된 문자열로 요청해주세요."
        )

    return matched


def extract_mail_target_keyword(message: str) -> str | None:
    """
    'aaaa로 메일 보내줘', 'aaaa에게 이메일 발송', 'aaaa한테 메일' 같은 표현에서
    선택 발송 대상 키워드를 추출합니다.
    """
    patterns = [
        r"([A-Za-z0-9._+-]+)\s*(?:에게|한테|로|으로)\s*(?:메일|이메일|mail|email)",
        r"(?:메일|이메일|mail|email)\s*(?:을|를)?\s*([A-Za-z0-9._+-]+)\s*(?:에게|한테|로|으로)",
    ]

    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            keyword = match.group(1).strip()
            # 너무 일반적인 단어가 잡히는 것 방지
            if keyword.lower() not in ["메일", "이메일", "mail", "email"]:
                return keyword

    return None


def send_email(subject: str, html_body: str, recipients_override: list[str] | None = None) -> dict[str, Any]:
    mail_from = get_env("MAIL_FROM")
    smtp_host = get_env("SMTP_HOST")
    smtp_port = int(get_env("SMTP_PORT"))
    smtp_username = get_env("SMTP_USERNAME")
    smtp_password = get_env("SMTP_PASSWORD")

    recipients = recipients_override if recipients_override else resolve_mail_recipients()

    if not recipients:
        raise ValueError("유효한 수신자가 없습니다.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except Exception:
                logging.info("SMTP STARTTLS not available. proceeding without TLS.")

            server.login(smtp_username, smtp_password)
            server.sendmail(mail_from, recipients, msg.as_string())
    except Exception as e:
        logging.exception("send_email failed")
        raise RuntimeError(f"메일 발송 실패: {e}")

    return {
        "status": "sent",
        "from": mail_from,
        "to": recipients,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
    }

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


@app.route(route="model_rollup_debug", methods=["GET"])
def model_rollup_debug(req: func.HttpRequest) -> func.HttpResponse:
    try:
        days_ago = int(req.params.get("days_ago", "4"))
        resources = load_resources()
        deployment_model_map = load_deployment_model_map()
        credential = DefaultAzureCredential()

        metrics = fetch_day_metrics(
            credential=credential,
            resources=resources,
            days_ago=days_ago,
            deployment_model_map=deployment_model_map,
        )

        result = {
            "days_ago": days_ago,
            "target_date_kst": metrics["target_date_kst"],
            "summary": metrics["summary"],
            "model_summary": metrics["model_summary"],
            "deployment_items": metrics["items"],
            "deployment_model_map": deployment_model_map,
        }

        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        logging.exception("model_rollup_debug failed")
        return func.HttpResponse(str(e), status_code=500, mimetype="text/plain")


@app.route(route="request_metric_debug", methods=["GET"])
def request_metric_debug(req: func.HttpRequest) -> func.HttpResponse:
    try:
        days_ago = int(req.params.get("days_ago", "4"))
        resources = load_resources()
        deployment_model_map = load_deployment_model_map()
        credential = DefaultAzureCredential()

        metric_name = req.params.get("metric", "AzureOpenAIRequests")
        start_utc, end_utc, target_date_kst = get_kst_day_range_to_utc(days_ago)

        debug_resources = []
        for resource in resources:
            rows = query_metric_split_by_deployment(
                credential=credential,
                resource_id=resource["resource_id"],
                metric_name=metric_name,
                start_time_utc=start_utc,
                end_time_utc=end_utc,
                deployment_model_map=deployment_model_map,
            )
            debug_resources.append({
                "resource_id": resource["resource_id"],
                "region": resource["region"],
                "items": rows,
            })

        return func.HttpResponse(
            json.dumps(
                {
                    "days_ago": days_ago,
                    "target_date_kst": target_date_kst,
                    "metric_name": metric_name,
                    "debug_resources": debug_resources,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.exception("request_metric_debug failed")
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
                    "message": result.get("message"),
                    "to": result.get("send_result", {}).get("to"),
                    "smtp_host": result.get("send_result", {}).get("smtp_host"),
                    "smtp_port": result.get("send_result", {}).get("smtp_port"),
                },
                ensure_ascii=False
            )
        )
    except Exception:
        logging.exception("daily_report_timer failed")
        raise


# -----------------------------
# 월간 리포트용 기간 유틸
# -----------------------------
def get_previous_month_range_to_utc() -> tuple[datetime, datetime, str, str, str]:
    now_kst = datetime.now(KST)
    this_month_start_kst = datetime(now_kst.year, now_kst.month, 1, 0, 0, 0, tzinfo=KST)
    prev_month_end_kst = this_month_start_kst - timedelta(seconds=1)
    prev_month_start_kst = datetime(prev_month_end_kst.year, prev_month_end_kst.month, 1, 0, 0, 0, tzinfo=KST)

    period_label = f"{prev_month_start_kst.strftime('%Y-%m')} 월간"
    return (
        prev_month_start_kst.astimezone(timezone.utc),
        this_month_start_kst.astimezone(timezone.utc),
        prev_month_start_kst.strftime("%Y-%m-%d"),
        prev_month_end_kst.strftime("%Y-%m-%d"),
        period_label,
    )


def fetch_metrics_for_custom_range(
    credential: DefaultAzureCredential,
    resources: list[dict[str, str]],
    start_time_utc: datetime,
    end_time_utc: datetime,
    deployment_model_map: dict[str, str],
) -> dict[str, Any]:
    all_rows: list[dict[str, Any]] = []

    for resource in resources:
        rows = query_all_metrics_for_resource(
            credential=credential,
            resource=resource,
            start_time_utc=start_time_utc,
            end_time_utc=end_time_utc,
            deployment_model_map=deployment_model_map,
        )
        all_rows.extend(rows)

    normalized = normalize_rows(all_rows)
    model_summary = aggregate_by_model(normalized)
    summary = sum_items(normalized)

    return {
        "start_time_utc": start_time_utc.isoformat(),
        "end_time_utc": end_time_utc.isoformat(),
        "items": normalized,
        "model_summary": model_summary,
        "summary": summary,
    }


def fetch_costs_for_custom_range(
    credential: DefaultAzureCredential,
    subscription_id: str,
    resource_ids: list[str],
    start_time_utc: datetime,
    end_time_utc: datetime,
    period_label: str,
    start_kst_str: str,
    end_kst_str: str,
) -> dict[str, Any]:
    token = credential.get_token("https://management.azure.com/.default").token

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CostManagement/query?api-version=2025-03-01"
    )

    body = {
        "type": "Usage",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start_time_utc.isoformat(),
            "to": end_time_utc.isoformat()
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
            data = response.json()
            properties = data.get("properties", {})
            rows = properties.get("rows", [])
            columns = properties.get("columns", [])

            resource_id_idx = find_column_index(columns, "ResourceId")
            total_cost_idx = find_column_index(columns, "totalCost", "PreTaxCost")
            currency_idx = find_column_index(columns, "Currency")
            usage_date_idx = find_column_index(columns, "UsageDate", "BillingMonth", "Date")

            normalized_resource_ids = {x.lower(): x for x in resource_ids}
            raw_daily_rows: list[dict[str, Any]] = []
            total_cost = 0.0
            currency = None

            for row in rows:
                row_resource_id = str(row[resource_id_idx]).lower() if resource_id_idx is not None else ""
                if resource_ids and row_resource_id not in normalized_resource_ids:
                    continue

                cost_value = float(row[total_cost_idx]) if total_cost_idx is not None else 0.0
                currency = row[currency_idx] if currency_idx is not None else currency
                usage_date = normalize_cost_usage_date(row[usage_date_idx]) if usage_date_idx is not None else ""

                raw_daily_rows.append({
                    "usage_date": usage_date,
                    "resource_id": normalized_resource_ids.get(row_resource_id, row_resource_id),
                    "cost": cost_value,
                    "currency": currency
                })
                total_cost += cost_value

            usage_dates = sorted({r.get("usage_date") for r in raw_daily_rows if r.get("usage_date")})
            first_usage_date = usage_dates[0] if usage_dates else start_kst_str

            daily_rows: list[dict[str, Any]] = []
            for row in raw_daily_rows:
                date_kst = map_cost_usage_date_to_kst_period(
                    usage_date=row.get("usage_date", ""),
                    first_usage_date=first_usage_date,
                    start_kst_str=start_kst_str,
                    end_kst_str=end_kst_str,
                )
                daily_rows.append({
                    **row,
                    "date_kst": date_kst,
                })

            daily_rows.sort(key=lambda x: (x.get("date_kst") or "", x.get("resource_id") or ""))

            resource_totals: dict[str, float] = {}
            for row in daily_rows:
                rid = row["resource_id"]
                resource_totals[rid] = resource_totals.get(rid, 0.0) + row["cost"]

            resource_costs = [
                {"resource_id": rid, "cost": cost, "currency": currency}
                for rid, cost in sorted(resource_totals.items(), key=lambda x: (-x[1], x[0]))
            ]

            return {
                "period_label": period_label,
                "period_kst": f"{start_kst_str} ~ {end_kst_str}",
                "start_time_utc": start_time_utc.isoformat(),
                "end_time_utc": end_time_utc.isoformat(),
                "currency": currency,
                "total_cost": total_cost,
                "daily_rows": daily_rows,
                "resource_costs": resource_costs,
                "cost_data_available": True,
                "cost_date_basis": "KST display adjusted from Cost Management UTC daily UsageDate",
            }

        last_response = response
        if response.status_code == 429 and attempt < len(retry_delays):
            delay = retry_delays[attempt]
            logging.warning(
                "Monthly Cost API throttled (429). retry=%s/%s wait=%ss",
                attempt + 1,
                len(retry_delays),
                delay
            )
            time.sleep(delay)
            continue
        break

    raise RuntimeError(
        f"월간 Cost API 호출 실패: {last_response.status_code} / {last_response.text}"
    )

def build_monthly_report_data() -> dict[str, Any]:
    resources = load_resources()
    deployment_model_map = load_deployment_model_map()
    credential = DefaultAzureCredential()
    subscription_id = get_env("SUBSCRIPTION_ID")
    resource_ids = [x["resource_id"] for x in resources]

    start_utc, end_utc, start_kst_str, end_kst_str, period_label = get_previous_month_range_to_utc()

    metrics = fetch_metrics_for_custom_range(
        credential=credential,
        resources=resources,
        start_time_utc=start_utc,
        end_time_utc=end_utc,
        deployment_model_map=deployment_model_map,
    )

    cost_error = None
    try:
        costs = fetch_costs_for_custom_range(
            credential=credential,
            subscription_id=subscription_id,
            resource_ids=resource_ids,
            start_time_utc=start_utc,
            end_time_utc=end_utc,
            period_label=period_label,
            start_kst_str=start_kst_str,
            end_kst_str=end_kst_str,
        )
    except Exception as e:
        logging.exception("Monthly cost data fetch failed")
        cost_error = str(e)
        costs = {
            "period_label": period_label,
            "period_kst": f"{start_kst_str} ~ {end_kst_str}",
            "start_time_utc": start_utc.isoformat(),
            "end_time_utc": end_utc.isoformat(),
            "currency": None,
            "total_cost": None,
            "daily_rows": [],
            "resource_costs": [],
            "cost_data_available": False,
        }

    return {
        "period_label": period_label,
        "period_kst": f"{start_kst_str} ~ {end_kst_str}",
        "month": start_kst_str[:7],
        "metrics": metrics,
        "costs": costs,
        "cost_error": cost_error,
    }




def get_azure_openai_client():
    endpoint = get_env("AZURE_OPENAI_ENDPOINT")
    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential,
        "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_version="2024-10-21",
        azure_ad_token_provider=token_provider
    )

def generate_monthly_report_text(monthly_data: dict[str, Any]) -> str:
    client = get_azure_openai_client()
    deployment_name = get_env("AZURE_OPENAI_DEPLOYMENT_NAME")

    metrics = monthly_data["metrics"]
    costs = monthly_data["costs"]
    summary = metrics["summary"]
    currency = costs.get("currency")
    top_models = metrics.get("model_summary", [])[:3]

    lightweight_data = {
        "period_label": monthly_data["period_label"],
        "period_kst": monthly_data["period_kst"],
        "cost_error": monthly_data.get("cost_error"),
        "summary": summary,
        "cost_total_text": format_cost_text(costs.get("total_cost"), currency),
        "cost_available": costs.get("cost_data_available", True),
        "top_models": top_models,
    }

    system_prompt = """
너는 Azure OpenAI 비용 분석 리포트를 작성하는 FinOps 분석가다.
사용자가 제공한 JSON 데이터를 바탕으로 짧고 명확한 한국어 월간 리포트를 작성한다.

규칙:
1. 과장하지 말고 데이터에 근거해서만 작성한다.
2. 6문장 이내로 작성한다.
3. 기간은 반드시 전월 1일~말일 기준이라고 자연스럽게 반영한다.
4. 비용은 반드시 사용자가 준 문자열(cost_total_text)을 그대로 사용한다.
5. 비용 문자열을 원 단위 정수로 다시 변환하거나 천 단위로 재해석하지 않는다.
6. 토큰은 input, output, total 순서로 언급하고, 요청 수가 있으면 함께 간단히 언급한다.
7. 모델별 정보가 있으면 canonical model 기준 상위 모델 1~3개를 자연스럽게 언급한다.
8. cost_data_available가 false이거나 cost_error가 있으면 비용 데이터는 일시적으로 조회되지 않았다고 안내하고 토큰/요청 사용량 중심으로 작성한다.
9. 같은 모델이 여러 리전이나 여러 deployment에서 합산되었을 수 있음을 자연스럽게 반영할 수 있다.
"""

    user_prompt = f"""
다음 JSON 데이터를 기반으로 AOAI FinOps Sentinel 월간 리포트를 한국어로 작성해줘.

중요:
- 비용은 cost_total_text 값을 그대로 사용해.
- model_summary와 top_models는 deployment가 아니라 canonical model 기준 집계다.
- summary 안의 request_count는 Azure OpenAI Requests 메트릭 기반 요청 수다.
- 기간은 전월 1일~말일 기준이다.

데이터:
{json.dumps(lightweight_data, ensure_ascii=False, indent=2)}
"""

    response = client.chat.completions.create(
        model=deployment_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.1,
        max_tokens=500
    )

    return add_line_breaks(response.choices[0].message.content.strip())


def build_monthly_model_summary_html(monthly_data: dict[str, Any]) -> str:
    model_summary = monthly_data["metrics"].get("model_summary", [])
    if not model_summary:
        return """
        <h3 style="margin:24px 0 8px;">모델별 월간 토큰 및 요청 집계</h3>
        <p>모델별 데이터가 없습니다.</p>
        """

    rows_html = ""
    for item in model_summary:
        rows_html += f"""
        <tr>
          <td style="border:1px solid #d1d5db; padding:8px;">{item["model_name"]}</td>
          <td style="border:1px solid #d1d5db; padding:8px;">{", ".join(item.get("regions", [])) or "-"}</td>
          <td style="border:1px solid #d1d5db; padding:8px;">{", ".join(item.get("deployments", [])) or "-"}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item.get("prompt_tokens"))}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item.get("completion_tokens"))}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item.get("total_tokens"))}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item.get("request_count"))}</td>
        </tr>
        """

    return f"""
    <h3 style="margin:24px 0 8px;">모델별 월간 토큰 및 요청 집계 (모델 기준 통합)</h3>
    <table style="border-collapse:collapse; width:100%; max-width:1400px; font-size:13px;">
      <tr style="background:#f3f4f6;">
        <th style="border:1px solid #d1d5db; padding:8px;">모델명</th>
        <th style="border:1px solid #d1d5db; padding:8px;">리전</th>
        <th style="border:1px solid #d1d5db; padding:8px;">포함된 배포명</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Input Tokens</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Output Tokens</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Total Tokens</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Requests</th>
      </tr>
      {rows_html}
    </table>
    """


def build_monthly_deployment_html(monthly_data: dict[str, Any]) -> str:
    items = monthly_data["metrics"].get("items", [])
    if not items:
        return """
        <h3 style="margin:24px 0 8px;">리전/배포별 월간 토큰 및 요청 집계</h3>
        <p>리전/배포별 데이터가 없습니다.</p>
        """

    rows_html = ""
    for item in items:
        rows_html += f"""
        <tr>
          <td style="border:1px solid #d1d5db; padding:8px;">{item["region"]}</td>
          <td style="border:1px solid #d1d5db; padding:8px;">{item["model_name"]}</td>
          <td style="border:1px solid #d1d5db; padding:8px;">{item["model_deployment_name"]}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["prompt_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["completion_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["total_tokens"])}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(item["request_count"])}</td>
        </tr>
        """

    return f"""
    <h3 style="margin:24px 0 8px;">리전/배포별 월간 토큰 및 요청 집계</h3>
    <table style="border-collapse:collapse; width:100%; max-width:1400px; font-size:13px;">
      <tr style="background:#f3f4f6;">
        <th style="border:1px solid #d1d5db; padding:8px;">리전</th>
        <th style="border:1px solid #d1d5db; padding:8px;">모델명</th>
        <th style="border:1px solid #d1d5db; padding:8px;">배포명</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Input Tokens</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Output Tokens</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Total Tokens</th>
        <th style="border:1px solid #d1d5db; padding:8px;">Requests</th>
      </tr>
      {rows_html}
    </table>
    """


def build_monthly_cost_html(monthly_data: dict[str, Any]) -> str:
    costs = monthly_data["costs"]
    currency = costs.get("currency")

    if not costs.get("cost_data_available", True):
        return f"""
        <h3 style="margin:24px 0 8px;">월간 비용</h3>
        <p>비용 데이터는 일시적으로 조회되지 않았습니다.</p>
        """

    def normalize_day_label(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        if len(text) == 8 and text.isdigit():
            return f"{text[4:6]}-{text[6:8]}"
        if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
            return text[5:10]
        return text

    # 최종 렌더링 시점에서도 날짜 기준 재합산하여,
    # 동일 날짜가 여러 리소스로 나뉘어 내려와도 한 줄로 표시되게 한다.
    merged_daily_costs: dict[str, float] = {}
    for row in costs.get("daily_rows", []):
        day_label = normalize_day_label(
            row.get("date_kst") or row.get("date") or row.get("usage_date")
        )
        merged_daily_costs[day_label] = merged_daily_costs.get(day_label, 0.0) + float(row.get("cost", 0.0) or 0.0)

    daily_rows_html = ""
    for day_label, merged_cost in sorted(merged_daily_costs.items(), key=lambda x: x[0]):
        daily_rows_html += f"""
        <tr>
          <td style="border:1px solid #d1d5db; padding:8px; white-space:nowrap;">{day_label}</td>
          <td style="border:1px solid #d1d5db; padding:8px; text-align:right; white-space:nowrap;">{format_cost_text(merged_cost, currency)}</td>
        </tr>
        """

    daily_rows_html += f"""
    <tr style="background:#f9fafb; font-weight:700;">
      <td style="border:1px solid #d1d5db; padding:8px; white-space:nowrap;">총합</td>
      <td style="border:1px solid #d1d5db; padding:8px; text-align:right; white-space:nowrap;">{format_cost_text(costs.get("total_cost"), currency)}</td>
    </tr>
    """

    return f"""
    <h3 style="margin:24px 0 8px;">월간 비용</h3>
    <p><strong>기간:</strong> {monthly_data["period_kst"]}</p>
    <p><strong>AI 전체 비용:</strong> {format_cost_text(costs.get("total_cost"), currency)}</p>
    <table style="border-collapse:collapse; width:auto; min-width:320px; font-size:13px; table-layout:auto;">
      <tr style="background:#f3f4f6;">
        <th style="border:1px solid #d1d5db; padding:8px; min-width:110px;">날짜</th>
        <th style="border:1px solid #d1d5db; padding:8px; min-width:140px;">비용</th>
      </tr>
      {daily_rows_html}
    </table>
    """


def build_monthly_email_html(report_text: str, monthly_data: dict[str, Any]) -> str:
    metrics = monthly_data["metrics"]
    costs = monthly_data["costs"]
    summary = metrics["summary"]
    currency = costs.get("currency")

    model_section = build_monthly_model_summary_html(monthly_data)
    deployment_section = build_monthly_deployment_html(monthly_data)
    cost_section = build_monthly_cost_html(monthly_data)

    html = f"""
    <html>
      <body style="font-family:Arial, sans-serif; color:#111827; background:#f9fafb; margin:0; padding:24px;">
        <div style="max-width:1200px; margin:0 auto; background:white; border:1px solid #e5e7eb; border-radius:12px; padding:24px;">
          <h2 style="margin-top:0;">AOAI FinOps Sentinel 월간 리포트</h2>
          <p style="color:#6b7280; margin-top:-8px;">대상 기간: {monthly_data["period_kst"]}</p>

          <div style="background:#f3f4f6; border-radius:8px; padding:16px; white-space:pre-wrap; line-height:1.6;">{report_text}</div>

          <h3 style="margin:24px 0 8px;">월간 요약</h3>
          <table style="border-collapse:collapse; width:auto; min-width:420px; font-size:13px; table-layout:auto;">
            <tr style="background:#f3f4f6;">
              <th style="border:1px solid #d1d5db; padding:8px; min-width:180px; white-space:nowrap;">항목</th>
              <th style="border:1px solid #d1d5db; padding:8px; min-width:140px; white-space:nowrap;">값</th>
            </tr>
            <tr>
              <td style="border:1px solid #d1d5db; padding:8px;">Input Tokens</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(summary.get("prompt_tokens"))}</td>
            </tr>
            <tr>
              <td style="border:1px solid #d1d5db; padding:8px;">Output Tokens</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(summary.get("completion_tokens"))}</td>
            </tr>
            <tr>
              <td style="border:1px solid #d1d5db; padding:8px;">Total Tokens</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(summary.get("total_tokens"))}</td>
            </tr>
            <tr>
              <td style="border:1px solid #d1d5db; padding:8px;">Requests</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_number(summary.get("request_count"))}</td>
            </tr>
            <tr>
              <td style="border:1px solid #d1d5db; padding:8px;">Total Cost</td>
              <td style="border:1px solid #d1d5db; padding:8px; text-align:right;">{format_cost_text(costs.get("total_cost"), currency)}</td>
            </tr>
          </table>

          {model_section}
          {deployment_section}
          {cost_section}

          <p style="margin-top:28px; color:#666; font-size:12px;">
            This report was generated by AOAI FinOps Sentinel.
          </p>
        </div>
      </body>
    </html>
    """
    return html


def execute_monthly_report_send() -> dict[str, Any]:
    monthly_data = build_monthly_report_data()
    report_text = generate_monthly_report_text(monthly_data)
    html_body = build_monthly_email_html(report_text, monthly_data)

    subject = f"[AOAI FinOps Sentinel] AOAI FinOps Sentinel 월간 리포트 - {monthly_data['month']}"
    send_result = send_email(subject, html_body)

    return {
        "message": "월간 메일 발송 완료",
        "send_result": send_result,
        "report_text": report_text,
        "monthly_data": monthly_data
    }


@app.route(route="monthly_report_data", methods=["GET"])
def monthly_report_data(req: func.HttpRequest) -> func.HttpResponse:
    try:
        result = build_monthly_report_data()
        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        logging.exception("monthly_report_data failed")
        return func.HttpResponse(str(e), status_code=500, mimetype="text/plain")


@app.route(route="monthly_report_preview", methods=["GET"])
def monthly_report_preview(req: func.HttpRequest) -> func.HttpResponse:
    try:
        monthly_data = build_monthly_report_data()
        report_text = generate_monthly_report_text(monthly_data)
        html_body = build_monthly_email_html(report_text, monthly_data)

        result = {
            "report_text": report_text,
            "source_data": monthly_data,
            "html_preview": html_body
        }

        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        logging.exception("monthly_report_preview failed")
        return func.HttpResponse(str(e), status_code=500, mimetype="text/plain")


@app.route(route="monthly_report_send", methods=["GET"])
def monthly_report_send(req: func.HttpRequest) -> func.HttpResponse:
    try:
        result = execute_monthly_report_send()
        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        logging.exception("monthly_report_send failed")
        return func.HttpResponse(str(e), status_code=500, mimetype="text/plain")


@app.function_name(name="monthly_report_timer")
@app.schedule(
    schedule="%MONTHLY_REPORT_SCHEDULE%",
    arg_name="mytimer",
    run_on_startup=False,
    use_monitor=True
)
def monthly_report_timer(mytimer: func.TimerRequest) -> None:
    logging.info("monthly_report_timer started. past_due=%s", mytimer.past_due if mytimer else None)

    try:
        result = execute_monthly_report_send()
        logging.info(
            "monthly_report_timer success: %s",
            json.dumps(
                {
                    "message": result["message"],
                    "period": result["monthly_data"]["period_kst"]
                },
                ensure_ascii=False
            )
        )
    except Exception:
        logging.exception("monthly_report_timer failed")
        raise


# -----------------------------
# Chat API - Static Web Apps Frontend 질의형 리포트 API
# -----------------------------
def parse_chat_date_range(message: str) -> tuple[datetime, datetime, str, str]:
    """
    사용자의 한국어/숫자형 날짜 표현을 KST 기준 시작/종료 일자로 변환합니다.

    지원 예:
    - 2026-04-01부터 2026-04-05까지
    - 2026년 4월 1일부터 4월 5일까지
    - 4월 1일부터 4월 5일까지
    - 4월1일 부터 5일까지
    - 4월 10일부터 11일까지
    - 4/1부터 4/5까지
    - 5월 1일 사용량 알려줘

    단일 날짜만 있으면 시작일과 종료일을 같은 날로 처리합니다.
    """
    now_kst = datetime.now(KST)
    default_year = now_kst.year
    text = message.strip()

    y1 = y2 = default_year
    m1 = m2 = d1 = d2 = None

    # 1) YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD 형식
    full_dates = re.findall(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})", text)
    if len(full_dates) >= 2:
        y1, m1, d1 = map(int, full_dates[0])
        y2, m2, d2 = map(int, full_dates[1])
    elif len(full_dates) == 1:
        y1, m1, d1 = map(int, full_dates[0])
        y2, m2, d2 = y1, m1, d1

    # 2) "4월1일 부터 5일까지", "4월 10일부터 11일까지"
    # 중요: 이 패턴은 "4월 10일" 단일 날짜보다 먼저 검사해야 기간 질의가 하루로 오인되지 않음.
    if m1 is None:
        inherited_month = re.search(
            r"(?P<month>\d{1,2})\s*월\s*(?P<start_day>\d{1,2})\s*일?\s*(?:부터|~|-|에서)\s*(?P<end_day>\d{1,2})\s*일?\s*(?:까지)?",
            text
        )
        if inherited_month:
            y1 = y2 = default_year
            m1 = m2 = int(inherited_month.group("month"))
            d1 = int(inherited_month.group("start_day"))
            d2 = int(inherited_month.group("end_day"))

    # 3) YYYY년 M월 D일 + M월 D일 / M월 D일 2개 / M월 D일 단일
    if m1 is None:
        first_with_year = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
        month_day_pairs = re.findall(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)

        if first_with_year and len(month_day_pairs) >= 2:
            y1 = int(first_with_year.group(1))
            m1 = int(month_day_pairs[0][0])
            d1 = int(month_day_pairs[0][1])
            y2 = y1
            m2 = int(month_day_pairs[1][0])
            d2 = int(month_day_pairs[1][1])
        elif first_with_year and len(month_day_pairs) == 1:
            y1 = int(first_with_year.group(1))
            m1 = int(month_day_pairs[0][0])
            d1 = int(month_day_pairs[0][1])
            y2, m2, d2 = y1, m1, d1
        elif len(month_day_pairs) >= 2:
            y1 = y2 = default_year
            m1 = int(month_day_pairs[0][0])
            d1 = int(month_day_pairs[0][1])
            m2 = int(month_day_pairs[1][0])
            d2 = int(month_day_pairs[1][1])
        elif len(month_day_pairs) == 1:
            y1 = y2 = default_year
            m1 = m2 = int(month_day_pairs[0][0])
            d1 = d2 = int(month_day_pairs[0][1])

    # 4) 4/1, 4/1부터 4/5까지
    if m1 is None:
        slash_pairs = re.findall(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", text)
        if len(slash_pairs) >= 2:
            y1 = y2 = default_year
            m1 = int(slash_pairs[0][0])
            d1 = int(slash_pairs[0][1])
            m2 = int(slash_pairs[1][0])
            d2 = int(slash_pairs[1][1])
        elif len(slash_pairs) == 1:
            y1 = y2 = default_year
            m1 = m2 = int(slash_pairs[0][0])
            d1 = d2 = int(slash_pairs[0][1])

    if m1 is None:
        raise ValueError(
            "조회 기간을 찾지 못했습니다. 예: '4월 1일부터 4월 5일까지 사용량 알려줘', "
            "'4월1일 부터 5일까지 사용량 알려줘', 또는 '5월 1일 사용량 알려줘'처럼 입력해주세요."
        )

    start_kst = datetime(y1, m1, d1, 0, 0, 0, tzinfo=KST)
    end_kst_inclusive = datetime(y2, m2, d2, 0, 0, 0, tzinfo=KST)

    if end_kst_inclusive < start_kst:
        raise ValueError("종료일이 시작일보다 빠릅니다. 조회 기간을 다시 확인해주세요.")

    end_kst_exclusive = end_kst_inclusive + timedelta(days=1)

    oldest_allowed = now_kst - timedelta(days=90)
    if start_kst < oldest_allowed:
        raise ValueError(
            f"Azure Monitor 메트릭은 최근 90일 이내 기간만 조회하도록 제한했습니다. "
            f"조회 시작일({start_kst.strftime('%Y-%m-%d')})을 최근 90일 이내로 지정해주세요."
        )

    if (end_kst_exclusive - start_kst).days > 90:
        raise ValueError("한 번에 조회 가능한 기간은 최대 90일입니다. 기간을 나누어 요청해주세요.")

    tomorrow_kst = datetime(now_kst.year, now_kst.month, now_kst.day, 0, 0, 0, tzinfo=KST) + timedelta(days=1)
    if start_kst >= tomorrow_kst:
        raise ValueError("미래 날짜는 조회할 수 없습니다.")

    if end_kst_exclusive > tomorrow_kst:
        end_kst_exclusive = tomorrow_kst
        end_kst_inclusive = tomorrow_kst - timedelta(days=1)

    return (
        start_kst,
        end_kst_exclusive,
        start_kst.strftime("%Y-%m-%d"),
        end_kst_inclusive.strftime("%Y-%m-%d"),
    )

def is_mail_request(message: str) -> bool:
    keywords = ["메일", "이메일", "mail", "email", "보내줘", "발송", "전송"]
    return any(keyword.lower() in message.lower() for keyword in keywords)


def build_unavailable_chat_costs(
    period_label: str,
    start_kst_str: str,
    end_kst_str: str,
    start_time_utc: datetime,
    end_time_utc: datetime,
    reason: str = "Chat Agent에서는 응답 지연 방지를 위해 비용 조회를 생략했습니다.",
) -> dict[str, Any]:
    """
    Chat Agent 전용 비용 fallback입니다.

    챗봇은 사용자가 실시간으로 응답을 기다리는 API이므로
    Cost Management API 429/지연으로 전체 리포트가 실패하지 않도록
    토큰/요청 수를 우선 제공하고 비용은 일시적으로 조회 불가로 표시합니다.
    """
    return {
        "period_label": period_label,
        "period_kst": f"{start_kst_str} ~ {end_kst_str}",
        "start_time_utc": start_time_utc.isoformat(),
        "end_time_utc": end_time_utc.isoformat(),
        "currency": None,
        "total_cost": None,
        "daily_rows": [],
        "resource_costs": [],
        "cost_data_available": False,
        "cost_error": reason,
    }


def build_chat_usage_report_data(message: str) -> dict[str, Any]:
    start_kst, end_kst_exclusive, start_label, end_label = parse_chat_date_range(message)

    start_utc = start_kst.astimezone(timezone.utc)
    end_utc = end_kst_exclusive.astimezone(timezone.utc)

    credential = DefaultAzureCredential()
    resources = load_resources()
    deployment_model_map = load_deployment_model_map()

    metrics = fetch_metrics_for_custom_range(
        credential=credential,
        resources=resources,
        start_time_utc=start_utc,
        end_time_utc=end_utc,
        deployment_model_map=deployment_model_map,
    )

    # 중요:
    # Chat Agent에서는 Cost Management API를 호출하지 않습니다.
    # Cost API가 429/지연 상태일 때 대화형 API가 60초 이상 대기하다 실패하는 것을 방지합니다.
    # 챗봇은 토큰/요청 수 중심으로 우선 응답하고, 비용은 정기 일일/월간 리포트에서 제공합니다.
    costs = build_unavailable_chat_costs(
        period_label=f"{start_label} ~ {end_label}",
        start_kst_str=start_label,
        end_kst_str=end_label,
        start_time_utc=start_utc,
        end_time_utc=end_utc,
    )

    return {
        "query": message,
        "period_kst": f"{start_label} ~ {end_label}",
        "start_date_kst": start_label,
        "end_date_kst": end_label,
        "start_time_utc": start_utc.isoformat(),
        "end_time_utc": end_utc.isoformat(),
        "metrics": metrics,
        "costs": costs,
        "cost_error": costs.get("cost_error"),
    }

def is_single_day_report(report_data: dict[str, Any]) -> bool:
    return report_data.get("start_date_kst") == report_data.get("end_date_kst")


def build_chat_daily_compare_data(report_data: dict[str, Any]) -> dict[str, Any]:
    """
    챗봇에서 단일 날짜를 조회한 경우, 기존 일일 리포트와 동일하게
    조회일과 전일을 비교하는 데이터 구조를 생성합니다.

    단, Chat Agent에서는 Cost Management API를 호출하지 않습니다.
    비용은 정기 일일/월간 리포트에서 처리하고, 챗봇은 토큰/요청 수 중심으로 응답합니다.
    """
    current_start = datetime.fromisoformat(report_data["start_time_utc"])
    previous_start = current_start - timedelta(days=1)
    previous_end = current_start

    current_date = report_data["start_date_kst"]
    previous_date = (datetime.fromisoformat(report_data["start_date_kst"]).date() - timedelta(days=1)).isoformat()

    credential = DefaultAzureCredential()
    resources = load_resources()
    deployment_model_map = load_deployment_model_map()

    previous_metrics = fetch_metrics_for_custom_range(
        credential=credential,
        resources=resources,
        start_time_utc=previous_start,
        end_time_utc=previous_end,
        deployment_model_map=deployment_model_map,
    )

    current_metrics = report_data["metrics"]

    model_breakdown = build_model_breakdown(
        previous_metrics.get("model_summary", []),
        current_metrics.get("model_summary", [])
    )

    deployment_breakdown = build_deployment_breakdown(
        previous_metrics.get("items", []),
        current_metrics.get("items", [])
    )

    previous_costs = build_unavailable_chat_costs(
        period_label=f"{previous_date} ~ {previous_date}",
        start_kst_str=previous_date,
        end_kst_str=previous_date,
        start_time_utc=previous_start,
        end_time_utc=previous_end,
    )
    current_costs = report_data["costs"]

    cost_change = {
        "difference": None,
        "rate_percent": None
    }

    token_change = {
        "prompt_tokens": calculate_change(
            current_metrics["summary"].get("prompt_tokens"),
            previous_metrics["summary"].get("prompt_tokens")
        ),
        "completion_tokens": calculate_change(
            current_metrics["summary"].get("completion_tokens"),
            previous_metrics["summary"].get("completion_tokens")
        ),
        "total_tokens": calculate_change(
            current_metrics["summary"].get("total_tokens"),
            previous_metrics["summary"].get("total_tokens")
        ),
        "request_count": calculate_change(
            current_metrics["summary"].get("request_count"),
            previous_metrics["summary"].get("request_count")
        ),
    }

    return {
        "timezone": "KST",
        "resource_count": len(resources),
        "deployment_model_map_count": len(deployment_model_map),
        "cost_error": "Chat Agent에서는 응답 지연 방지를 위해 비용 조회를 생략했습니다.",
        "comparison": {
            "previous_day": {
                "date_kst": previous_date,
                "metrics": previous_metrics,
                "costs": previous_costs
            },
            "current_day": {
                "date_kst": current_date,
                "metrics": current_metrics,
                "costs": current_costs
            },
            "summary_change": {
                "tokens": token_change,
                "cost": cost_change
            },
            "model_breakdown": model_breakdown,
            "deployment_breakdown": deployment_breakdown
        }
    }

def build_chat_daily_compare_html(report_text: str, compare_data: dict[str, Any]) -> str:
    daily_html = build_email_html(report_text, compare_data)
    # 기존 이메일 HTML은 전체 html/body를 포함하므로, 채팅 화면에 넣을 때도 그대로 사용 가능하다.
    return daily_html


def generate_chat_usage_summary(report_data: dict[str, Any]) -> str:
    try:
        client = get_azure_openai_client()
        deployment_name = get_env("AZURE_OPENAI_DEPLOYMENT_NAME")

        metrics = report_data["metrics"]
        costs = report_data["costs"]
        summary = metrics.get("summary", {})
        currency = costs.get("currency")

        lightweight_data = {
            "period_kst": report_data["period_kst"],
            "query": report_data["query"],
            "summary": summary,
            "cost_total_text": format_cost_text(costs.get("total_cost"), currency),
            "cost_available": costs.get("cost_data_available", True),
            "cost_error": costs.get("cost_error"),
            "top_models": metrics.get("model_summary", [])[:5],
        }

        system_prompt = """
너는 Azure OpenAI 사용량을 분석하는 FinOps Agent다.
사용자 질의에 대해 지정 기간의 사용량을 간결한 한국어로 요약한다.

규칙:
1. 데이터에 근거해서만 말한다.
2. 5문장 이내로 작성한다.
3. 기간, 총 토큰, 요청 수, 비용을 포함한다.
4. 모델별 상위 사용량이 있으면 canonical model 기준으로 언급한다.
5. 비용은 cost_total_text 값을 그대로 사용한다.
6. cost_available이 false이면 비용은 챗봇 조회에서 생략되었다고 안내하고 토큰/요청 수 중심으로 설명한다.
"""

        user_prompt = f"""
다음 데이터를 기반으로 채팅 응답용 사용량 요약을 작성해줘.

데이터:
{json.dumps(lightweight_data, ensure_ascii=False, indent=2)}
"""

        response = client.chat.completions.create(
            model=deployment_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=500,
        )
        return add_line_breaks(response.choices[0].message.content.strip())

    except Exception as e:
        logging.exception("generate_chat_usage_summary failed")
        summary = report_data["metrics"].get("summary", {})
        costs = report_data["costs"]
        cost_sentence = (
            f"총 비용은 {format_cost_text(costs.get('total_cost'), costs.get('currency'))}입니다."
            if costs.get("cost_data_available") is not False
            else "비용 데이터는 응답 지연 방지를 위해 챗봇 조회에서 생략되었습니다. 토큰/요청 수 기준으로 리포트를 제공합니다."
        )
        return (
            f"{report_data['period_kst']} 기준 Azure OpenAI 사용량입니다.<br>"
            f"총 토큰은 {format_number(summary.get('total_tokens'))}, "
            f"입력 토큰은 {format_number(summary.get('prompt_tokens'))}, "
            f"출력 토큰은 {format_number(summary.get('completion_tokens'))}, "
            f"요청 수는 {format_number(summary.get('request_count'))}건입니다.<br>"
            f"{cost_sentence}"
        )


def build_chat_usage_report_html(report_text: str, report_data: dict[str, Any]) -> str:
    metrics = report_data["metrics"]
    costs = report_data["costs"]
    summary = metrics.get("summary", {})
    currency = costs.get("currency")

    table_style = "border-collapse:collapse; width:100%; max-width:900px; font-size:13px; margin:8px 0 20px;"
    th_style = "border:1px solid #d1d5db; padding:8px; background:#f3f4f6; white-space:nowrap; text-align:center;"
    td_style = "border:1px solid #d1d5db; padding:8px; white-space:nowrap;"
    td_right = "border:1px solid #d1d5db; padding:8px; text-align:right; white-space:nowrap;"

    model_rows = ""
    for item in metrics.get("model_summary", []):
        model_rows += f"""
        <tr>
          <td style="{td_style}">{item.get("model_name", "-")}</td>
          <td style="{td_style}">{", ".join(item.get("regions", []))}</td>
          <td style="{td_style}">{", ".join(item.get("deployments", []))}</td>
          <td style="{td_right}">{format_number(item.get("prompt_tokens"))}</td>
          <td style="{td_right}">{format_number(item.get("completion_tokens"))}</td>
          <td style="{td_right}">{format_number(item.get("total_tokens"))}</td>
          <td style="{td_right}">{format_number(item.get("request_count"))}</td>
        </tr>
        """

    if not model_rows:
        model_rows = f"""
        <tr>
          <td colspan="7" style="{td_style}">조회된 모델 사용량이 없습니다.</td>
        </tr>
        """

    deployment_rows = ""
    for item in metrics.get("items", []):
        deployment_rows += f"""
        <tr>
          <td style="{td_style}">{item.get("region", "-")}</td>
          <td style="{td_style}">{item.get("model_name", "-")}</td>
          <td style="{td_style}">{item.get("model_deployment_name", "-")}</td>
          <td style="{td_right}">{format_number(item.get("prompt_tokens"))}</td>
          <td style="{td_right}">{format_number(item.get("completion_tokens"))}</td>
          <td style="{td_right}">{format_number(item.get("total_tokens"))}</td>
          <td style="{td_right}">{format_number(item.get("request_count"))}</td>
        </tr>
        """

    if not deployment_rows:
        deployment_rows = f"""
        <tr>
          <td colspan="7" style="{td_style}">조회된 리전/배포별 사용량이 없습니다.</td>
        </tr>
        """

    html = f"""
    <div style="font-family:Arial, sans-serif; color:#111827; line-height:1.5;">
      <h2 style="margin:0 0 12px;">AOAI FinOps Sentinel 질의형 사용량 리포트</h2>
      <p style="margin:0 0 20px; color:#374151;">조회 기간: {report_data["period_kst"]} (KST)</p>

      <h3 style="margin:24px 0 8px;">요약</h3>
      <div style="background:#f3f4f6; border-radius:8px; padding:14px; max-width:900px; margin-bottom:20px;">
        {report_text}
      </div>

      <h3 style="margin:24px 0 8px;">사용량 요약</h3>
      <table style="border-collapse:collapse; width:100%; max-width:700px; font-size:13px; margin:8px 0 20px;">
        <tr>
          <th style="{th_style}">항목</th>
          <th style="{th_style}">값</th>
        </tr>
        <tr><td style="{td_style}">Input Tokens</td><td style="{td_right}">{format_number(summary.get("prompt_tokens"))}</td></tr>
        <tr><td style="{td_style}">Output Tokens</td><td style="{td_right}">{format_number(summary.get("completion_tokens"))}</td></tr>
        <tr><td style="{td_style}">Total Tokens</td><td style="{td_right}">{format_number(summary.get("total_tokens"))}</td></tr>
        <tr><td style="{td_style}">Requests</td><td style="{td_right}">{format_number(summary.get("request_count"))}</td></tr>
        <tr><td style="{td_style}">Total Cost</td><td style="{td_right}">{format_cost_text(costs.get("total_cost"), currency)}</td></tr>
      </table>

      <h3 style="margin:24px 0 8px;">모델별 사용량</h3>
      <table style="{table_style}">
        <tr>
          <th style="{th_style}">모델명</th>
          <th style="{th_style}">리전</th>
          <th style="{th_style}">포함된 배포명</th>
          <th style="{th_style}">Input Tokens</th>
          <th style="{th_style}">Output Tokens</th>
          <th style="{th_style}">Total Tokens</th>
          <th style="{th_style}">Requests</th>
        </tr>
        {model_rows}
      </table>

      <h3 style="margin:24px 0 8px;">리전/배포별 사용량</h3>
      <table style="{table_style}">
        <tr>
          <th style="{th_style}">리전</th>
          <th style="{th_style}">모델명</th>
          <th style="{th_style}">배포명</th>
          <th style="{th_style}">Input Tokens</th>
          <th style="{th_style}">Output Tokens</th>
          <th style="{th_style}">Total Tokens</th>
          <th style="{th_style}">Requests</th>
        </tr>
        {deployment_rows}
      </table>
    </div>
    """
    return html


@app.route(route="chat_query", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def chat_query(req: func.HttpRequest) -> func.HttpResponse:
    """
    Static Web Apps 챗봇 질의 API입니다.

    기능:
    - 자연어에서 날짜 범위 추출
    - 90일 초과/오래된 기간 차단
    - 토큰/요청/비용 조회
    - 채팅 화면용 HTML 리포트 반환
    - '메일로 보내줘' 요청 시 동일 리포트를 메일 발송
    """
    try:
        body = {}

        try:
            parsed = req.get_json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}

        if not body:
            try:
                raw_body = req.get_body()
                raw_text = raw_body.decode("utf-8-sig").strip() if raw_body else ""
                if raw_text:
                    parsed = json.loads(raw_text)
                    if isinstance(parsed, dict):
                        body = parsed
            except Exception:
                body = {}

        user_message = (
            body.get("message")
            or body.get("question")
            or body.get("text")
            or req.params.get("message")
            or req.params.get("question")
            or req.params.get("text")
            or ""
        ).strip()

        if not user_message:
            return func.HttpResponse(
                json.dumps({
                    "status": "error",
                    "answer": "질문 내용이 비어 있습니다. 예: '4월 1일부터 4월 5일까지 사용량 알려줘'",
                    "answer_html": "<p>질문 내용이 비어 있습니다.</p>",
                }, ensure_ascii=False),
                status_code=400,
                mimetype="application/json"
            )

        report_data = build_chat_usage_report_data(user_message)

        if is_single_day_report(report_data):
            compare_data = build_chat_daily_compare_data(report_data)
            report_text = generate_report_text(compare_data)
            report_html = build_chat_daily_compare_html(report_text, compare_data)
            subject = f"[AOAI FinOps Sentinel] 질의형 일일 사용량 리포트 - {report_data['start_date_kst']}"
            response_report_data = {
                "mode": "single_day_compare",
                "report_data": report_data,
                "compare_data": compare_data,
            }
        else:
            report_text = generate_chat_usage_summary(report_data)
            report_html = build_chat_usage_report_html(report_text, report_data)
            subject = f"[AOAI FinOps Sentinel] 질의형 사용량 리포트 - {report_data['period_kst']}"
            response_report_data = {
                "mode": "range_summary",
                "report_data": report_data,
            }

        mail_sent = False
        mail_result = None

        if is_mail_request(user_message):
            target_keyword = extract_mail_target_keyword(user_message)
            target_recipients = resolve_mail_recipients(target_keyword) if target_keyword else None

            full_html = f"""
            <html>
              <body style="font-family:Arial, sans-serif; color:#111827;">
                {report_html}
                <p style="margin-top:24px; color:#6b7280; font-size:12px;">
                  This report was generated by AOAI FinOps Sentinel Chat Agent.
                </p>
              </body>
            </html>
            """
            mail_result = send_email(subject, full_html, recipients_override=target_recipients)
            mail_sent = True

        final_answer = report_text
        if mail_sent:
            sent_to = ", ".join(mail_result.get("to", [])) if isinstance(mail_result, dict) else ""
            final_answer += f"<br><br>요청하신 리포트를 메일로 발송했습니다."
            if sent_to:
                final_answer += f"<br>수신자: {sent_to}"

        return func.HttpResponse(
            json.dumps({
                "status": "ok",
                "answer": final_answer,
                "answer_html": report_html + ("<p><strong>메일 발송 완료</strong></p>" if mail_sent else ""),
                "mail_sent": mail_sent,
                "mail_result": mail_result,
                "report_data": report_data,
            }, ensure_ascii=False, default=str),
            status_code=200,
            mimetype="application/json"
        )

    except ValueError as e:
        return func.HttpResponse(
            json.dumps({
                "status": "error",
                "answer": str(e),
                "answer_html": f"<p>{str(e)}</p>",
            }, ensure_ascii=False),
            status_code=400,
            mimetype="application/json"
        )

    except Exception as e:
        logging.exception("chat_query 처리 중 오류 발생")
        return func.HttpResponse(
            json.dumps({
                "status": "error",
                "answer": f"chat_query 처리 중 오류가 발생했습니다: {str(e)}",
                "answer_html": f"<p>chat_query 처리 중 오류가 발생했습니다: {str(e)}</p>",
            }, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )

