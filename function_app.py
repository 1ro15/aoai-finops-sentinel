import json
import logging
import os
import smtplib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import azure.functions as func
import requests
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.monitor.querymetrics import MetricsClient, MetricAggregationType
from openai import AzureOpenAI

app = func.FunctionApp()

KST = timezone(timedelta(hours=9))


def get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"환경 변수 누락: {name}")
    return value


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


def get_metrics_endpoint(region: str) -> str:
    return f"https://{region}.metrics.monitor.azure.com"


def safe_attr(obj: Any, attr_name: str, default: str = "") -> str:
    try:
        value = getattr(obj, attr_name, None)
        if value is None:
            return default
        return str(value)
    except Exception:
        return default


def parse_dimension_map(metadata_values: list[Any]) -> dict[str, str]:
    result: dict[str, str] = {}

    for item in metadata_values or []:
        name = safe_attr(item, "name")
        value = safe_attr(item, "value")

        if not name and isinstance(item, dict):
            name = str(item.get("name", ""))
            value = str(item.get("value", ""))

        if name:
            result[name] = value

    return result


def metric_name_to_field(metric_name: str) -> str:
    mapping = {
        "ProcessedPromptTokens": "prompt_tokens",
        "GeneratedTokens": "completion_tokens",
        "TokenTransaction": "total_tokens",
    }
    return mapping.get(metric_name, metric_name)


