from __future__ import annotations

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
    Metric,
    OrderBy,
    RunRealtimeReportRequest,
    RunReportRequest,
)
from google.oauth2 import service_account


APP_NAME = "Advanced Manufacturing App"
CONFIG_DIR = Path(__file__).parent / "config"
DOWNLOAD_EVENT_NAME = "first_open"
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


@dataclass(frozen=True)
class GaConfig:
    property_id: str
    service_account_file: Path


def get_property_id() -> str:
    try:
        secret_value = st.secrets.get("GA_PROPERTY_ID", "")
    except Exception:
        secret_value = ""
    return os.getenv("GA_PROPERTY_ID", secret_value).strip()


def get_service_account_file() -> Path:
    credential_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if credential_path:
        return Path(credential_path).expanduser()

    candidates = sorted(CONFIG_DIR.glob("*.json"))
    if candidates:
        return candidates[0]

    return CONFIG_DIR / "service-account.json"


@st.cache_resource(show_spinner=False)
def get_client(service_account_file: str) -> BetaAnalyticsDataClient:
    credentials = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=SCOPES,
    )
    return BetaAnalyticsDataClient(credentials=credentials)


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
) -> pd.DataFrame:
    request = RunReportRequest(
        property=f"properties/{config.property_id}",
        dimensions=[Dimension(name=name) for name in dimensions],
        metrics=[Metric(name=name) for name in metrics],
        date_ranges=[DateRange(start_date=date_range_label(start, end)[0], end_date=date_range_label(start, end)[1])],
        limit=limit,
    )
    if event_name:
        request.dimension_filter = FilterExpression(
            filter=Filter(
                field_name="eventName",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT,
                    value=event_name,
                ),
            )
        )
    if order_metric:
        request.order_bys = [OrderBy(metric=OrderBy.MetricOrderBy(metric_name=order_metric), desc=True)]

    response = get_client(str(config.service_account_file)).run_report(request)
    return rows_to_dataframe(response, dimensions, metrics)


def run_realtime_report(config: GaConfig, dimensions: list[str], limit: int = 10) -> pd.DataFrame:
    response = get_client(str(config.service_account_file)).run_realtime_report(
        RunRealtimeReportRequest(
            property=f"properties/{config.property_id}",
            dimensions=[Dimension(name=name) for name in dimensions],
            metrics=[Metric(name="activeUsers")],
            limit=limit,
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="activeUsers"), desc=True)],
        )
    )
    return rows_to_dataframe(response, dimensions, ["activeUsers"])


@st.cache_data(ttl=300, show_spinner=False)
def load_dashboard_data(config: GaConfig, start: date, end: date) -> dict[str, pd.DataFrame]:
    downloads_daily = run_report(
        config,
        dimensions=["date", "operatingSystem"],
        metrics=["eventCount"],
        start=start,
        end=end,
        event_name=DOWNLOAD_EVENT_NAME,
    )
    users_daily = run_report(
        config,
        dimensions=["date"],
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
    device_categories = run_realtime_report(config, ["deviceCategory"], limit=8)
    top_countries = run_realtime_report(config, ["country"], limit=8)
    realtime_minutes = run_realtime_report(config, ["minutesAgo"], limit=30)
    return {
        "downloads_daily": downloads_daily,
        "users_daily": users_daily,
        "engagement_daily": engagement_daily,
        "summary": summary,
        "top_countries": top_countries,
        "device_models": device_models,
        "device_categories": device_categories,
        "realtime_minutes": realtime_minutes,
    }


def normalize_date_column(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "date" not in df:
        return df
    output = df.copy()
    output["date"] = pd.to_datetime(output["date"], format="%Y%m%d")
    return output


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


def render_ranked_table(df: pd.DataFrame, label_column: str, metric_column: str = "activeUsers") -> None:
    if df.empty:
        st.caption("No realtime data available.")
        return
    table_df = df[[label_column, metric_column]].copy()
    table_df.columns = [label_column.replace("_", " ").title(), "Active Users"]
    st.dataframe(
        table_df,
        hide_index=True,
        use_container_width=True,
        column_config={"Active Users": st.column_config.NumberColumn(format="%d")},
    )


def render_realtime_card(data: dict[str, pd.DataFrame]) -> None:
    top_countries = data["top_countries"]
    realtime_minutes = data["realtime_minutes"].copy()
    active_total = int(top_countries["activeUsers"].sum()) if not top_countries.empty else 0

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
            fig.update_layout(
                height=110,
                margin=dict(l=0, r=0, t=2, b=0),
                xaxis=dict(visible=False),
                yaxis=dict(visible=False),
                bargap=0.15,
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("Realtime minute data is not available yet.")

        st.caption("TOP COUNTRIES")
        render_ranked_table(top_countries, "country")


def render_engagement_chart(engagement_daily: pd.DataFrame) -> None:
    df = normalize_date_column(engagement_daily)
    if df.empty:
        st.info("No engagement data returned for this date range.")
        return

    df["average_engagement_seconds"] = (
        df["userEngagementDuration"] / df["activeUsers"].replace(0, pd.NA)
    ).fillna(0).round(0)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["average_engagement_seconds"],
            mode="lines",
            name="Avg engagement seconds",
            line=dict(color="#1a73e8", width=2),
        )
    )
    fig.add_trace(
        go.Bar(
            x=df["date"],
            y=df["engagedSessions"],
            name="Engaged sessions",
            marker_color="#8a4b08",
            opacity=0.5,
            yaxis="y2",
        )
    )
    fig.update_layout(
        height=310,
        margin=dict(l=0, r=0, t=8, b=0),
        legend=dict(orientation="h", y=1.12),
        yaxis=dict(title="Seconds"),
        yaxis2=dict(title="Sessions", overlaying="y", side="right", showgrid=False),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_downloads(downloads_daily: pd.DataFrame) -> None:
    df = normalize_date_column(downloads_daily)
    if df.empty:
        st.info("No install/download events returned for this date range.")
        return
    df["store"] = df["operatingSystem"].map(platform_label)

    left, right = st.columns([1.4, 1])
    with left:
        fig = px.line(
            df.groupby(["date", "store"], as_index=False)["eventCount"].sum(),
            x="date",
            y="eventCount",
            color="store",
            markers=True,
            labels={"eventCount": "Downloads", "store": "Platform"},
            color_discrete_map={"Apple": "#1a73e8", "Android": "#34a853"},
        )
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=8, b=0), legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    with right:
        platform_totals = df.groupby("store", as_index=False)["eventCount"].sum()
        fig = px.pie(
            platform_totals,
            names="store",
            values="eventCount",
            hole=0.55,
            color="store",
            color_discrete_map={"Apple": "#1a73e8", "Android": "#34a853"},
        )
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=8, b=0), showlegend=True)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_user_trends(users_daily: pd.DataFrame) -> None:
    df = normalize_date_column(users_daily)
    if df.empty:
        st.info("No user trend data returned for this date range.")
        return
    fig = px.area(
        df,
        x="date",
        y=["activeUsers", "newUsers"],
        labels={"value": "Users", "variable": "Metric"},
        color_discrete_sequence=["#1a73e8", "#fbbc04"],
    )
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=8, b=0), legend=dict(orientation="h", y=1.12))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_device_models(device_models: pd.DataFrame) -> None:
    with st.container(border=True):
        header_left, header_right = st.columns([1, 0.2])
        with header_left:
            st.markdown("**Active users by device model**")
        with header_right:
            status_pill()
        render_ranked_table(device_models, "deviceModel")


