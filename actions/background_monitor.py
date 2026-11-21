"""
BackgroundMonitor — user-configured topic watching.
Checks DDG news once per day per topic; alerts JARVIS when a new headline appears.
No crypto, no finance, no uninvited tracking.
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


# ── Blocked categories (never monitor regardless of what user says) ────────────

_BLOCKED = {
    # Brand / asset names — spelled the same in every language
    "bitcoin", "ethereum", "dogecoin", "solana", "binance",
    "nft", "blockchain", "defi", "altcoin", "memecoin", "coin", "token",
    # spellings of the "crypto" root across different languages
    "crypto", "kripto", "cripto", "krypto", "крипто", "仮想通貨", "暗号資産",
    "cryptocurrency",
}

def _is_blocked(topic: str) -> bool:
    t = topic.lower()
    return any(word in t for word in _BLOCKED)


# ── Slug / hash helpers ────────────────────────────────────────────────────────

def _slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", topic.lower().strip())[:40].strip("_")

def _title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8", errors="ignore")).hexdigest()[:12]


# ── Memory I/O ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    from memory.memory_manager import load_memory
    data = load_memory().get("monitors", {})
    return data if isinstance(data, dict) else {}

def _save(monitors: dict) -> None:
    from memory.memory_manager import load_memory, MEMORY_PATH, _lock
    memory = load_memory()
    memory["monitors"] = monitors
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def add_monitor(topic: str) -> str:
    topic = topic.strip()
    if not topic:
        return "Please specify a topic to monitor."
    if _is_blocked(topic):
        return "I don't monitor crypto or financial topics."
    monitors = _load()
    slug = _slug(topic)
    if slug in monitors:
        return f"Already monitoring: {monitors[slug]['topic']}"
    monitors[slug] = {
        "topic":      topic,
        "added":      datetime.now().strftime("%Y-%m-%d"),
        "last_check": "",
        "last_hash":  "",
    }
    _save(monitors)
    print(f"[Monitor] ➕ Added: {topic}")
    return f"Now monitoring: {topic}"


def remove_monitor(topic: str) -> str:
    topic = topic.strip().lower()
    monitors = _load()
    # exact slug match first
    slug = _slug(topic)
    if slug in monitors:
        label = monitors.pop(slug)["topic"]
        _save(monitors)
        return f"Stopped monitoring: {label}"
    # partial match fallback
    for key, val in list(monitors.items()):
        if topic in val.get("topic", "").lower():
            label = monitors.pop(key)["topic"]
            _save(monitors)
            return f"Stopped monitoring: {label}"
    return f"Not found in monitored topics: {topic}"


def list_monitors() -> list[str]:
    return [v.get("topic", k) for k, v in _load().items()]


def check_all() -> list[str]:
    """
    Run all pending topic checks (once per day per topic).
    Returns a list of [MONITOR_ALERT] strings — empty if nothing new.
    """
    from actions.web_search import _ddg_news

    monitors = _load()
    if not monitors:
        return []

    today   = datetime.now().strftime("%Y-%m-%d")
    alerts  = []
    changed = False

    for slug, data in monitors.items():
        if data.get("last_check") == today:
            continue                     # already checked today

        topic = data.get("topic", slug)
        try:
            from actions.web_search import (
                _detect_news_category,
                NEWS_CATEGORIES,
                _match_tech_outlet,
                _is_non_tech_blocked,
                _extract_source_name,
            )

            cat = _detect_news_category(topic)
            site_q = NEWS_CATEGORIES.get(cat, {}).get("site_query", "")
            tech_q = f"({topic}) AND ({site_q})" if site_q else topic
            results = _ddg_news(tech_q, max_results=6)
            approved = [r for r in results if not _is_non_tech_blocked(r) and _match_tech_outlet(r)]
            if approved:
                results = approved
            elif not results:
                results = _ddg_news(topic, max_results=5)

            if not results:
                monitors[slug]["last_check"] = today
                changed = True
                continue

            top   = results[0]
            title = top.get("title", "").strip()
            if not title:
                continue

            h = _title_hash(title)
            monitors[slug]["last_check"] = today
            changed = True

            if h == data.get("last_hash"):
                continue                 # same headline as last check — no alert

            monitors[slug]["last_hash"] = h

            snippet = top.get("snippet", "")[:150]
            source  = top.get("source", "")
            outlet_name = _extract_source_name(top) or source
            parts   = [f"[MONITOR_ALERT] {topic}", f"Engineering Headline: {title}"]
            if snippet:
                parts.append(f"Technical Brief: {snippet}")
            if outlet_name:
                parts.append(f"Source: {outlet_name}")
            alerts.append("\n".join(parts))
            print(f"[Monitor] 🔔 New headline for '{topic}': {title[:60]}")

        except Exception as e:
            print(f"[Monitor] ⚠️ Check failed for '{topic}': {e}")

    if changed:
        _save(monitors)

    return alerts
