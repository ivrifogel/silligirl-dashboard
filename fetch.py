#!/usr/bin/env python3
"""
silligirl IG dashboard fetcher.
Pulls follower / post / engagement stats for the 3 creator accounts and writes
data.js (for index.html) + history.json (growth over time).

Data sources, in order:
  1. ScrapeCreators IG profile API   (reliable; needs credits)
  2. Instagram public web_profile_info (free; IG rate-limits datacenter IPs)

Run: python3 fetch.py     (the launchd job runs this every 12h)
"""
import os, json, time, datetime, urllib.request, urllib.error, pathlib

HANDLES = ["brookes.siligirl", "tahliasclips", "islasclips"]
HERE = pathlib.Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"


def load_key():
    # (path, var) in priority order — the oleah landing key has credits.
    sources = [
        ("/Users/ivrifogel/Desktop/INF/_ALL DEPLOYMENTS/oleah/landing/.env.local", "SCRAPE_CREATORS_KEY"),
        ("/Users/ivrifogel/.claude/.env.local", "SCRAPECREATORS_API_KEY"),
    ]
    for path, var in sources:
        try:
            for line in open(path):
                if line.strip().startswith(var + "="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        except FileNotFoundError:
            pass
    return os.environ.get("SCRAPE_CREATORS_KEY") or os.environ.get("SCRAPECREATORS_API_KEY")


def _get(url, headers, timeout=35):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "ignore")