def render_device_categories(device_categories: pd.DataFrame) -> None:
    with st.container(border=True):
        header_left, header_right = st.columns([1, 0.2])
        with header_left:
            st.markdown("**Realtime users by device category**")
        with header_right:
            status_pill()
        render_ranked_table(device_categories, "deviceCategory")


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
    st.caption("Google Analytics 4 app usage dashboard")

    property_id = get_property_id()
    service_account_file = get_service_account_file()
    if not service_account_file.exists():
        st.error(f"Service account file not found: {service_account_file}")
        st.stop()

    with st.sidebar:
        st.header("Controls")
        today = date.today()
        default_start = today - timedelta(days=30)
        start, end = st.date_input("Date range", value=(default_start, today), max_value=today)
        st.caption("Uses GA4 `first_open` events as installs/downloads.")
        refresh = st.button("Refresh data", type="primary", use_container_width=True)

    if refresh:
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()

    if not property_id:
        st.warning("Add `GA_PROPERTY_ID` in your environment or `.streamlit/secrets.toml` to load data.")
        st.stop()

    config = GaConfig(property_id=property_id, service_account_file=service_account_file)

    with st.spinner("Loading Google Analytics data..."):
        try:
            data = load_dashboard_data(config, start, end)
        except Exception as exc:
            st.error("Google Analytics could not be loaded.")
            st.exception(exc)
            st.stop()

    downloads_total = int(data["downloads_daily"]["eventCount"].sum()) if not data["downloads_daily"].empty else 0
    summary = data["summary"]
    active_users = int(summary["activeUsers"].iloc[0]) if not summary.empty else 0
    new_users = int(summary["newUsers"].iloc[0]) if not summary.empty else 0
    total_users = int(summary["totalUsers"].iloc[0]) if not summary.empty else 0
    avg_engagement = (
        summary["userEngagementDuration"].iloc[0] / active_users
        if active_users and not summary.empty
        else 0
    )

    metric_cols = st.columns(4)
    with metric_cols[0]:
        metric_card("Downloads", f"{downloads_total:,}", f"`{DOWNLOAD_EVENT_NAME}` events")
    with metric_cols[1]:
        metric_card("Active users", f"{active_users:,}")
    with metric_cols[2]:
        metric_card("New users", f"{new_users:,}")
    with metric_cols[3]:
        metric_card("Avg engagement / active user", f"{avg_engagement:.0f}s", f"{total_users:,} total users")

    top_left, top_right = st.columns([1.1, 0.9])
    with top_left:
        st.subheader("Realtime")
        render_realtime_card(data)
    with top_right:
        st.subheader("Device models")
        render_device_models(data["device_models"])
        render_device_categories(data["device_categories"])

    st.subheader("Downloads and platform breakdown")
    render_downloads(data["downloads_daily"])

    st.subheader("User trends")
    render_user_trends(data["users_daily"])

    st.subheader("Engagement")
    render_engagement_chart(data["engagement_daily"])


if __name__ == "__main__":
    main()
