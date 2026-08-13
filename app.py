from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    OrderBy,
    RunRealtimeReportRequest,
    RunReportRequest,
)
from google.oauth2 import service_account

from store_downloads import fetch_store_downloads


APP_NAME = "Advanced Manufacturing App"
DEPLOY_VERSION = "breakdown-fallback-2026-08-13"
CONFIG_DIR = Path(__file__).parent / "config"
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
APP_PLATFORMS = ["Android", "iOS"]
DISPLAY_AD_START_DATE = date(2026, 8, 6)
DISPLAY_AD_END_NOTE = "December 2026"
DOWNLOAD_BREAKDOWN_MODES = ["Daily", "Weekly", "Monthly"]


@dataclass(frozen=True)
class GaConfig:
    property_id: str
    service_account_file: Path | None = None
    service_account_info: dict | None = None


def get_property_id() -> str:
    try:
        secret_value = st.secrets.get("GA_PROPERTY_ID", "")
    except Exception:
        secret_value = ""
    return os.getenv("GA_PROPERTY_ID", secret_value).strip()


def get_store_config_path() -> str | None:
    try:
        secret_value = st.secrets.get("APP_STORES_CONFIG", "")
    except Exception:
        secret_value = ""
    configured = os.getenv("APP_STORES_CONFIG", secret_value).strip()
    return configured or None


def secrets_section(name: str) -> dict:
    try:
        section = st.secrets.get(name)
    except Exception:
        return {}
    if not section:
        return {}
    return json.loads(json.dumps(section.to_dict() if hasattr(section, "to_dict") else dict(section)))


def normalize_private_key(secret_info: dict | None) -> dict | None:
    if not secret_info:
        return secret_info
    output = dict(secret_info)
    private_key = output.get("private_key")
    if not private_key:
        return output

    output["private_key"] = rebuild_pem_private_key(str(private_key))
    return output


def rebuild_pem_private_key(private_key: str) -> str:
    normalized = private_key.strip().replace("\\n", "\n")
    normalized = normalized.replace("-----BEGIN PRIVATE KEY-----", "")
    normalized = normalized.replace("-----END PRIVATE KEY-----", "")
    body = "".join(normalized.split())
    lines = [body[index : index + 64] for index in range(0, len(body), 64)]
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"


def get_store_config() -> dict | None:
    google_play = secrets_section("google_play")
    app_store = secrets_section("app_store")
    if not google_play and not app_store:
        return None
    if google_play.get("service_account"):
        google_play["service_account"] = normalize_private_key(google_play["service_account"])
    app_store = normalize_private_key(app_store) or {}
    return {
        "auth_mode": google_play.get("auth_mode", "service_account"),
        "google_play": google_play,
        "app_store": app_store,
    }


def get_ga_service_account_info() -> dict | None:
    return normalize_private_key(secrets_section("ga4_service_account")) or None


def get_service_account_file() -> Path:
    try:
        secret_value = st.secrets.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    except Exception:
        secret_value = ""
    credential_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", secret_value).strip()
    if credential_path:
        return Path(credential_path).expanduser()

    for candidate in sorted(CONFIG_DIR.glob("*.json")):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("type") == "service_account" and payload.get("client_email") and payload.get("token_uri"):
            return candidate

    return CONFIG_DIR / "service-account.json"


@st.cache_resource(show_spinner=False)
def get_client_from_file(service_account_file: str) -> BetaAnalyticsDataClient:
    credentials = service_account.Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
    return BetaAnalyticsDataClient(credentials=credentials)


def get_client(config: GaConfig) -> BetaAnalyticsDataClient:
    if config.service_account_info:
        credentials = service_account.Credentials.from_service_account_info(
            config.service_account_info,
            scopes=SCOPES,
        )
        return BetaAnalyticsDataClient(credentials=credentials)
    if not config.service_account_file:
        raise ValueError("GA4 service account credentials are not configured.")
    return get_client_from_file(str(config.service_account_file))


def metric_value(row, index: int, cast=float):
    raw_value = row.metric_values[index].value
    if raw_value in ("", None):
        return cast(0)
    return cast(float(raw_value))


def rows_to_dataframe(response, dimensions: Iterable[str], metrics: Iterable[str]) -> pd.DataFrame:
    dimension_names = list(dimensions)
    metric_names = list(metrics)
    records = []
    for row in response.rows:
        record = {
            name: row.dimension_values[index].value
            for index, name in enumerate(dimension_names)
        }
        for index, name in enumerate(metric_names):
            value = row.metric_values[index].value
            record[name] = float(value) if "." in value else int(value or 0)
        records.append(record)
    return pd.DataFrame.from_records(records, columns=dimension_names + metric_names)


def app_platform_filter() -> FilterExpression:
    return FilterExpression(
        filter=Filter(
            field_name="platform",
            in_list_filter=Filter.InListFilter(values=APP_PLATFORMS, case_sensitive=False),
        )
    )


def combine_dimension_filters(*filters: FilterExpression | None) -> FilterExpression | None:
    active_filters = [item for item in filters if item is not None]
    if not active_filters:
        return None
    if len(active_filters) == 1:
        return active_filters[0]
    return FilterExpression(and_group=FilterExpressionList(expressions=active_filters))


