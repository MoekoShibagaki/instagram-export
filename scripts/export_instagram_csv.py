import csv
import os
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://graph.instagram.com/v25.0"
TOKEN = os.environ["IG_ACCESS_TOKEN"]
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "30"))
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

session = requests.Session()


def api_get(path, params=None, absolute_url=None):
    url = absolute_url or f"{BASE_URL}{path}"
    params = params or {}

    if not absolute_url:
        params["access_token"] = TOKEN

    resp = session.get(url, params=params, timeout=60)

    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text}

    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {data}")

    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])

    return data


def write_csv(path, rows):
    rows = list(rows)
    if not rows:
        print(f"skip empty csv: {path}")
        return

    fieldnames = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote: {path}")


def get_me():
    data = api_get("/me", {
        "fields": "user_id,username"
    })

    if "data" in data and isinstance(data["data"], list):
        if not data["data"]:
            raise RuntimeError("/me returned empty data")
        return data["data"][0]

    return data

def get_account_profile(ig_user_id):
    fields = "id,username,name,account_type,followers_count,follows_count,media_count"
    data = api_get(f"/{ig_user_id}", {"fields": fields})
    return data


def get_all_media_ids(ig_user_id):
    results = []
    data = api_get(f"/{ig_user_id}/media")

    while True:
        results.extend(item["id"] for item in data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        data = api_get(None, params={}, absolute_url=next_url)

    return results


def get_media_detail(media_id):
    fields = "id,caption,timestamp,permalink,media_type,comments_count,like_count"
    data = api_get(f"/{media_id}", {"fields": fields})
    data["media_id"] = data.pop("id")
    return data


def get_media_insights(media_id):
    # 投稿種別によって一部返らないことがあるので、まずは共通寄りで試す
    metrics = "reach,likes,comments,saved,shares,total_interactions"
    data = api_get(f"/{media_id}/insights", {"metric": metrics})

    row = {"media_id": media_id}
    for item in data.get("data", []):
        name = item.get("name")
        values = item.get("values", [])
        if values:
            row[name] = values[0].get("value")
    return row


def get_account_daily_insights(ig_user_id, since_date, until_date):
    metrics = "reach,total_interactions,accounts_engaged"
    data = api_get(
        f"/{ig_user_id}/insights",
        {
            "metric": metrics,
            "metric_type": "time_series",
            "period": "day",
            "since": since_date,
            "until": until_date,
        }
    )

    by_date = {}
    for metric_obj in data.get("data", []):
        metric_name = metric_obj.get("name")
        for v in metric_obj.get("values", []):
            end_time = v.get("end_time", "")
            date_key = end_time[:10] if end_time else "unknown"
            by_date.setdefault(date_key, {"date": date_key, "ig_user_id": ig_user_id})
            by_date[date_key][metric_name] = v.get("value")

    return list(by_date.values())


def main():
    extracted_at = datetime.now(timezone.utc).isoformat()
    utc_today = datetime.now(timezone.utc).date()
    since_date = (utc_today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    until_date = utc_today.isoformat()

    me = get_me()
    ig_user_id = me.get("user_id", me.get("id"))
    username = me.get("username")

    profile = get_account_profile(ig_user_id)

    print(f"IG user: {ig_user_id} / {username}")

    account_master = [{
    "ig_user_id": ig_user_id,
    "username": username,
    "name": profile.get("name"),
    "account_type": profile.get("account_type"),
    "followers_count": profile.get("followers_count"),
    "follows_count": profile.get("follows_count"),
    "media_count": profile.get("media_count"),
    "extracted_at_utc": extracted_at,
    }]
    write_csv(OUTPUT_DIR / "account_master.csv", account_master)

    # account daily
    try:
        account_daily = get_account_daily_insights(ig_user_id, since_date, until_date)
        for row in account_daily:
            row["username"] = username
            row["extracted_at_utc"] = extracted_at
        write_csv(OUTPUT_DIR / "account_daily.csv", account_daily)
    except Exception as e:
        print(f"account daily insights skipped: {e}")

    media_ids = get_all_media_ids(ig_user_id)
    print(f"media count fetched: {len(media_ids)}")

    media_master_rows = []
    media_insights_rows = []

    for media_id in media_ids:
        try:
            detail = get_media_detail(media_id)
            detail["ig_user_id"] = ig_user_id
            detail["username"] = username
            detail["extracted_at_utc"] = extracted_at
            media_master_rows.append(detail)
        except Exception as e:
            media_master_rows.append({
                "media_id": media_id,
                "ig_user_id": ig_user_id,
                "username": username,
                "detail_error": str(e),
                "extracted_at_utc": extracted_at,
            })

        try:
            insight = get_media_insights(media_id)
            insight["ig_user_id"] = ig_user_id
            insight["username"] = username
            insight["extracted_at_utc"] = extracted_at
            media_insights_rows.append(insight)
        except Exception as e:
            media_insights_rows.append({
                "media_id": media_id,
                "ig_user_id": ig_user_id,
                "username": username,
                "insight_error": str(e),
                "extracted_at_utc": extracted_at,
            })

    write_csv(OUTPUT_DIR / "media_master.csv", media_master_rows)
    write_csv(OUTPUT_DIR / "media_insights.csv", media_insights_rows)

    print("done.")


if __name__ == "__main__":
    main()
