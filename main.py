"""
NFT Alert Bot
Watches for new NFT listings/activity (OpenSea), community NFT chatter (Reddit),
and mint/drop conversations (Farcaster -- strong signal for Zora/Base NFT culture).

IMPORTANT: This bot surfaces NFT activity for informational purposes only. It does
no authenticity or scam verification. This is not financial advice. Always DYOR.

Runs on a schedule via GitHub Actions (see .github/workflows/alert.yml)
"""

import os
import re
import json
import time
import hashlib
import requests
import feedparser
from datetime import datetime, timezone, timedelta

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
NEYNAR_API_KEY = os.environ.get("NEYNAR_API_KEY", "")

RECENCY_HOURS = 48
SEEN_FILE = "seen_nft.json"
MAX_ALERTS_PER_RUN = 20

FARCASTER_NFT_QUERIES = ["nft mint", "zora mint", "nft drop", "new collection"]

RSS_FEEDS = {
    "Reddit-NFT": "https://www.reddit.com/r/NFT/new/.rss",
    "Reddit-opensea": "https://www.reddit.com/r/opensea/new/.rss",
    "Reddit-NFTsMarketplace": "https://www.reddit.com/r/NFTsMarketplace/new/.rss",
}


# ---------- HELPERS ----------
def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    trimmed = list(seen)[-5000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(trimmed, f)


def make_id(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, data=payload, timeout=15)
    if not r.ok:
        print("Telegram send failed:", r.text)


# ---------- OPENSEA (instant free key, no signup) ----------
OPENSEA_API_KEY = os.environ.get("OPENSEA_API_KEY", "")


def get_opensea_key():
    if OPENSEA_API_KEY:
        return OPENSEA_API_KEY
    try:
        r = requests.post("https://api.opensea.io/api/v2/auth/keys", headers=HEADERS, timeout=15)
        if r.ok:
            return r.json().get("api_key", "")
        print(f"OpenSea instant key request failed: HTTP {r.status_code} - {r.text[:150]}")
    except Exception as e:
        print(f"OpenSea instant key request failed: {e}")
    return ""


def fetch_opensea_new_collections(cutoff):
    items = []
    key = get_opensea_key()
    if not key:
        return items
    try:
        r = requests.get(
            "https://api.opensea.io/api/v2/collections",
            headers={"x-api-key": key, **HEADERS},
            params={"order_by": "created_date", "limit": 30},
            timeout=15,
        )
        if not r.ok:
            print(f"OpenSea collections fetch failed: HTTP {r.status_code} - {r.text[:150]}")
            return items
        data = r.json()
        entries = data.get("collections", [])
        print(f"OpenSea raw collections returned: {len(entries)}")
        now = datetime.now(timezone.utc)
        for c in entries[:30]:
            slug = c.get("collection", c.get("slug", ""))
            name = c.get("name", slug)
            created = c.get("created_date", "")
            try:
                pub_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else now
            except Exception:
                pub_dt = now
            # no strict cutoff filter here -- rely on seen-id dedup, since not every
            # response reliably includes created_date and we don't want to silently drop items
            description = (c.get("description") or "")[:150]
            items.append({
                "source": "OpenSea",
                "id": make_id("os-collection", slug),
                "title": f"New collection: {name}",
                "link": f"https://opensea.io/collection/{slug}" if slug else "https://opensea.io",
                "detail": description,
                "published": pub_dt,
            })
    except Exception as e:
        print(f"OpenSea collections fetch failed: {e}")
    return items


# ---------- REDDIT (via RSS, no key) ----------
def fetch_rss(name, url, cutoff):
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "")
            summary = re.sub("<[^<]+?>", "", entry.get("summary", ""))[:250]
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if not published:
                continue
            pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
            items.append({
                "source": name,
                "id": make_id("rss", link),
                "title": title,
                "link": link,
                "detail": summary,
                "published": pub_dt,
            })
    except Exception as e:
        print(f"RSS fetch failed for {name}: {e}")
    return items