def date_range_label(start: date, end: date) -> tuple[str, str]:
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def run_report(
    config: GaConfig,
    dimensions: list[str],
    metrics: list[str],
    start: date,
    end: date,
    limit: int = 1000,
    event_name: str | None = None,
    order_metric: str | None = None,
    app_only: bool = True,
) -> pd.DataFrame:
    request = RunReportRequest(
        property=f"properties/{config.property_id}",
        dimensions=[Dimension(name=name) for name in dimensions],
        metrics=[Metric(name=name) for name in metrics],
        date_ranges=[DateRange(start_date=date_range_label(start, end)[0], end_date=date_range_label(start, end)[1])],
        limit=limit,
    )
    event_filter = None
    if event_name:
        event_filter = FilterExpression(
            filter=Filter(
                field_name="eventName",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT,
                    value=event_name,
                ),
            )
        )
    request.dimension_filter = combine_dimension_filters(
        app_platform_filter() if app_only else None,
        event_filter,
    )
    if order_metric:
        request.order_bys = [OrderBy(metric=OrderBy.MetricOrderBy(metric_name=order_metric), desc=True)]

    response = get_client(config).run_report(request)
    return rows_to_dataframe(response, dimensions, metrics)


def run_realtime_report(config: GaConfig, dimensions: list[str], limit: int = 10, app_only: bool = True) -> pd.DataFrame:
    response = get_client(config).run_realtime_report(
        RunRealtimeReportRequest(
            property=f"properties/{config.property_id}",
            dimensions=[Dimension(name=name) for name in dimensions],
            metrics=[Metric(name="activeUsers")],
            limit=limit,
            dimension_filter=app_platform_filter() if app_only else None,
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="activeUsers"), desc=True)],
        )
    )
    return rows_to_dataframe(response, dimensions, ["activeUsers"])

def optional_report(label: str, *args, **kwargs) -> tuple[pd.DataFrame, str | None]:
    try:
        return run_report(*args, **kwargs), None
    except Exception as exc:
        return pd.DataFrame(), f"{label} unavailable from GA4: {exc.__class__.__name__}: {exc}"


def load_dashboard_data(
    config: GaConfig,
    start: date,
    end: date,
    store_config_path: str | None = None,
    store_config: dict | None = None,
) -> dict[str, pd.DataFrame]:
    store_downloads = fetch_store_downloads(start, end, store_config or store_config_path)
    users_daily = run_report(
        config,
        dimensions=["date"],
        metrics=["activeUsers", "newUsers", "totalUsers"],
        start=start,
        end=end,
    )
    users_weekly = run_report(
        config,
        dimensions=["yearWeek"],
        metrics=["activeUsers", "newUsers", "totalUsers"],
        start=start,
        end=end,
    )
    users_monthly = run_report(
        config,
        dimensions=["yearMonth"],
        metrics=["activeUsers", "newUsers", "totalUsers"],
        start=start,
        end=end,
    )
    engagement_daily = run_report(
        config,
        dimensions=["date"],
        metrics=["userEngagementDuration", "activeUsers", "engagedSessions"],
        start=start,
        end=end,
    )
    engagement_weekly = run_report(
        config,
        dimensions=["yearWeek"],
        metrics=["userEngagementDuration", "activeUsers", "engagedSessions"],
        start=start,
        end=end,
    )
    engagement_monthly = run_report(
        config,
        dimensions=["yearMonth"],
        metrics=["userEngagementDuration", "activeUsers", "engagedSessions"],
        start=start,
        end=end,
    )
    summary = run_report(
        config,
        dimensions=[],
        metrics=["activeUsers", "newUsers", "totalUsers", "userEngagementDuration", "engagedSessions"],
        start=start,
        end=end,
    )
    device_models = run_report(
        config,
        dimensions=["deviceModel"],
        metrics=["activeUsers"],
        start=start,
        end=end,
        limit=8,
        order_metric="activeUsers",
    )
    growth_notes = []
    first_open_daily, note = optional_report(
        "First-open daily",
        config,
        dimensions=["date"],
        metrics=["eventCount"],
        start=start,
        end=end,
        event_name="first_open",
    )
    if note:
        growth_notes.append(note)
    first_open_weekly, note = optional_report(
        "First-open weekly",
        config,
        dimensions=["yearWeek"],
        metrics=["eventCount"],
        start=start,
        end=end,
        event_name="first_open",
    )
    if note:
        growth_notes.append(note)
    first_open_monthly, note = optional_report(
        "First-open monthly",
        config,
        dimensions=["yearMonth"],
        metrics=["eventCount"],
        start=start,
        end=end,
        event_name="first_open",
    )
    if note:
        growth_notes.append(note)
    acquisition_channels, note = optional_report(
        "Acquisition channels",
        config,
        dimensions=["firstUserDefaultChannelGroup"],
        metrics=["activeUsers", "newUsers", "totalUsers"],
        start=start,
        end=end,
        limit=10,
        order_metric="newUsers",
    )
    if note:
        growth_notes.append(note)
    acquisition_sources, note = optional_report(
        "Acquisition source / medium",
        config,
        dimensions=["firstUserSourceMedium"],
        metrics=["activeUsers", "newUsers"],
        start=start,
        end=end,
        limit=12,
        order_metric="newUsers",
    )
    if note:
        growth_notes.append(note)
    acquisition_campaigns, note = optional_report(
        "Acquisition campaigns",
        config,
        dimensions=["firstUserCampaignName"],
        metrics=["activeUsers", "newUsers"],
        start=start,
        end=end,
        limit=12,
        order_metric="newUsers",
    )
    if note:
        growth_notes.append(note)
    country_usage = run_report(
        config,
        dimensions=["country"],
        metrics=["activeUsers", "newUsers", "totalUsers", "engagedSessions", "userEngagementDuration"],
        start=start,
        end=end,
        limit=12,
        order_metric="activeUsers",
    )
    region_usage, note = optional_report(
        "Region usage",
        config,
        dimensions=["region"],
        metrics=["activeUsers", "newUsers"],
        start=start,
        end=end,
        limit=12,
        order_metric="activeUsers",
    )
    if note:
        growth_notes.append(note)
    device_category_usage = run_report(
        config,
        dimensions=["deviceCategory"],
        metrics=["activeUsers", "newUsers", "totalUsers"],
        start=start,
        end=end,
        limit=8,
        order_metric="activeUsers",
    )
    os_usage = run_report(
        config,
        dimensions=["operatingSystem"],
        metrics=["activeUsers", "newUsers", "totalUsers"],
        start=start,
        end=end,
        limit=8,
        order_metric="activeUsers",
    )
    app_version_usage, note = optional_report(
        "App version usage",
        config,
        dimensions=["appVersion"],
        metrics=["activeUsers", "newUsers", "totalUsers"],
        start=start,
        end=end,
        limit=10,
        order_metric="activeUsers",
    )
    if note:
        growth_notes.append(note)
    top_events = run_report(
        config,
        dimensions=["eventName"],
        metrics=["eventCount", "activeUsers"],
        start=start,
        end=end,
        limit=15,
        order_metric="eventCount",
    )
    device_categories = run_realtime_report(config, ["deviceCategory"], limit=8)
    top_countries = run_realtime_report(config, ["country"], limit=8)
    realtime_minutes = run_realtime_report(config, ["minutesAgo"], limit=30)
    realtime_summary = run_realtime_report(config, [], limit=1)
    return {
        "downloads_daily": store_downloads.downloads_daily,
        "download_notes": pd.DataFrame({"note": store_downloads.notes}),
        "users_daily": users_daily,
        "users_weekly": users_weekly,
        "users_monthly": users_monthly,
        "engagement_daily": engagement_daily,
        "engagement_weekly": engagement_weekly,
        "engagement_monthly": engagement_monthly,
        "summary": summary,
        "top_countries": top_countries,
        "device_models": device_models,
        "first_open_daily": first_open_daily,
        "first_open_weekly": first_open_weekly,
        "first_open_monthly": first_open_monthly,
        "acquisition_channels": acquisition_channels,
        "acquisition_sources": acquisition_sources,
        "acquisition_campaigns": acquisition_campaigns,
        "country_usage": country_usage,
        "region_usage": region_usage,
        "device_category_usage": device_category_usage,
        "os_usage": os_usage,
        "app_version_usage": app_version_usage,
        "top_events": top_events,
        "growth_notes": pd.DataFrame({"note": growth_notes}),
        "device_categories": device_categories,
        "realtime_minutes": realtime_minutes,
        "realtime_summary": realtime_summary,
    }