def query_metrics_for_region(
    credential: DefaultAzureCredential,
    region: str,
    resource_ids: list[str],
    start_time_utc: datetime,
    end_time_utc: datetime,
) -> list[dict[str, Any]]:
    endpoint = get_metrics_endpoint(region)
    client = MetricsClient(endpoint, credential)

    results = client.query_resources(
        resource_ids=resource_ids,
        metric_namespace="Microsoft.CognitiveServices/accounts",
        metric_names=[
            "ProcessedPromptTokens",
            "GeneratedTokens",
            "TokenTransaction",
        ],
        timespan=(start_time_utc, end_time_utc),
        granularity=timedelta(days=1),
        aggregations=[MetricAggregationType.TOTAL],
    )

    rows: list[dict[str, Any]] = []

    for index, metrics_query_result in enumerate(results):
        resource_id = resource_ids[index]

        for metric in metrics_query_result.metrics:
            metric_name = str(metric.name)

            for ts in metric.timeseries:
                dimension_map = parse_dimension_map(getattr(ts, "metadata_values", []))

                total_value = 0
                for point in ts.data:
                    if getattr(point, "total", None) is not None:
                        total_value += point.total

                rows.append({
                    "resource_id": resource_id,
                    "region": region,
                    "metric_name": metric_name,
                    "model_deployment_name": dimension_map.get("ModelDeploymentName", ""),
                    "model_name": dimension_map.get("ModelName", ""),
                    "model_version": dimension_map.get("ModelVersion", ""),
                    "api_name": dimension_map.get("ApiName", ""),
                    "usage_channel": dimension_map.get("UsageChannel", ""),
                    "feature_name": dimension_map.get("FeatureName", ""),
                    "total": total_value,
                    "raw_dimensions": dimension_map,
                })

    return rows


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple, dict[str, Any]] = {}

    for row in rows:
        key = (
            row["resource_id"],
            row["region"],
            row["model_deployment_name"],
            row["model_name"],
            row["model_version"],
            row["api_name"],
            row["usage_channel"],
            row["feature_name"],
        )

        if key not in grouped:
            grouped[key] = {
                "resource_id": row["resource_id"],
                "region": row["region"],
                "model_deployment_name": row["model_deployment_name"],
                "model_name": row["model_name"],
                "model_version": row["model_version"],
                "api_name": row["api_name"],
                "usage_channel": row["usage_channel"],
                "feature_name": row["feature_name"],
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


def calculate_change(current_value: float, previous_value: float) -> dict[str, float | None]:
    diff = current_value - previous_value
    rate = None
    if previous_value != 0:
        rate = (diff / previous_value) * 100
    return {
        "difference": diff,
        "rate_percent": rate
    }


def get_kst_day_range_to_utc(days_ago: int) -> tuple[datetime, datetime, str]:
    now_kst = datetime.now(KST)
    target_date_kst = (now_kst - timedelta(days=days_ago)).date()

    start_kst = datetime.combine(target_date_kst, datetime.min.time(), tzinfo=KST)
    end_kst = start_kst + timedelta(days=1)

    start_utc = start_kst.astimezone(timezone.utc)
    end_utc = end_kst.astimezone(timezone.utc)

    return start_utc, end_utc, str(target_date_kst)


def fetch_day_metrics(
    credential: DefaultAzureCredential,
    resources: list[dict[str, str]],
    days_ago: int,
) -> dict[str, Any]:
    start_utc, end_utc, target_date_kst = get_kst_day_range_to_utc(days_ago)

    region_to_resource_ids: dict[str, list[str]] = defaultdict(list)
    for item in resources:
        region_to_resource_ids[item["region"]].append(item["resource_id"])

    all_rows: list[dict[str, Any]] = []
    for region, resource_ids in region_to_resource_ids.items():
        region_rows = query_metrics_for_region(
            credential=credential,
            region=region,
            resource_ids=resource_ids,
            start_time_utc=start_utc,
            end_time_utc=end_utc,
        )
        all_rows.extend(region_rows)

    normalized = normalize_rows(all_rows)
    summary = sum_items(normalized)

    return {
        "target_date_kst": target_date_kst,
        "start_time_utc": start_utc.isoformat(),
        "end_time_utc": end_utc.isoformat(),
        "items": normalized,
        "summary": summary,
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
        raise RuntimeError(f"Cost API 호출 실패: {response.status_code} / {response.text}")

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

    resource_costs = []
    total_cost = 0
    currency = None

    normalized_resource_ids = {x.lower(): x for x in resource_ids}

    for row in rows:
        row_resource_id = str(row[resource_id_idx]).lower() if resource_id_idx is not None else ""
        if row_resource_id not in normalized_resource_ids:
            continue

        cost_value = float(row[total_cost_idx]) if total_cost_idx is not None else 0.0
        currency = row[currency_idx] if currency_idx is not None else currency

        resource_costs.append({
            "resource_id": normalized_resource_ids[row_resource_id],
            "usage_date": str(row[usage_date_idx]) if usage_date_idx is not None else target_date_kst,
            "cost": cost_value,
            "currency": currency
        })

        total_cost += cost_value

    return {
        "target_date_kst": target_date_kst,
        "start_time_utc": start_utc.isoformat(),
        "end_time_utc": end_utc.isoformat(),
        "currency": currency,
        "total_cost": total_cost,
        "resource_costs": resource_costs
    }


def build_daily_compare_data() -> dict[str, Any]:
    resources = load_resources()
    credential = DefaultAzureCredential()
    subscription_id = get_env("SUBSCRIPTION_ID")

    resource_ids = [x["resource_id"] for x in resources]

    d5_metrics = fetch_day_metrics(credential, resources, days_ago=5)
    d4_metrics = fetch_day_metrics(credential, resources, days_ago=4)

    d5_costs = fetch_day_costs(credential, subscription_id, resource_ids, days_ago=5)
    d4_costs = fetch_day_costs(credential, subscription_id, resource_ids, days_ago=4)

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

    cost_change = calculate_change(
        d4_costs["total_cost"],
        d5_costs["total_cost"]
    )

    return {
        "timezone": "KST",
        "resource_count": len(resources),
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
            }
        }
    }


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

    system_prompt = """
너는 Azure OpenAI 비용 분석 리포트를 작성하는 FinOps 분석가다.
사용자가 제공한 JSON 데이터를 바탕으로 짧고 명확한 한국어 일일 리포트를 작성한다.

규칙:
1. 과장하지 말고 데이터에 근거해서만 작성한다.
2. 값이 0이거나 변화가 없으면 '변동이 없습니다'처럼 담백하게 쓴다.
3. 5문장 이내로 작성한다.
4. 날짜는 KST 기준이라고 자연스럽게 반영한다.
5. 금액 단위는 원으로 표기하되, 값이 없으면 비용 집계가 없다고 쓴다.
6. 토큰은 input/output/total 순서로 언급하면 좋다.
7. 모델명이 비어 있으면 모델명 언급 없이 전체 사용량 기준으로 쓴다.
"""

    user_prompt = f"""
다음 JSON 데이터를 기반으로 Azure OpenAI 일일 리포트를 한국어로 작성해줘.

데이터:
{json.dumps(compare_data, ensure_ascii=False, indent=2)}
"""

    response = client.chat.completions.create(
        model=deployment_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.2,
        max_tokens=400
    )

    return response.choices[0].message.content.strip()


