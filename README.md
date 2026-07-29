# Advanced Manufacturing App Analytics Dashboard

Streamlit dashboard for GA4 app usage metrics.

## Setup

1. Install dependencies:

```powershell
pip install -r requirements.txt
```

2. Put the Google service account JSON in `config/`, or set `GOOGLE_APPLICATION_CREDENTIALS` to its path. Make sure the GA4 property has granted Viewer access to the service account email.

3. Provide the GA4 numeric property ID with either an environment variable:

```powershell
$env:GA_PROPERTY_ID="123456789"
```

or Streamlit secrets:

```toml
# .streamlit/secrets.toml
GA_PROPERTY_ID = "123456789"
```

4. Run:

```powershell
streamlit run app.py
```

## Notes

GA4 does not expose App Store / Play Store download counts directly through the Analytics Data API. This dashboard treats the GA4 `first_open` event as installs/downloads. If the app sends a custom download/install event, update `DOWNLOAD_EVENT_NAME` in `app.py`.