def deep_first(obj, keys):
    """Find the first value under any of `keys` anywhere in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (int, float, str)):
                return v
            r = deep_first(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_first(v, keys)
            if r is not None:
                return r
    return None


def parse_posts(node_list):
    out = []
    for n in node_list or []:
        n = n.get("node", n) if isinstance(n, dict) else n
        if not isinstance(n, dict):
            continue
        out.append({
            "shortcode": n.get("shortcode") or n.get("code") or n.get("id"),
            "views": n.get("video_view_count") or n.get("play_count") or n.get("view_count") or n.get("views"),
            "likes": (n.get("edge_liked_by", {}) or {}).get("count") if isinstance(n.get("edge_liked_by"), dict) else (n.get("like_count") or n.get("likes")),
            "comments": (n.get("edge_media_to_comment", {}) or {}).get("count") if isinstance(n.get("edge_media_to_comment"), dict) else (n.get("comment_count") or n.get("comments")),
            "is_video": n.get("is_video", n.get("media_type") == 2),
        })
    return out[:12]


def via_scrapecreators(handle, key):
    if not key:
        raise RuntimeError("no API key")
    url = f"https://api.scrapecreators.com/v1/instagram/profile?handle={handle}"
    st, body = _get(url, {"x-api-key": key})
    data = json.loads(body)
    if not data or data.get("success") is False:
        raise RuntimeError(data.get("message", "scrapecreators error"))
    u = data.get("data", {}).get("user", data.get("user", data))
    followers = deep_first(u, {"follower_count", "followers", "followers_count"}) or \
        (u.get("edge_followed_by", {}) or {}).get("count")
    following = deep_first(u, {"following_count", "follows_count"}) or \
        (u.get("edge_follow", {}) or {}).get("count")
    posts = deep_first(u, {"media_count", "post_count", "posts_count"}) or \
        (u.get("edge_owner_to_timeline_media", {}) or {}).get("count")
    edges = (u.get("edge_owner_to_timeline_media", {}) or {}).get("edges") or u.get("posts") or u.get("media") or []
    return {"followers": followers, "following": following, "posts": posts,
            "recent": parse_posts(edges), "bio": (u.get("biography") or "")[:120], "source": "scrapecreators"}


def via_public(handle):
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={handle}"
    st, body = _get(url, {"x-ig-app-id": "936619743392459", "User-Agent": UA, "Accept": "*/*"})
    u = json.loads(body)["data"]["user"]
    return {
        "followers": (u.get("edge_followed_by", {}) or {}).get("count"),
        "following": (u.get("edge_follow", {}) or {}).get("count"),
        "posts": (u.get("edge_owner_to_timeline_media", {}) or {}).get("count"),
        "recent": parse_posts((u.get("edge_owner_to_timeline_media", {}) or {}).get("edges")),
        "bio": (u.get("biography") or "")[:120], "source": "public",
    }


def fetch_one(handle, key):
    errs = []
    for fn in (lambda: via_scrapecreators(handle, key), lambda: via_public(handle)):
        try:
            return fn(), None
        except Exception as e:
            errs.append(str(e)[:140])
            time.sleep(1)
    return None, " | ".join(errs)


def via_shopify():
    """Pull orders + revenue from Shopify Admin API. No-op unless creds are set.
    Needs env: SHOPIFY_STORE (e.g. siligirl.myshopify.com) + SHOPIFY_ADMIN_TOKEN (shpat_...)."""
    store = os.environ.get("SHOPIFY_STORE", "").strip().replace("https://", "").rstrip("/")
    token = os.environ.get("SHOPIFY_ADMIN_TOKEN", "").strip()
    if not store or not token:
        return None
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=8)).isoformat()
    url = (f"https://{store}/admin/api/2024-07/orders.json?status=any&limit=250"
           f"&created_at_min={since}&fields=created_at,total_price,financial_status")
    st, body = _get(url, {"X-Shopify-Access-Token": token, "Accept": "application/json"})
    orders = json.loads(body).get("orders", [])
    today = datetime.datetime.now(datetime.timezone.utc).date()
    wk = today - datetime.timedelta(days=7)
    o_today = o_wk = 0
    r_today = r_wk = 0.0
    for o in orders:
        try:
            d = datetime.datetime.fromisoformat(o["created_at"].replace("Z", "+00:00")).date()
            amt = float(o.get("total_price") or 0)
        except (ValueError, KeyError):
            continue
        if d == today:
            o_today += 1; r_today += amt
        if d >= wk:
            o_wk += 1; r_wk += amt
    return {"orders_today": o_today, "revenue_today": round(r_today, 2),
            "orders_7d": o_wk, "revenue_7d": round(r_wk, 2),
            "aov_7d": round(r_wk / o_wk, 2) if o_wk else None}


def main():
    key = load_key()
    now = datetime.datetime.now().astimezone().isoformat(timespec="minutes")
    accounts, hist_point = {}, {"ts": now}
    for h in HANDLES:
        res, err = fetch_one(h, key)
        if res:
            res["error"] = None
            accounts[h] = res
            hist_point[h] = {"followers": res["followers"], "posts": res["posts"]}
            print(f"OK   {h}: {res['followers']} followers, {res['posts']} posts ({res['source']})")
        else:
            accounts[h] = {"error": err, "followers": None, "following": None, "posts": None, "recent": []}
            print(f"FAIL {h}: {err}")

    hp = HERE / "history.json"
    history = json.loads(hp.read_text()) if hp.exists() else []
    if any(v for k, v in hist_point.items() if k != "ts"):
        history.append(hist_point)
        history = history[-200:]
        hp.write_text(json.dumps(history, indent=1))

    shop = None
    try:
        shop = via_shopify()
        if shop:
            print(f"OK   shopify: {shop['orders_today']} orders today, "
                  f"{shop['orders_7d']} / ${shop['revenue_7d']} last 7d")
    except Exception as e:
        print("FAIL shopify:", str(e)[:120])

    payload = {"updated": now, "accounts": accounts, "history": history, "shop": shop}
    (HERE / "data.json").write_text(json.dumps(payload, indent=1))
    (HERE / "data.js").write_text("window.DATA = " + json.dumps(payload) + ";")
    print("wrote data.js / data.json /", len(history), "history points")


if __name__ == "__main__":
    main()