def format_number(value: float | int | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return f"{value:,}"


def build_email_html(report_text: str, compare_data: dict[str, Any]) -> str:
    prev_day = compare_data["comparison"]["previous_day"]
    curr_day = compare_data["comparison"]["current_day"]
    token_change = compare_data["comparison"]["summary_change"]["tokens"]
    cost_change = compare_data["comparison"]["summary_change"]["cost"]

    currency = curr_day["costs"].get("currency") or prev_day["costs"].get("currency") or "KRW"

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #222;">
        <h2>[AOAI FinOps Sentinel] Azure OpenAI 일일 비용 리포트</h2>
        <p><strong>비교 기준:</strong> {prev_day["date_kst"]} → {curr_day["date_kst"]} (KST)</p>

        <h3>요약</h3>
        <p>{report_text}</p>

        <h3>토큰 요약</h3>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse;">
          <tr>
            <th>항목</th>
            <th>{prev_day["date_kst"]}</th>
            <th>{curr_day["date_kst"]}</th>
            <th>증감</th>
            <th>증감률</th>
          </tr>
          <tr>
            <td>Input Tokens</td>
            <td>{format_number(prev_day["metrics"]["summary"]["prompt_tokens"])}</td>
            <td>{format_number(curr_day["metrics"]["summary"]["prompt_tokens"])}</td>
            <td>{format_number(token_change["prompt_tokens"]["difference"])}</td>
            <td>{format_number(token_change["prompt_tokens"]["rate_percent"])}%</td>
          </tr>
          <tr>
            <td>Output Tokens</td>
            <td>{format_number(prev_day["metrics"]["summary"]["completion_tokens"])}</td>
            <td>{format_number(curr_day["metrics"]["summary"]["completion_tokens"])}</td>
            <td>{format_number(token_change["completion_tokens"]["difference"])}</td>
            <td>{format_number(token_change["completion_tokens"]["rate_percent"])}%</td>
          </tr>
          <tr>
            <td>Total Tokens</td>
            <td>{format_number(prev_day["metrics"]["summary"]["total_tokens"])}</td>
            <td>{format_number(curr_day["metrics"]["summary"]["total_tokens"])}</td>
            <td>{format_number(token_change["total_tokens"]["difference"])}</td>
            <td>{format_number(token_change["total_tokens"]["rate_percent"])}%</td>
          </tr>
        </table>

        <h3>비용 요약</h3>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse;">
          <tr>
            <th>항목</th>
            <th>{prev_day["date_kst"]}</th>
            <th>{curr_day["date_kst"]}</th>
            <th>증감</th>
            <th>증감률</th>
          </tr>
          <tr>
            <td>Total Cost ({currency})</td>
            <td>{format_number(prev_day["costs"]["total_cost"])}</td>
            <td>{format_number(curr_day["costs"]["total_cost"])}</td>
            <td>{format_number(cost_change["difference"])}</td>
            <td>{format_number(cost_change["rate_percent"])}%</td>
          </tr>
        </table>

        <p style="margin-top: 24px; color: #666; font-size: 12px;">
          This report was generated by AOAI FinOps Sentinel.
        </p>
      </body>
    </html>
    """
    return html


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


@app.route(route="daily_compare", methods=["GET"])
def daily_compare(req: func.HttpRequest) -> func.HttpResponse:
    try:
        result = build_daily_compare_data()
        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception:
        logging.exception("daily_compare failed")
        return func.HttpResponse("daily_compare failed", status_code=500, mimetype="text/plain")


@app.route(route="daily_report_preview", methods=["GET"])
def daily_report_preview(req: func.HttpRequest) -> func.HttpResponse:
    try:
        compare_data = build_daily_compare_data()
        report_text = generate_report_text(compare_data)

        result = {
            "report_text": report_text,
            "source_data": compare_data
        }

        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception:
        logging.exception("daily_report_preview failed")
        return func.HttpResponse("daily_report_preview failed", status_code=500, mimetype="text/plain")


@app.route(route="daily_report_send", methods=["GET"])
def daily_report_send(req: func.HttpRequest) -> func.HttpResponse:
    try:
        result = execute_daily_report_send()
        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            mimetype="application/json",
            status_code=200
        )
    except Exception:
        logging.exception("daily_report_send failed")
        return func.HttpResponse("daily_report_send failed", status_code=500, mimetype="text/plain")


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
