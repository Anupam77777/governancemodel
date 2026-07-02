"""
monitoring.py
Autonomous monitoring layer for the Azure Governance Bot.

Provides:
  - A SQLite store (history.db) with a watch list, metric snapshots, and digests
  - Metric extraction from a generated report
  - A scheduler (background thread) that periodically runs reports for watched
    subscriptions and records snapshots + an AI change-digest
  - Query helpers for the dashboard UI

Everything is local: a single SQLite file alongside the backend.
"""
import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "history.db")
_LOCK = threading.Lock()


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _LOCK, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS watch_list (
                subscription_id   TEXT PRIMARY KEY,
                subscription_name TEXT,
                added_utc         TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id   TEXT,
                subscription_name TEXT,
                run_utc           TEXT,
                metrics_json      TEXT,
                inventory_json    TEXT
            )
        """)
        # Migration: add inventory_json to older databases that lack it.
        cols = [r[1] for r in c.execute("PRAGMA table_info(snapshots)").fetchall()]
        if "inventory_json" not in cols:
            c.execute("ALTER TABLE snapshots ADD COLUMN inventory_json TEXT")
        c.execute("""
            CREATE TABLE IF NOT EXISTS digests (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id   TEXT,
                subscription_name TEXT,
                run_utc           TEXT,
                digest_text       TEXT,
                severity          TEXT,
                changes_json      TEXT
            )
        """)
        dcols = [r[1] for r in c.execute("PRAGMA table_info(digests)").fetchall()]
        if "changes_json" not in dcols:
            c.execute("ALTER TABLE digests ADD COLUMN changes_json TEXT")
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)


# ----------------------------------------------------------------------------
# Watch list
# ----------------------------------------------------------------------------
def get_watch_list():
    with _conn() as c:
        rows = c.execute(
            "SELECT subscription_id, subscription_name, added_utc "
            "FROM watch_list ORDER BY subscription_name").fetchall()
    return [dict(r) for r in rows]


def set_watch_list(subscriptions):
    """subscriptions: list of {subscription_id, subscription_name}."""
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM watch_list")
        for s in subscriptions:
            c.execute(
                "INSERT OR REPLACE INTO watch_list "
                "(subscription_id, subscription_name, added_utc) VALUES (?,?,?)",
                (s["subscription_id"], s.get("subscription_name", ""), now))
    return get_watch_list()


# ----------------------------------------------------------------------------
# Metric extraction from a report dict
# ----------------------------------------------------------------------------
def extract_metrics(report):
    """Pull the key scalar metrics from a full report dict."""
    def ok(key):
        b = report.get(key, {})
        return b.get("data") if b.get("status") == "ok" else None

    m = {}
    inv = ok("inventory")
    m["resource_total"] = inv["total"] if inv else None

    b = ok("backup")
    if b:
        m["vms_total"] = b["total_vms"]
        m["vms_unprotected"] = b["unprotected_count"]
        m["backup_coverage_pct"] = (
            round(100 * b["protected_count"] / b["total_vms"], 1)
            if b["total_vms"] else 100.0)
    else:
        m["vms_total"] = m["vms_unprotected"] = m["backup_coverage_pct"] = None

    n = ok("nsg")
    m["nsg_internet_open"] = n["internet_open_count"] if n else None
    m["nsg_any_any"] = n["any_any_count"] if n else None

    a = ok("advisor")
    if a:
        m["advisor_security"] = a["security_count"]
        m["advisor_reliability"] = a["reliability_count"]
        m["advisor_performance"] = a["performance_count"]
    else:
        m["advisor_security"] = m["advisor_reliability"] = m["advisor_performance"] = None

    c = ok("cost")
    m["cost_total"] = c["total"] if c else None
    m["cost_currency"] = c["currency"] if c else None

    s = ok("storage")
    if s:
        unprotected_shares = sum(
            1 for acct in s["accounts"] for sh in acct.get("shares", [])
            if not sh["backed_up"])
        m["storage_accounts"] = s["count"]
        m["file_shares_unprotected"] = unprotected_shares
    else:
        m["storage_accounts"] = m["file_shares_unprotected"] = None

    p = ok("policy")
    if p:
        comp = p.get("compliance") or {}
        m["policy_compliance_pct"] = comp.get("compliance_pct")
        m["policy_noncompliant"] = comp.get("non_compliant_resources")
        m["policy_exemptions"] = p.get("exemption_count")
    else:
        m["policy_compliance_pct"] = m["policy_noncompliant"] = m["policy_exemptions"] = None

    return m


def extract_inventory(report):
    """
    Build a dict of {resource_id: {name, type}} from the report inventory so we
    can diff exactly which resources were added/removed between runs.
    Falls back to an empty dict if inventory rows aren't available.
    """
    inv = report.get("inventory", {})
    if inv.get("status") != "ok":
        return {}
    rows = inv["data"].get("rows", [])
    out = {}
    for r in rows:
        rid = r.get("id") or f"{r.get('type','')}/{r.get('name','')}"
        out[rid] = {"name": r.get("name", ""), "type": r.get("type", "")}
    return out


def record_snapshot(subscription_id, subscription_name, metrics, inventory=None):
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO snapshots "
            "(subscription_id, subscription_name, run_utc, metrics_json, inventory_json) "
            "VALUES (?,?,?,?,?)",
            (subscription_id, subscription_name, now, json.dumps(metrics),
             json.dumps(inventory or {})))
    return now


def last_two_snapshots(subscription_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT run_utc, metrics_json, inventory_json FROM snapshots "
            "WHERE subscription_id=? ORDER BY id DESC LIMIT 2",
            (subscription_id,)).fetchall()
    out = []
    for r in rows:
        try:
            inv = json.loads(r["inventory_json"]) if r["inventory_json"] else {}
        except Exception:
            inv = {}
        out.append({"run_utc": r["run_utc"],
                    "metrics": json.loads(r["metrics_json"]),
                    "inventory": inv})
    return out  # [latest, previous] (previous may be missing)


def diff_inventory(latest_inv, previous_inv):
    """Return {'added': [...], 'removed': [...]} lists of {name, type, id}."""
    if not previous_inv:
        return {"added": [], "removed": []}  # first run: nothing to diff
    latest_ids = set(latest_inv.keys())
    prev_ids = set(previous_inv.keys())
    added = [{"id": i, **latest_inv[i]} for i in (latest_ids - prev_ids)]
    removed = [{"id": i, **previous_inv[i]} for i in (prev_ids - latest_ids)]
    # Sort for stable display.
    added.sort(key=lambda x: x.get("type", ""))
    removed.sort(key=lambda x: x.get("type", ""))
    return {"added": added, "removed": removed}


def snapshot_history(subscription_id, limit=30):
    with _conn() as c:
        rows = c.execute(
            "SELECT run_utc, metrics_json FROM snapshots "
            "WHERE subscription_id=? ORDER BY id DESC LIMIT ?",
            (subscription_id, limit)).fetchall()
    return [{"run_utc": r["run_utc"], "metrics": json.loads(r["metrics_json"])}
            for r in rows][::-1]  # chronological


# ----------------------------------------------------------------------------
# Change digest (LLM compares latest vs previous metrics)
# ----------------------------------------------------------------------------
def _diff_summary(latest, previous):
    """Build a plain dict of changed metrics for the LLM and a severity guess."""
    changes = {}
    severity = "info"
    for k, new in latest.items():
        old = previous.get(k) if previous else None
        if isinstance(new, (int, float)) and isinstance(old, (int, float)) and new != old:
            changes[k] = {"from": old, "to": new, "delta": round(new - old, 2)}
    # crude severity: worse security/backup posture => higher
    bad_up = ("vms_unprotected", "nsg_internet_open", "nsg_any_any",
              "advisor_security", "file_shares_unprotected")
    for k in bad_up:
        if k in changes and changes[k]["delta"] > 0:
            severity = "warn"
    if ("nsg_internet_open" in changes and changes["nsg_internet_open"]["delta"] > 0) or \
       ("nsg_any_any" in changes and changes["nsg_any_any"]["delta"] > 0):
        severity = "alert"
    return changes, severity


DIGEST_SYSTEM = (
    "You are an Azure governance monitor. You are given the latest metrics for a "
    "subscription, the metric changes since the previous run, and lists of "
    "resources that were ADDED or REMOVED since the last run. Write a SHORT digest "
    "(2-6 sentences) describing what changed and why it matters, in plain English. "
    "Lead with the most important change. Name specific added/removed resources "
    "when notable (especially security-relevant ones like NSGs, public IPs, or "
    "deleted VMs/storage). If a change increases risk (new internet exposure, "
    "dropped backup coverage, new unprotected resources, deleted production "
    "resources), say so plainly and recommend the single most important action. "
    "If nothing material changed, say posture is stable. Do not invent changes not "
    "in the data. No headers."
)


def generate_digest(subscription_name, latest, previous, inv_diff=None):
    """Return (digest_text, severity). Falls back to a rule-based digest if no LLM."""
    changes, severity = _diff_summary(latest, previous)
    inv_diff = inv_diff or {"added": [], "removed": []}

    # Removals can be high-signal (someone deleted something) — bump severity.
    if inv_diff["removed"] and severity == "info":
        severity = "warn"

    # Try LLM for a natural-language digest.
    try:
        import ai_insights
        client, reason = ai_insights._client()
        if client is not None:
            payload = {
                "subscription": subscription_name,
                "latest_metrics": latest,
                "changes_since_last_run": changes,
                "resources_added": [f"{r['type']}/{r['name']}" for r in inv_diff["added"][:40]],
                "resources_removed": [f"{r['type']}/{r['name']}" for r in inv_diff["removed"][:40]],
                "is_first_run": previous is None,
            }
            msg = client.messages.create(
                model=ai_insights.MODEL,
                max_tokens=500,
                system=DIGEST_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
            )
            parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
            text = "\n".join(parts).strip()
            if text:
                return text, severity
    except Exception:
        pass

    # Rule-based fallback (no LLM available).
    if previous is None:
        return (f"First monitoring run recorded for {subscription_name}. "
                f"Baseline captured.", "info")
    if not changes and not inv_diff["added"] and not inv_diff["removed"]:
        return ("No material changes since the last run. Posture is stable.", "info")
    bits = []
    label = {
        "vms_unprotected": "unprotected VMs",
        "backup_coverage_pct": "backup coverage %",
        "nsg_internet_open": "internet-open NSG rules",
        "nsg_any_any": "any-any NSG rules",
        "advisor_security": "Advisor security findings",
        "cost_total": "total cost",
        "file_shares_unprotected": "unprotected file shares",
        "resource_total": "total resources",
    }
    for k, ch in changes.items():
        name = label.get(k, k)
        arrow = "up" if ch["delta"] > 0 else "down"
        bits.append(f"{name} {arrow} from {ch['from']} to {ch['to']}")
    msg_parts = []
    if bits:
        msg_parts.append("Changes: " + "; ".join(bits) + ".")
    if inv_diff["added"]:
        names = ", ".join(f"{r['name']} ({r['type'].split('/')[-1]})"
                          for r in inv_diff["added"][:10])
        msg_parts.append(f"Added: {names}.")
    if inv_diff["removed"]:
        names = ", ".join(f"{r['name']} ({r['type'].split('/')[-1]})"
                          for r in inv_diff["removed"][:10])
        msg_parts.append(f"Removed: {names}.")
    return (" ".join(msg_parts), severity)


def record_digest(subscription_id, subscription_name, run_utc, text, severity, changes=None):
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO digests "
            "(subscription_id, subscription_name, run_utc, digest_text, severity, changes_json) "
            "VALUES (?,?,?,?,?,?)",
            (subscription_id, subscription_name, run_utc, text, severity,
             json.dumps(changes or {})))


def latest_digest(subscription_id):
    with _conn() as c:
        r = c.execute(
            "SELECT run_utc, digest_text, severity, changes_json FROM digests "
            "WHERE subscription_id=? ORDER BY id DESC LIMIT 1",
            (subscription_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["changes"] = json.loads(d.pop("changes_json")) if d.get("changes_json") else {}
    except Exception:
        d["changes"] = {}
    return d


# ----------------------------------------------------------------------------
# Run one subscription: build report -> metrics -> snapshot -> digest
# ----------------------------------------------------------------------------
def run_one(subscription_id, subscription_name):
    import report as report_mod
    report = report_mod.build_report(subscription_id, None, subscription_name)
    metrics = extract_metrics(report)
    inventory = extract_inventory(report)

    snaps = last_two_snapshots(subscription_id)  # before inserting new one
    previous = snaps[0]["metrics"] if snaps else None
    previous_inv = snaps[0]["inventory"] if snaps else None

    inv_diff = diff_inventory(inventory, previous_inv)

    run_utc = record_snapshot(subscription_id, subscription_name, metrics, inventory)
    text, severity = generate_digest(subscription_name, metrics, previous, inv_diff)
    record_digest(subscription_id, subscription_name, run_utc, text, severity, inv_diff)
    return {"subscription_id": subscription_id, "run_utc": run_utc,
            "severity": severity, "metrics": metrics,
            "added": len(inv_diff["added"]), "removed": len(inv_diff["removed"])}


def run_all_watched():
    """Run every subscription on the watch list, sequentially (throttle-friendly)."""
    results = []
    for s in get_watch_list():
        try:
            results.append(run_one(s["subscription_id"], s["subscription_name"]))
        except Exception as e:
            results.append({"subscription_id": s["subscription_id"],
                            "error": str(e)[:300]})
        time.sleep(2)  # small gap to ease Azure API pressure
    set_setting("last_run_utc", datetime.now(timezone.utc).isoformat())
    return results


# ----------------------------------------------------------------------------
# Settings (schedule interval, last run)
# ----------------------------------------------------------------------------
def get_setting(key, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with _LOCK, _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                  (key, str(value)))


# ----------------------------------------------------------------------------
# Dashboard data
# ----------------------------------------------------------------------------
def dashboard_data():
    out = []
    for s in get_watch_list():
        sid = s["subscription_id"]
        snaps = last_two_snapshots(sid)
        latest = snaps[0]["metrics"] if snaps else None
        previous = snaps[1]["metrics"] if len(snaps) > 1 else None
        digest = latest_digest(sid)
        history = snapshot_history(sid, limit=20)
        out.append({
            "subscription_id": sid,
            "subscription_name": s["subscription_name"],
            "latest": latest,
            "previous": previous,
            "last_run_utc": snaps[0]["run_utc"] if snaps else None,
            "digest": digest,
            "history": history,
        })
    return {"subscriptions": out, "last_run_utc": get_setting("last_run_utc")}


# ----------------------------------------------------------------------------
# Background scheduler
# ----------------------------------------------------------------------------
_scheduler_thread = None
_scheduler_stop = threading.Event()


def _scheduler_loop():
    while not _scheduler_stop.is_set():
        interval_hours = float(get_setting("interval_hours", "24") or "24")
        last = get_setting("last_run_utc")
        due = True
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                due = elapsed >= interval_hours * 3600
            except Exception:
                due = True
        if due and get_watch_list():
            try:
                run_all_watched()
            except Exception as e:
                print(f"[scheduler] run failed: {e}")
        # Check again every 5 minutes whether a run is due.
        _scheduler_stop.wait(300)


def start_scheduler():
    global _scheduler_thread
    init_db()
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()
