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
CONFIG_DIR = Path(__file__).parent / "config"
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
APP_PLATFORMS = ["Android", "iOS"]


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


def get_store_config() -> dict | None:
    google_play = secrets_section("google_play")
    app_store = secrets_section("app_store")
    if not google_play and not app_store:
        return None
    return {
        "auth_mode": google_play.get("auth_mode", "service_account"),
        "google_play": google_play,
        "app_store": app_store,
    }


def get_ga_service_account_info() -> dict | None:
    return secrets_section("ga4_service_account") or None


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
    realtime_summary = run_realtime_report(config, [], limit=1)
    return {
        "downloads_daily": store_downloads.downloads_daily,
        "download_notes": pd.DataFrame({"note": store_downloads.notes}),
        "users_daily": users_daily,
        "engagement_daily": engagement_daily,
        "summary": summary,
        "top_countries": top_countries,
        "device_models": device_models,
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
        use_container_width=True,
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
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("No app users active in the last 30 minutes.")

        st.caption("TOP COUNTRIES")
        render_ranked_table(
            top_countries,
            "country",
            empty_message="No app users active in this realtime window.",
        )


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
            hovertemplate="<b>%{y:.0f}s avg engagement</b><br>%{x|%b %-d, %Y}<extra></extra>",
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
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_downloads(downloads_daily: pd.DataFrame) -> None:
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
        fig = px.line(
            df.groupby(["date", "store"], as_index=False)["downloads"].sum(),
            x="date",
            y="downloads",
            color="store",
            markers=True,
            labels={"downloads": "Downloads", "store": "Store"},
            color_discrete_map={"Apple": "#1a73e8", "Android": "#34a853"},
        )
        fig.update_traces(hovertemplate="<b>%{y:,} downloads</b><br>%{x|%b %-d, %Y}<extra>%{fullData.name}</extra>")
        fig.update_layout(
            height=320,
            margin=dict(l=0, r=0, t=8, b=0),
            legend=dict(orientation="h", y=1.12),
            hovermode="x unified",
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
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
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_user_trends(users_daily: pd.DataFrame) -> None:
    df = normalize_date_column(users_daily)
    if df.empty:
        st.info("No user trend data returned for this date range.")
        return
    trend_df = df.melt(
        id_vars="date",
        value_vars=["activeUsers", "newUsers"],
        var_name="metric",
        value_name="users",
    )
    trend_df["metric"] = trend_df["metric"].map({"activeUsers": "Active users", "newUsers": "New users"})
    fig = px.area(
        trend_df,
        x="date",
        y="users",
        color="metric",
        labels={"users": "Users", "metric": "Metric", "date": "Date"},
        color_discrete_sequence=["#1a73e8", "#fbbc04"],
    )
    fig.update_traces(hovertemplate="<b>%{y:,} users</b><br>%{x|%b %-d, %Y}<extra>%{fullData.name}</extra>")
    fig.update_layout(
        height=320,
        margin=dict(l=0, r=0, t=8, b=0),
        legend=dict(orientation="h", y=1.12),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


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
        today = date.today()
        default_start = today - timedelta(days=30)
        start, end = st.date_input("Date range", value=(default_start, today), max_value=today)
        st.caption("Downloads come from App Store Connect and Google Play.")
        refresh = st.button("Refresh data", type="primary", use_container_width=True)

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

    downloads_total = int(data["downloads_daily"]["downloads"].sum()) if not data["downloads_daily"].empty else 0
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
        metric_card("Store downloads", f"{downloads_total:,}", "Apple App Store + Google Play")
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
    with st.expander("Download source notes"):
        notes = data["download_notes"]
        if notes.empty:
            st.caption("No download source notes.")
        else:
            for note in notes["note"].tolist():
                st.caption(note)

    st.subheader("User trends")
    render_user_trends(data["users_daily"])

    st.subheader("Engagement")
    render_engagement_chart(data["engagement_daily"])


if __name__ == "__main__":
    main()