def normalize_date_column(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "date" not in df:
        return df
    output = df.copy()
    raw_dates = output["date"].astype(str)
    if raw_dates.str.fullmatch(r"\d{8}").all():
        output["date"] = pd.to_datetime(raw_dates, format="%Y%m%d")
    else:
        output["date"] = pd.to_datetime(output["date"])
    return output.sort_values("date")


def platform_label(platform: str) -> str:
    normalized = str(platform).lower()
    if normalized in {"ios", "macintosh", "apple"}:
        return "Apple"
    if normalized in {"android"}:
        return "Android"
    return platform or "Unknown"


def status_pill(label: str = "Live") -> None:
    st.markdown(f"<span class='status-pill'><span></span>{label}</span>", unsafe_allow_html=True)


def metric_card(label: str, value: str, help_text: str | None = None) -> None:
    st.markdown("<div class='metric-card'>", unsafe_allow_html=True)
    st.metric(label, value, help=help_text)
    st.markdown("</div>", unsafe_allow_html=True)


def render_ranked_table(
    df: pd.DataFrame,
    label_column: str,
    metric_column: str = "activeUsers",
    empty_message: str = "No data available.",
) -> None:
    if df.empty:
        st.caption(empty_message)
        return
    table_df = df[[label_column, metric_column]].copy()
    label_map = {
        "country": "Country",
        "deviceModel": "Device Model",
        "deviceCategory": "Device Category",
    }
    table_df.columns = [label_map.get(label_column, label_column.replace("_", " ").title()), "Active Users"]
    st.dataframe(
        table_df,
        hide_index=True,
        width="stretch",
        column_config={"Active Users": st.column_config.NumberColumn(format="%d")},
    )


def render_realtime_card(data: dict[str, pd.DataFrame]) -> None:
    top_countries = data["top_countries"]
    realtime_minutes = data["realtime_minutes"].copy()
    realtime_summary = data["realtime_summary"]
    active_total = int(realtime_summary["activeUsers"].iloc[0]) if not realtime_summary.empty else 0

    with st.container(border=True):
        left, right = st.columns([1, 0.16])
        with left:
            st.caption("ACTIVE USERS IN LAST 30 MINUTES")
            st.markdown(f"<div class='big-number'>{active_total:,}</div>", unsafe_allow_html=True)
        with right:
            status_pill()

        st.caption("ACTIVE USERS PER MINUTE")
        if not realtime_minutes.empty:
            realtime_minutes["minutesAgo"] = realtime_minutes["minutesAgo"].astype(int)
            realtime_minutes = realtime_minutes.sort_values("minutesAgo", ascending=False)
            fig = px.bar(realtime_minutes, x="minutesAgo", y="activeUsers", color_discrete_sequence=["#1a73e8"])
            fig.update_traces(
                hovertemplate="<b>%{y:,} active users</b><br>%{x} minutes ago<extra></extra>"
            )
            fig.update_layout(
                height=110,
                margin=dict(l=0, r=0, t=2, b=0),
                xaxis=dict(visible=False),
                yaxis=dict(visible=False),
                bargap=0.15,
                showlegend=False,
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            st.info("No app users active in the last 30 minutes.")

        st.caption("TOP COUNTRIES")
        render_ranked_table(
            top_countries,
            "country",
            empty_message="No app users active in this realtime window.",
        )


def normalize_breakdown_mode(breakdown_mode: str | None) -> str:
    return breakdown_mode if breakdown_mode in DOWNLOAD_BREAKDOWN_MODES else DOWNLOAD_BREAKDOWN_MODES[0]


def add_period_column(df: pd.DataFrame, breakdown_mode: str) -> pd.DataFrame:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    if breakdown_mode == "Weekly":
        return df.assign(period=df["date"].dt.to_period("W-SUN").dt.start_time)
    if breakdown_mode == "Monthly":
        return df.assign(period=df["date"].dt.to_period("M").dt.start_time)
    return df.assign(period=df["date"])


def normalize_period_report(df: pd.DataFrame, period_column: str) -> pd.DataFrame:
    if df.empty or period_column not in df:
        return df

    output = df.copy()
    values = output[period_column].astype(str)
    if period_column == "yearMonth":
        output["period"] = pd.to_datetime(values + "01", format="%Y%m%d", errors="coerce")
    elif period_column == "yearWeek":
        output["period"] = pd.to_datetime(values.str.zfill(6) + "1", format="%G%V%u", errors="coerce")
    return output.dropna(subset=["period"]).sort_values("period")


def engagement_for_chart(data: dict[str, pd.DataFrame], breakdown_mode: str) -> pd.DataFrame:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    if breakdown_mode == "Weekly":
        df = normalize_period_report(data.get("engagement_weekly", pd.DataFrame()), "yearWeek")
    elif breakdown_mode == "Monthly":
        df = normalize_period_report(data.get("engagement_monthly", pd.DataFrame()), "yearMonth")
    else:
        df = normalize_date_column(data.get("engagement_daily", pd.DataFrame()))
        if not df.empty:
            df = df.assign(period=df["date"])

    if df.empty:
        return df

    output = df.copy()
    output["average_engagement_seconds"] = (
        output["userEngagementDuration"] / output["activeUsers"].replace(0, pd.NA)
    ).fillna(0).round(0)
    return output


def render_engagement_chart(data: dict[str, pd.DataFrame], breakdown_mode: str) -> None:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    df = engagement_for_chart(data, breakdown_mode)
    if df.empty:
        st.info("No engagement data returned for this date range.")
        return

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["period"],
            y=df["average_engagement_seconds"],
            mode="lines",
            name="Avg engagement seconds",
            line=dict(color="#1a73e8", width=2),
            hovertemplate="<b>%{y:.0f}s avg engagement</b><br>%{x|%b %-d, %Y}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=df["period"],
            y=df["engagedSessions"],
            name="Engaged sessions",
            marker_color="#8a4b08",
            opacity=0.5,
            yaxis="y2",
            hovertemplate="<b>%{y:,} engaged sessions</b><br>%{x|%b %-d, %Y}<extra></extra>",
        )
    )
    fig.update_layout(
        height=310,
        margin=dict(l=0, r=0, t=8, b=0),
        legend=dict(orientation="h", y=1.12),
        yaxis=dict(title="Seconds"),
        yaxis2=dict(title="Sessions", overlaying="y", side="right", showgrid=False),
        hovermode="x unified",
        xaxis_title=breakdown_mode,
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def aggregate_downloads_for_chart(downloads_daily: pd.DataFrame, breakdown_mode: str) -> pd.DataFrame:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    df = normalize_date_column(downloads_daily)
    if df.empty:
        return df

    df = add_period_column(df, breakdown_mode)
    return df.groupby(["period", "store"], as_index=False)["downloads"].sum()

def render_downloads(downloads_daily: pd.DataFrame, breakdown_mode: str) -> None:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    df = normalize_date_column(downloads_daily)
    if df.empty:
        st.info("No store download data returned for this date range.")
        return

    totals = df.groupby("store", as_index=False)["downloads"].sum()
    apple_total = int(totals.loc[totals["store"].eq("Apple"), "downloads"].sum())
    android_total = int(totals.loc[totals["store"].eq("Android"), "downloads"].sum())
    total_cols = st.columns(3)
    with total_cols[0]:
        st.metric("Apple downloads", f"{apple_total:,}")
    with total_cols[1]:
        st.metric("Android downloads", f"{android_total:,}")
    with total_cols[2]:
        st.metric("Store total", f"{apple_total + android_total:,}")

    left, right = st.columns([1.4, 1])
    with left:
        chart_df = aggregate_downloads_for_chart(df, breakdown_mode)
        fig = px.line(
            chart_df,
            x="period",
            y="downloads",
            color="store",
            markers=True,
            labels={"downloads": "Downloads", "store": "Store", "period": breakdown_mode},
            color_discrete_map={"Apple": "#1a73e8", "Android": "#34a853"},
        )
        fig.update_traces(hovertemplate="<b>%{y:,} downloads</b><br>%{x|%b %-d, %Y}<extra>%{fullData.name}</extra>")
        if df["date"].min().date() <= DISPLAY_AD_START_DATE <= df["date"].max().date():
            marker_date = pd.Timestamp(DISPLAY_AD_START_DATE)
            fig.add_shape(
                type="line",
                x0=marker_date,
                x1=marker_date,
                y0=0,
                y1=1,
                xref="x",
                yref="paper",
                line=dict(color="#ff6b6b", dash="dash", width=2),
            )
            fig.add_annotation(
                x=marker_date,
                y=1,
                xref="x",
                yref="paper",
                text="Display ads started",
                showarrow=False,
                xanchor="left",
                yanchor="bottom",
                font=dict(color="#ff6b6b"),
            )
        fig.update_layout(
            height=320,
            margin=dict(l=0, r=0, t=8, b=0),
            legend=dict(orientation="h", y=1.12),
            hovermode="x unified",
            xaxis_title=breakdown_mode,
        )
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    with right:
        platform_totals = df.groupby("store", as_index=False)["downloads"].sum()
        fig = px.pie(
            platform_totals,
            names="store",
            values="downloads",
            hole=0.55,
            color="store",
            color_discrete_map={"Apple": "#1a73e8", "Android": "#34a853"},
        )
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=8, b=0), showlegend=True)
        fig.update_traces(hovertemplate="<b>%{label}</b><br>%{value:,} downloads<br>%{percent}<extra></extra>")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def user_trends_for_chart(data: dict[str, pd.DataFrame], breakdown_mode: str) -> pd.DataFrame:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    if breakdown_mode == "Weekly":
        df = normalize_period_report(data.get("users_weekly", pd.DataFrame()), "yearWeek")
    elif breakdown_mode == "Monthly":
        df = normalize_period_report(data.get("users_monthly", pd.DataFrame()), "yearMonth")
    else:
        df = normalize_date_column(data.get("users_daily", pd.DataFrame()))
        if not df.empty:
            df = df.assign(period=df["date"])

    if df.empty:
        return df
    return df[["period", "activeUsers", "newUsers"]].sort_values("period")


