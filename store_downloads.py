from __future__ import annotations

import base64
import csv
import gzip
import io
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import pandas as pd
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_ROOT / "config"
SCORECARDS_CONFIG = PROJECT_ROOT.parent / "Scorecards" / "config" / "google_play_sources.json"
DEFAULT_CONFIG = CONFIG_DIR / "app_store_sources.json"
READONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
STORAGE_API_BASE = "https://storage.googleapis.com/storage/v1"
APP_STORE_API_BASE = "https://api.appstoreconnect.apple.com/v1"


class StoreDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoreDownloadResult:
    downloads_daily: pd.DataFrame
    notes: list[str]


def resolve_config_path() -> Path:
    configured = os.getenv("APP_STORES_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser()
    if SCORECARDS_CONFIG.exists():
        return SCORECARDS_CONFIG
    return DEFAULT_CONFIG


def load_store_config(config_path: str | Path | dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    if isinstance(config_path, dict):
        return config_path, "Streamlit secrets"

    path = Path(config_path).expanduser() if config_path else resolve_config_path()
    if not path.exists():
        raise StoreDownloadError(f"Store download config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle), str(path)


def each_month(start: date, end: date) -> list[date]:
    cursor = date(start.year, start.month, 1)
    months = []
    while cursor <= end:
        months.append(cursor)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def each_day(start: date, end: date) -> list[date]:
    days = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def make_google_credentials(config: dict[str, Any]) -> Any:
    play_config = config.get("google_play", {})
    auth_mode = play_config.get("auth_mode", config.get("auth_mode", "oauth"))
    if auth_mode == "service_account":
        service_account_info = play_config.get("service_account") or config.get("service_account")
        if service_account_info:
            return service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=[READONLY_SCOPE],
            )

        service_account_file = play_config.get("service_account_file") or config.get("service_account_file")
        if not service_account_file:
            raise StoreDownloadError("Google Play service account credentials are not configured.")
        return service_account.Credentials.from_service_account_file(
            str(Path(service_account_file).expanduser()),
            scopes=[READONLY_SCOPE],
        )

    if auth_mode == "oauth":
        oauth = config.get("oauth", {})
        token_file = Path(oauth.get("token_file", CONFIG_DIR / "google_play_oauth_token.json")).expanduser()
        credentials = None
        if token_file.exists():
            credentials = Credentials.from_authorized_user_file(str(token_file), [READONLY_SCOPE])
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(GoogleAuthRequest())
            else:
                client_secret_file = oauth.get("client_secret_file")
                if not client_secret_file:
                    raise StoreDownloadError("Google Play oauth.client_secret_file is not configured.")
                flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, scopes=[READONLY_SCOPE])
                credentials = flow.run_local_server(port=0)
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(credentials.to_json(), encoding="utf-8")
        return credentials

    raise StoreDownloadError(f"Unsupported Google Play auth_mode: {auth_mode}")


def google_storage_get_bytes(credentials: Any, bucket_id: str, object_name: str) -> bytes:
    if not credentials.valid:
        credentials.refresh(GoogleAuthRequest())
    request = Request(
        f"{STORAGE_API_BASE}/b/{bucket_id}/o/{quote(object_name, safe='')}?alt=media",
        headers={"Authorization": f"Bearer {credentials.token}"},
        method="GET",
    )
    with urlopen(request, timeout=60) as response:
        return response.read()


def parse_google_play_daily(csv_bytes: bytes, play_config: dict[str, Any]) -> pd.DataFrame:
    text = csv_bytes.decode(play_config.get("encoding", "utf-16"))
    reader = csv.DictReader(io.StringIO(text))
    metric_column = play_config.get("metric_column", "Daily User Installs")
    date_column = play_config.get("report_date_column", "Date")
    rows = []
    for row in reader:
        if metric_column not in row:
            available = ", ".join(row.keys())
            raise StoreDownloadError(
                f"Metric column '{metric_column}' not found in Google Play CSV. Available: {available}"
            )
        if date_column not in row:
            available = ", ".join(row.keys())
            raise StoreDownloadError(
                f"Date column '{date_column}' not found in Google Play CSV. Available: {available}"
            )
        raw_value = (row.get(metric_column) or "0").replace(",", "").strip()
        rows.append(
            {
                "date": pd.to_datetime(row[date_column]).date(),
                "store": "Android",
                "downloads": int(float(raw_value or 0)),
            }
        )
    return pd.DataFrame(rows, columns=["date", "store", "downloads"])


def google_play_object_name(play_config: dict[str, Any], month: date) -> str:
    package_name = play_config.get("package_name")
    template = play_config.get("report_object_template")
    if not package_name or not template:
        raise StoreDownloadError("Google Play config needs package_name and report_object_template.")
    return template.format(package_name=package_name, year_month=f"{month.year}{month.month:02d}")


def fetch_google_play_downloads(config: dict[str, Any], start: date, end: date) -> tuple[pd.DataFrame, list[str]]:
    play_config = config.get("google_play", {})
    if not play_config.get("enabled", False):
        return pd.DataFrame(columns=["date", "store", "downloads"]), ["Google Play is disabled in store config."]
    bucket_id = play_config.get("bucket_id")
    if not bucket_id:
        raise StoreDownloadError("Google Play bucket_id is not configured.")

    credentials = make_google_credentials(config)
    frames = []
    notes = []
    for month in each_month(start, end):
        object_name = google_play_object_name(play_config, month)
        try:
            report = google_storage_get_bytes(credentials, bucket_id, object_name)
        except HTTPError as error:
            if error.code == 404:
                notes.append(f"Google Play report not found for {month:%Y-%m}.")
                continue
            raise
        frame = parse_google_play_daily(report, play_config)
        frames.append(frame)
        notes.append(f"Loaded Google Play installs report for {month:%Y-%m}.")

    if not frames:
        return pd.DataFrame(columns=["date", "store", "downloads"]), notes
    output = pd.concat(frames, ignore_index=True)
    return filter_download_dates(output, start, end), notes


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def app_store_key_id(app_config: dict[str, Any]) -> str:
    if app_config.get("key_id"):
        return app_config["key_id"]
    key_file = Path(app_config.get("private_key_file", ""))
    if key_file.name.startswith("ApiKey_") and key_file.suffix == ".p8":
        return key_file.stem.removeprefix("ApiKey_")
    if key_file.name.startswith("AuthKey_") and key_file.suffix == ".p8":
        return key_file.stem.removeprefix("AuthKey_")
    raise StoreDownloadError("App Store Connect key_id is not configured.")


def app_store_private_key(app_config: dict[str, Any]) -> ec.EllipticCurvePrivateKey:
    private_key = app_config.get("private_key")
    if private_key:
        key = serialization.load_pem_private_key(private_key.encode("utf-8"), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise StoreDownloadError("App Store Connect private key must be an EC private key.")
        return key

    key_file = Path(app_config.get("private_key_file", "")).expanduser()
    if not key_file.exists():
        raise StoreDownloadError(f"App Store Connect private key file does not exist: {key_file}")
    key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise StoreDownloadError("App Store Connect private key must be an EC private key.")
    return key


def app_store_jwt(app_config: dict[str, Any]) -> str:
    issuer_id = app_config.get("issuer_id")
    key_type = app_config.get("key_type", "team")
    if key_type != "individual" and not issuer_id:
        raise StoreDownloadError("App Store Connect issuer_id is not configured.")

    now = int(datetime.now(timezone.utc).timestamp())
    header = {"alg": "ES256", "kid": app_store_key_id(app_config), "typ": "JWT"}
    payload = {"iat": now, "exp": now + 20 * 60, "aud": "appstoreconnect-v1"}
    if key_type != "individual":
        payload["iss"] = issuer_id
    signing_input = (
        f"{b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    der_signature = app_store_private_key(app_config).sign(
        signing_input.encode("ascii"),
        ec.ECDSA(hashes.SHA256()),
    )
    r, s = utils.decode_dss_signature(der_signature)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{b64url(signature)}"


def app_store_report_bytes(app_config: dict[str, Any], report_date: date) -> bytes:
    vendor_number = app_config.get("vendor_number")
    if not vendor_number:
        raise StoreDownloadError("App Store Connect vendor_number is not configured.")

    params = {
        "filter[frequency]": "DAILY",
        "filter[reportDate]": report_date.isoformat(),
        "filter[reportSubType]": app_config.get("report_subtype", "SUMMARY"),
        "filter[reportType]": app_config.get("report_type", "SALES"),
        "filter[vendorNumber]": vendor_number,
        "filter[version]": app_config.get("version", "1_0"),
    }
    request = Request(
        f"{APP_STORE_API_BASE}/salesReports?{urlencode(params)}",
        headers={"Authorization": f"Bearer {app_store_jwt(app_config)}", "Accept": "application/a-gzip"},
        method="GET",
    )
    with urlopen(request, timeout=60) as response:
        body = response.read()
    try:
        return gzip.decompress(body)
    except OSError:
        return body


def parse_app_store_daily(report_bytes: bytes, app_config: dict[str, Any], report_date: date) -> pd.DataFrame:
    text = report_bytes.decode(app_config.get("encoding", "utf-8-sig"))
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    units_column = app_config.get("units_column", "Units")
    product_type_filter = set(app_config.get("product_type_identifiers", ["1", "1F", "1T"]))
    sku_filter = set(app_config.get("skus", []))
    app_apple_id_filter = set(str(item) for item in app_config.get("apple_ids", []))

    rows = []
    for row in reader:
        if units_column not in row:
            available = ", ".join(row.keys())
            raise StoreDownloadError(
                f"Units column '{units_column}' not found in App Store report. Available: {available}"
            )
        if product_type_filter and row.get("Product Type Identifier", "") not in product_type_filter:
            continue
        if sku_filter and row.get("SKU") not in sku_filter:
            continue
        if app_apple_id_filter and row.get("Apple Identifier") not in app_apple_id_filter:
            continue
        raw_value = (row.get(units_column) or "0").replace(",", "").strip()
        rows.append({"date": report_date, "store": "Apple", "downloads": int(float(raw_value or 0))})
    return pd.DataFrame(rows, columns=["date", "store", "downloads"])


def fetch_app_store_downloads(config: dict[str, Any], start: date, end: date) -> tuple[pd.DataFrame, list[str]]:
    app_config = config.get("app_store", {})
    if not app_config.get("enabled", False):
        return pd.DataFrame(columns=["date", "store", "downloads"]), ["App Store Connect is disabled in store config."]

    frames = []
    missing_reports = 0
    failed_reports = 0
    for report_date in each_day(start, end):
        try:
            report = app_store_report_bytes(app_config, report_date)
        except HTTPError as error:
            if error.code in {404, 409, 500, 502, 503, 504}:
                if error.code in {500, 502, 503, 504}:
                    failed_reports += 1
                else:
                    missing_reports += 1
                continue
            raise
        except URLError:
            failed_reports += 1
            continue
        try:
            frames.append(parse_app_store_daily(report, app_config, report_date))
        except StoreDownloadError as error:
            if "No matching" in str(error):
                missing_reports += 1
                continue
            raise

    notes = [f"Loaded App Store Connect daily sales reports for {len(frames)} day(s)."]
    if missing_reports:
        notes.append(f"Skipped {missing_reports} App Store day(s) with unavailable reports.")
    if failed_reports:
        notes.append(f"Skipped {failed_reports} App Store day(s) with temporary API errors.")
    if not frames:
        return pd.DataFrame(columns=["date", "store", "downloads"]), notes
    return filter_download_dates(pd.concat(frames, ignore_index=True), start, end), notes


def filter_download_dates(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if df.empty:
        return df
    output = df.copy()
    output["date"] = pd.to_datetime(output["date"]).dt.date
    return output[(output["date"] >= start) & (output["date"] <= end)]


def fetch_store_downloads(
    start: date,
    end: date,
    config_path: str | Path | dict[str, Any] | None = None,
) -> StoreDownloadResult:
    config, loaded_path = load_store_config(config_path)
    notes = [f"Loaded store config from {loaded_path}."]
    frames = []

    try:
        google_df, google_notes = fetch_google_play_downloads(config, start, end)
        frames.append(google_df)
        notes.extend(google_notes)
    except (StoreDownloadError, HTTPError, URLError) as error:
        notes.append(f"Google Play unavailable: {api_error_message(error)}")

    try:
        apple_df, apple_notes = fetch_app_store_downloads(config, start, end)
        frames.append(apple_df)
        notes.extend(apple_notes)
    except (StoreDownloadError, HTTPError, URLError) as error:
        notes.append(f"App Store Connect unavailable: {api_error_message(error)}")

    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return StoreDownloadResult(pd.DataFrame(columns=["date", "store", "downloads"]), notes)

    downloads = pd.concat(frames, ignore_index=True)
    downloads = downloads.groupby(["date", "store"], as_index=False)["downloads"].sum()
    downloads["date"] = pd.to_datetime(downloads["date"])
    return StoreDownloadResult(complete_store_series(downloads, start, end, config), notes)


def complete_store_series(df: pd.DataFrame, start: date, end: date, config: dict[str, Any]) -> pd.DataFrame:
    stores = []
    if config.get("google_play", {}).get("enabled", False):
        stores.append("Android")
    if config.get("app_store", {}).get("enabled", False):
        stores.append("Apple")
    if not stores:
        stores = sorted(df["store"].dropna().unique().tolist())

    dates = pd.date_range(start=start, end=end, freq="D")
    skeleton = pd.MultiIndex.from_product([dates, stores], names=["date", "store"]).to_frame(index=False)
    merged = skeleton.merge(df, on=["date", "store"], how="left")
    merged["downloads"] = merged["downloads"].fillna(0).astype(int)
    return merged


def api_error_message(error: Exception) -> str:
    if isinstance(error, HTTPError):
        try:
            body = error.read().decode("utf-8")
        except Exception:
            body = ""
        return f"{error.code} {body or error.reason}"
    if isinstance(error, URLError):
        return str(error.reason)
    return str(error)
