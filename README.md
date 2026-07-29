# Advanced Manufacturing App Analytics Dashboard

Streamlit dashboard for app store downloads and GA4 usage metrics.

## Setup

1. Install dependencies:

```powershell
pip install -r requirements.txt
```

2. Put the GA4 Google service account JSON in `config/`, or set `GOOGLE_APPLICATION_CREDENTIALS` to its path. Make sure the GA4 property has granted Viewer access to the service account email.

3. Provide the GA4 numeric property ID with either an environment variable:

```powershell
$env:GA_PROPERTY_ID="123456789"
```

or Streamlit secrets:

```toml
# .streamlit/secrets.toml
GA_PROPERTY_ID = "123456789"
```

Use `.streamlit/secrets.toml.template` as the full local secrets template.

4. Run:

```powershell
streamlit run app.py
```

## Store Downloads

Downloads come from Apple App Store Connect Sales and Trends and Google Play Console bulk install reports.

By default, the dashboard reuses the existing Scorecards config at:

```text
..\Scorecards\config\google_play_sources.json
```

You can override that with:

```powershell
$env:APP_STORES_CONFIG="C:\Path\To\app_store_sources.json"
```

For a local config shape, see:

```text
config/app_store_sources.example.json
```

Real files under `config/*.json` and `config/*.p8` are ignored so API keys and service account files do not get committed.