def render_user_trends(data: dict[str, pd.DataFrame], breakdown_mode: str) -> None:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    df = user_trends_for_chart(data, breakdown_mode)
    if df.empty:
        st.info("No user trend data returned for this date range.")
        return
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["period"],
            y=df["activeUsers"],
            mode="lines+markers",
            name="Active users",
            line=dict(color="#1a73e8", width=2),
            marker=dict(size=6),
            hovertemplate="<b>Active users</b><br>%{x|%b %-d, %Y}<br>%{y:,} users<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["period"],
            y=df["newUsers"],
            mode="lines+markers",
            name="New users",
            line=dict(color="#fbbc04", width=2),
            marker=dict(size=6),
            hovertemplate="<b>New users</b><br>%{x|%b %-d, %Y}<br>%{y:,} users<extra></extra>",
        )
    )
    fig.update_layout(
        height=320,
        margin=dict(l=0, r=0, t=8, b=0),
        legend=dict(orientation="h", y=1.12),
        xaxis_title=breakdown_mode,
        yaxis_title="Users",
        hovermode="x unified",
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

def render_device_models(device_models: pd.DataFrame) -> None:
    with st.container(border=True):
        st.markdown("**Active users by device model**")
        st.caption("Selected date range")
        render_ranked_table(device_models, "deviceModel")


def render_device_categories(device_categories: pd.DataFrame) -> None:
    with st.container(border=True):
        header_left, header_right = st.columns([1, 0.2])
        with header_left:
            st.markdown("**Realtime users by device category**")
        with header_right:
            status_pill()
        render_ranked_table(
            device_categories,
            "deviceCategory",
            empty_message="No app users active in this realtime window.",
        )



def humanize_label(value: object) -> str:
    text = str(value or "Unknown")
    if text in {"(not set)", ""}:
        return "Unknown"
    return text


def render_data_table(
    df: pd.DataFrame,
    columns: list[str],
    labels: dict[str, str],
    empty_message: str = "No data available.",
) -> None:
    if df.empty:
        st.caption(empty_message)
        return
    visible_columns = [column for column in columns if column in df]
    if not visible_columns:
        st.caption(empty_message)
        return
    table_df = df[visible_columns].copy()
    for column in visible_columns:
        if table_df[column].dtype == "object":
            table_df[column] = table_df[column].map(humanize_label)
    table_df = table_df.rename(columns=labels)
    number_columns = {
        labels.get(column, column): st.column_config.NumberColumn(format="%d")
        for column in visible_columns
        if column not in labels or labels.get(column, column) != labels.get(visible_columns[0], visible_columns[0])
    }
    st.dataframe(table_df, hide_index=True, width="stretch", column_config=number_columns)


def render_horizontal_bar(
    df: pd.DataFrame,
    dimension: str,
    metric: str,
    title: str,
    metric_label: str,
    color: str = "#1a73e8",
) -> None:
    with st.container(border=True):
        st.markdown(f"**{title}**")
        if df.empty or dimension not in df or metric not in df:
            st.caption("No data available.")
            return
        chart_df = df[[dimension, metric]].copy().head(10)
        chart_df[dimension] = chart_df[dimension].map(humanize_label)
        chart_df = chart_df.sort_values(metric, ascending=True)
        fig = px.bar(
            chart_df,
            x=metric,
            y=dimension,
            orientation="h",
            labels={metric: metric_label, dimension: ""},
            color_discrete_sequence=[color],
        )
        fig.update_traces(hovertemplate=f"<b>%{{y}}</b><br>%{{x:,}} {metric_label.lower()}<extra></extra>")
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=8, b=0), showlegend=False)
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def first_open_for_chart(data: dict[str, pd.DataFrame], breakdown_mode: str) -> pd.DataFrame:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    if breakdown_mode == "Weekly":
        df = normalize_period_report(data.get("first_open_weekly", pd.DataFrame()), "yearWeek")
    elif breakdown_mode == "Monthly":
        df = normalize_period_report(data.get("first_open_monthly", pd.DataFrame()), "yearMonth")
    else:
        df = normalize_date_column(data.get("first_open_daily", pd.DataFrame()))
        if not df.empty:
            df = df.assign(period=df["date"])
    if df.empty or "eventCount" not in df:
        return pd.DataFrame(columns=["period", "first_opens"])
    return df[["period", "eventCount"]].rename(columns={"eventCount": "first_opens"}).sort_values("period")