# ---------- FARCASTER (via Neynar, catches Zora/Base NFT culture) ----------
def fetch_farcaster_nft(cutoff):
    items = []
    if not NEYNAR_API_KEY:
        print("Farcaster: NEYNAR_API_KEY not set, skipping this source")
        return items
    seen_hashes = set()
    for query in FARCASTER_NFT_QUERIES:
        try:
            r = requests.get(
                "https://api.neynar.com/v2/farcaster/cast/search",
                headers={"x-api-key": NEYNAR_API_KEY},
                params={"q": query, "limit": 15},
                timeout=15,
            )
            if not r.ok:
                continue
            data = r.json()
            casts = data.get("result", {}).get("casts", [])
            for cast in casts:
                cast_hash = cast.get("hash", "")
                if not cast_hash or cast_hash in seen_hashes:
                    continue
                seen_hashes.add(cast_hash)
                ts = cast.get("timestamp", "")
                try:
                    pub_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
                if pub_dt < cutoff:
                    continue
                text = cast.get("text", "").strip()
                if not text:
                    continue
                author = cast.get("author", {}).get("username", "unknown")
                items.append({
                    "source": "Farcaster",
                    "id": make_id("fc", cast_hash),
                    "title": f"NFT chatter: {text[:150]}",
                    "link": f"https://warpcast.com/{author}/{cast_hash[:10]}",
                    "detail": f"by @{author}",
                    "published": pub_dt,
                })
        except Exception as e:
            print(f"Farcaster NFT fetch failed for query '{query}': {e}")
    return items


def fetch_magic_eden_launches(cutoff):
    """Magic Eden's Solana public read API is free, no key required (120 req/min limit)."""
    items = []
    try:
        r = requests.get(
            "https://api-mainnet.magiceden.dev/v2/launchpad/collections",
            headers=HEADERS, params={"offset": 0, "limit": 20}, timeout=15,
        )
        if not r.ok:
            print(f"Magic Eden fetch failed: HTTP {r.status_code}")
            return items
        data = r.json()
        entries = data if isinstance(data, list) else data.get("collections", [])
        print(f"Magic Eden raw entries returned: {len(entries)}")
        now = datetime.now(timezone.utc)
        for c in entries[:20]:
            symbol = c.get("symbol", "")
            name = c.get("name", symbol)
            launch_ts = c.get("launchDatetime", "")
            try:
                pub_dt = datetime.fromisoformat(str(launch_ts).replace("Z", "+00:00")) if launch_ts else now
            except Exception:
                pub_dt = now
            # no recency filter here -- launchpad list isn't strictly time-ordered,
            # so we rely on the seen-id dedup to only alert on genuinely new-to-us entries
            price = c.get("price", "")
            items.append({
                "source": "Magic Eden (Solana)",
                "id": make_id("me", symbol),
                "title": f"New Solana mint: {name}",
                "link": f"https://magiceden.io/launchpad/{symbol}",
                "detail": f"Mint price: {price} SOL" if price else "",
                "published": pub_dt,
            })
    except Exception as e:
        print(f"Magic Eden fetch failed: {e}")
    return items


# ---------- FORMATTING ----------
def format_alert(item):
    lines = [
        f"🎨 <b>{item['title']}</b>",
        f"Source: {item['source']}",
    ]
    if item.get("detail"):
        lines.append(item["detail"])
    lines.append(f"\n{item['link']}")
    return "\n".join(lines)


# ---------- MAIN ----------
def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RECENCY_HOURS)

    seen = load_seen()
    all_items = []

    os_items = fetch_opensea_new_collections(cutoff)
    print(f"OpenSea: fetched {len(os_items)} items")
    all_items.extend(os_items)

    for name, url in RSS_FEEDS.items():
        fetched = fetch_rss(name, url, cutoff)
        print(f"{name}: fetched {len(fetched)} items")
        all_items.extend(fetched)

    fc_items = fetch_farcaster_nft(cutoff)
    print(f"Farcaster: fetched {len(fc_items)} items")
    all_items.extend(fc_items)

    me_items = fetch_magic_eden_launches(cutoff)
    print(f"Magic Eden: fetched {len(me_items)} items")
    all_items.extend(me_items)

    print(f"Total items fetched: {len(all_items)}")

    new_items = [it for it in all_items if it["id"] not in seen]
    print(f"New (unseen) items: {len(new_items)}")

    if not new_items:
        print("Nothing new this run.")
        return

    new_items.sort(key=lambda x: x["published"], reverse=True)
    to_send = new_items[:MAX_ALERTS_PER_RUN]

    for it in to_send:
        send_telegram(format_alert(it))
        seen.add(it["id"])
        time.sleep(1)

    for it in new_items:
        seen.add(it["id"])

    save_seen(seen)
    print(f"Sent {len(to_send)} NFT alerts.")


if __name__ == "__main__":
    main()