def store_to_open_for_chart(data: dict[str, pd.DataFrame], breakdown_mode: str) -> pd.DataFrame:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    downloads = aggregate_downloads_for_chart(data.get("downloads_daily", pd.DataFrame()), breakdown_mode)
    if downloads.empty:
        downloads = pd.DataFrame(columns=["period", "store_downloads"])
    else:
        downloads = downloads.groupby("period", as_index=False)["downloads"].sum().rename(columns={"downloads": "store_downloads"})
    first_opens = first_open_for_chart(data, breakdown_mode)
    chart_df = downloads.merge(first_opens, on="period", how="outer").sort_values("period")
    if chart_df.empty:
        return chart_df
    chart_df[["store_downloads", "first_opens"]] = chart_df[["store_downloads", "first_opens"]].fillna(0).astype(int)
    return chart_df


def render_store_to_open_signal(data: dict[str, pd.DataFrame], breakdown_mode: str) -> None:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    chart_df = store_to_open_for_chart(data, breakdown_mode)
    with st.container(border=True):
        st.markdown("**Store downloads vs first opens**")
        st.caption("Use this to spot gaps between store installs/downloads and app starts tracked in GA4.")
        if chart_df.empty:
            st.caption("No download or first-open data available for this date range.")
            return
        totals = chart_df[["store_downloads", "first_opens"]].sum()
        cols = st.columns(3)
        with cols[0]:
            st.metric("Store downloads", f"{int(totals['store_downloads']):,}")
        with cols[1]:
            st.metric("GA4 first opens", f"{int(totals['first_opens']):,}")
        with cols[2]:
            rate = totals["first_opens"] / totals["store_downloads"] * 100 if totals["store_downloads"] else 0
            st.metric("First opens / download", f"{rate:.0f}%" if totals["store_downloads"] else "n/a")
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=chart_df["period"],
                y=chart_df["store_downloads"],
                mode="lines+markers",
                name="Store downloads",
                line=dict(color="#34a853", width=2),
                hovertemplate="<b>Store downloads</b><br>%{x|%b %-d, %Y}<br>%{y:,}<extra></extra>",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=chart_df["period"],
                y=chart_df["first_opens"],
                mode="lines+markers",
                name="GA4 first opens",
                line=dict(color="#fbbc04", width=2),
                hovertemplate="<b>GA4 first opens</b><br>%{x|%b %-d, %Y}<br>%{y:,}<extra></extra>",
            )
        )
        fig.update_layout(
            height=300,
            margin=dict(l=0, r=0, t=8, b=0),
            legend=dict(orientation="h", y=1.12),
            hovermode="x unified",
            xaxis_title=breakdown_mode,
            yaxis_title="Count",
        )
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def render_retention_signal(data: dict[str, pd.DataFrame]) -> None:
    kpis = calculate_kpis(data)
    estimated_returning = max(int(kpis["active_users"] - kpis["new_users"]), 0)
    with st.container(border=True):
        st.markdown("**New vs returning usage**")
        st.caption("Estimated returning users are active users minus new users for the selected range.")
        cols = st.columns(3)
        with cols[0]:
            st.metric("New users", f"{int(kpis['new_users']):,}")
        with cols[1]:
            st.metric("Estimated returning", f"{estimated_returning:,}")
        with cols[2]:
            return_rate = estimated_returning / kpis["active_users"] * 100 if kpis["active_users"] else 0
            st.metric("Returning share", f"{return_rate:.0f}%")
        mix = pd.DataFrame(
            {"User type": ["New", "Estimated returning"], "Users": [int(kpis["new_users"]), estimated_returning]}
        )
        fig = px.bar(mix, x="User type", y="Users", color="User type", color_discrete_sequence=["#fbbc04", "#1a73e8"])
        fig.update_traces(hovertemplate="<b>%{x}</b><br>%{y:,} users<extra></extra>")
        fig.update_layout(height=230, margin=dict(l=0, r=0, t=8, b=0), showlegend=False)
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def render_store_listing_notes() -> None:
    with st.container(border=True):
        st.markdown("**Store listing funnel**")
        st.caption("Downloads are connected. Store impressions, product-page views, conversion rate, ratings, and reviews need separate App Store Connect / Google Play listing analytics feeds.")
        st.write("Add these when available: listing impressions, product page views, store conversion rate, rating, review count, crashes, and app version adoption.")


def render_growth_diagnostics(data: dict[str, pd.DataFrame], breakdown_mode: str) -> None:
    breakdown_mode = normalize_breakdown_mode(breakdown_mode)
    st.subheader("Growth diagnostics")
    st.caption("Signals for deciding whether to focus on awareness, store-page conversion, targeting, or post-install usage.")

    left, right = st.columns([1.2, 0.8])
    with left:
        render_store_to_open_signal(data, breakdown_mode)
    with right:
        render_retention_signal(data)

    acq_left, acq_right = st.columns([1, 1])
    with acq_left:
        render_horizontal_bar(
            data.get("acquisition_channels", pd.DataFrame()),
            "firstUserDefaultChannelGroup",
            "newUsers",
            "New users by acquisition channel",
            "New users",
            "#34a853",
        )
    with acq_right:
        with st.container(border=True):
            st.markdown("**Top source / medium**")
            render_data_table(
                data.get("acquisition_sources", pd.DataFrame()),
                ["firstUserSourceMedium", "newUsers", "activeUsers"],
                {"firstUserSourceMedium": "Source / Medium", "newUsers": "New Users", "activeUsers": "Active Users"},
            )

    detail_left, detail_right = st.columns([1, 1])
    with detail_left:
        with st.container(border=True):
            st.markdown("**Campaigns**")
            render_data_table(
                data.get("acquisition_campaigns", pd.DataFrame()),
                ["firstUserCampaignName", "newUsers", "activeUsers"],
                {"firstUserCampaignName": "Campaign", "newUsers": "New Users", "activeUsers": "Active Users"},
            )
    with detail_right:
        render_store_listing_notes()

    audience_left, audience_right = st.columns([1, 1])
    with audience_left:
        render_horizontal_bar(
            data.get("country_usage", pd.DataFrame()),
            "country",
            "activeUsers",
            "Active users by country",
            "Active users",
            "#1a73e8",
        )
    with audience_right:
        with st.container(border=True):
            st.markdown("**Device and app health**")
            st.caption("Selected date range")
            tabs = st.tabs(["Device", "OS", "Version"])
            with tabs[0]:
                render_data_table(
                    data.get("device_category_usage", pd.DataFrame()),
                    ["deviceCategory", "activeUsers", "newUsers", "totalUsers"],
                    {"deviceCategory": "Device", "activeUsers": "Active Users", "newUsers": "New Users", "totalUsers": "Total Users"},
                )
            with tabs[1]:
                render_data_table(
                    data.get("os_usage", pd.DataFrame()),
                    ["operatingSystem", "activeUsers", "newUsers", "totalUsers"],
                    {"operatingSystem": "OS", "activeUsers": "Active Users", "newUsers": "New Users", "totalUsers": "Total Users"},
                )
            with tabs[2]:
                render_data_table(
                    data.get("app_version_usage", pd.DataFrame()),
                    ["appVersion", "activeUsers", "newUsers", "totalUsers"],
                    {"appVersion": "App Version", "activeUsers": "Active Users", "newUsers": "New Users", "totalUsers": "Total Users"},
                )

    event_left, event_right = st.columns([1, 1])
    with event_left:
        with st.container(border=True):
            st.markdown("**Top app events**")
            render_data_table(
                data.get("top_events", pd.DataFrame()),
                ["eventName", "eventCount", "activeUsers"],
                {"eventName": "Event", "eventCount": "Event Count", "activeUsers": "Active Users"},
            )
    with event_right:
        with st.container(border=True):
            st.markdown("**Regional response**")
            render_data_table(
                data.get("region_usage", pd.DataFrame()),
                ["region", "activeUsers", "newUsers"],
                {"region": "Region", "activeUsers": "Active Users", "newUsers": "New Users"},
            )

    notes = data.get("growth_notes", pd.DataFrame())
    if not notes.empty:
        with st.expander("Growth diagnostics source notes"):
            for note in notes["note"].dropna().tolist():
                st.caption(note)


def calculate_kpis(data: dict[str, pd.DataFrame]) -> dict[str, float]:
    downloads_daily = data.get("downloads_daily", pd.DataFrame())
    summary = data.get("summary", pd.DataFrame())

    downloads_total = int(downloads_daily["downloads"].sum()) if not downloads_daily.empty else 0
    active_users = int(summary["activeUsers"].iloc[0]) if not summary.empty else 0
    new_users = int(summary["newUsers"].iloc[0]) if not summary.empty else 0
    total_users = int(summary["totalUsers"].iloc[0]) if not summary.empty else 0
    avg_engagement = (
        float(summary["userEngagementDuration"].iloc[0]) / active_users
        if active_users and not summary.empty
        else 0
    )
    return {
        "downloads": downloads_total,
        "active_users": active_users,
        "total_users": total_users,
        "new_users": new_users,
        "avg_engagement": avg_engagement,
    }


def render_primary_kpis(kpis: dict[str, float]) -> None:
    metric_cols = st.columns(5)
    with metric_cols[0]:
        metric_card("Store downloads", f"{int(kpis['downloads']):,}", "Apple App Store + Google Play")
    with metric_cols[1]:
        metric_card("Active users", f"{int(kpis['active_users']):,}")
    with metric_cols[2]:
        metric_card("Total users", f"{int(kpis['total_users']):,}")
    with metric_cols[3]:
        metric_card("New users", f"{int(kpis['new_users']):,}")
    with metric_cols[4]:
        metric_card("Avg engagement / active user", f"{kpis['avg_engagement']:.0f}s")


def format_delta(current: float, comparison: float, suffix: str = "") -> str:
    delta = current - comparison
    sign = "+" if delta >= 0 else ""
    if suffix:
        value = f"{sign}{delta:.0f}{suffix}"
    else:
        value = f"{sign}{delta:,.0f}"
    if comparison:
        percent = delta / comparison * 100
        value += f" ({sign}{percent:.1f}%)"
    return value


def render_comparison_kpis(current: dict[str, float], comparison: dict[str, float]) -> None:
    st.subheader("KPI comparison")
    st.caption("Current selected period compared with the comparison period.")
    cols = st.columns(5)
    metrics = [
        ("Store downloads", "downloads", ""),
        ("Active users", "active_users", ""),
        ("Total users", "total_users", ""),
        ("New users", "new_users", ""),
        ("Avg engagement", "avg_engagement", "s"),
    ]
    for col, (label, key, suffix) in zip(cols, metrics):
        with col:
            value = f"{current[key]:.0f}{suffix}" if suffix else f"{int(current[key]):,}"
            st.metric(label, value, delta=format_delta(current[key], comparison[key], suffix))

def apply_styles() -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.5rem;
            max-width: 1180px;
        }
        .big-number {
            color: #202124;
            font-size: 3.1rem;
            line-height: 1;
            margin: -0.25rem 0 1rem;
        }
        .status-pill {
            align-items: center;
            border: 1px solid #dadce0;
            border-radius: 999px;
            color: #3c4043;
            display: inline-flex;
            font-size: 0.8rem;
            gap: 0.35rem;
            padding: 0.28rem 0.55rem;
            white-space: nowrap;
        }
        .status-pill span {
            background: #34a853;
            border-radius: 50%;
            display: inline-block;
            height: 0.55rem;
            width: 0.55rem;
        }
        [data-testid="stMetricValue"] {
            font-size: 2rem;
        }
        [data-testid="stDataFrame"] {
            border: 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title=f"{APP_NAME} Analytics", page_icon=":bar_chart:", layout="wide")
    apply_styles()

    st.title(f"{APP_NAME} Analytics")
    st.caption("App Store Connect, Google Play, and Google Analytics usage dashboard")

    property_id = get_property_id()
    store_config_path = get_store_config_path()
    store_config = get_store_config()
    ga_service_account_info = get_ga_service_account_info()
    service_account_file = get_service_account_file()
    if not ga_service_account_info and not service_account_file.exists():
        st.error(f"Service account file not found: {service_account_file}")
        st.stop()

    with st.sidebar:
        st.header("Controls")
        st.caption(f"Build: {DEPLOY_VERSION}")
        today = date.today()
        default_start = today - timedelta(days=30)
        start = st.date_input("Start date", value=default_start, max_value=today)
        end = st.date_input("End date", value=today, max_value=today)
        download_breakdown = st.segmented_control(
            "Line chart breakdown",
            DOWNLOAD_BREAKDOWN_MODES,
            default="Daily",
        )
        download_breakdown = normalize_breakdown_mode(download_breakdown)
        compare_enabled = st.checkbox("Compare with another period")
        comparison_start = comparison_end = None
        if compare_enabled:
            previous_end = default_start - timedelta(days=1)
            previous_start = previous_end - (end - start)
            comparison_start = st.date_input("Comparison start", value=previous_start, max_value=today)
            comparison_end = st.date_input("Comparison end", value=previous_end, max_value=today)
        st.caption("Downloads come from App Store Connect and Google Play.")
        refresh = st.button("Refresh data", type="primary", width="stretch")

    if start > end:
        st.warning("Start date must be on or before end date.")
        st.stop()

    if compare_enabled and comparison_start and comparison_end and comparison_start > comparison_end:
        st.warning("Comparison start date must be on or before comparison end date.")
        st.stop()

    if refresh:
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()

    if not property_id:
        st.warning("Add `GA_PROPERTY_ID` in your environment or `.streamlit/secrets.toml` to load data.")
        st.stop()

    config = GaConfig(
        property_id=property_id,
        service_account_file=None if ga_service_account_info else service_account_file,
        service_account_info=ga_service_account_info,
    )

    with st.spinner("Loading app store and Google Analytics data..."):
        try:
            data = load_dashboard_data(config, start, end, store_config_path, store_config)
        except Exception as exc:
            st.error("Dashboard data could not be loaded.")
            st.exception(exc)
            st.stop()

    current_kpis = calculate_kpis(data)
    render_primary_kpis(current_kpis)

    if compare_enabled and comparison_start and comparison_end:
        with st.spinner("Loading comparison period..."):
            try:
                comparison_data = load_dashboard_data(
                    config,
                    comparison_start,
                    comparison_end,
                    store_config_path,
                    store_config,
                )
            except Exception as exc:
                st.error("Comparison data could not be loaded.")
                st.exception(exc)
                st.stop()
        render_comparison_kpis(current_kpis, calculate_kpis(comparison_data))

    top_left, top_right = st.columns([1.1, 0.9])
    with top_left:
        st.subheader("Realtime")
        render_realtime_card(data)
    with top_right:
        st.subheader("Device models")
        render_device_models(data["device_models"])
        render_device_categories(data["device_categories"])

    st.subheader("Downloads and platform breakdown")
    st.info(
        "Display ads on advancedmanufacturing.org started August 6, 2026 for targeted devices "
        f"to drive app downloads and are planned to run through {DISPLAY_AD_END_NOTE}. "
        "Organic social promotion is also active; paid social has not started yet."
    )
    render_downloads(data["downloads_daily"], download_breakdown)
    with st.expander("Download source notes"):
        notes = data["download_notes"]
        if notes.empty:
            st.caption("No download source notes.")
        else:
            for note in notes["note"].tolist():
                st.caption(note)

    st.subheader("User trends")
    render_user_trends(data, download_breakdown)

    st.subheader("Engagement")
    render_engagement_chart(data, download_breakdown)

    render_growth_diagnostics(data, download_breakdown)

if __name__ == "__main__":
    main()
