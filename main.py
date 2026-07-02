"""
main.py
FastAPI backend for the Azure Governance Bot.
Step 1: auth + inventory endpoints + serves the UI.
(Terraform export, DevOps push, and PDF report endpoints added in later steps.)
"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import tempfile
import uuid

import inventory
import report as report_mod
import pdf_report
import terraform_export
import ai_insights
import monitoring
import zipfile

app = FastAPI(title="Azure Governance Bot")

# Start the background monitoring scheduler on app startup.
monitoring.start_scheduler()

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "generated_reports")
IAC_DIR = os.path.join(os.path.dirname(__file__), "generated_iac")
SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "report_sessions")
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(IAC_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# Report sessions persist to disk (as JSON) so they survive uvicorn --reload,
# which would otherwise wipe an in-memory store and break the chatbot.
import json


def _store_report(data):
    sid = uuid.uuid4().hex
    path = os.path.join(SESSIONS_DIR, f"{sid}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
    except Exception as e:
        print(f"Warning: could not persist session {sid}: {e}")
    # Bound the number of stored sessions (keep newest ~25).
    try:
        files = sorted(
            (os.path.join(SESSIONS_DIR, f) for f in os.listdir(SESSIONS_DIR)
             if f.endswith(".json")),
            key=os.path.getmtime)
        for old in files[:-25]:
            os.remove(old)
    except Exception:
        pass
    return sid


def _load_report(sid):
    path = os.path.join(SESSIONS_DIR, f"{os.path.basename(sid)}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class ReportRequest(BaseModel):
    subscription_id: str
    subscription_name: str | None = None
    resource_group: str | None = None


class ChatRequest(BaseModel):
    session_id: str
    question: str
    history: list[dict] | None = None


@app.get("/api/subscriptions")
def get_subscriptions():
    try:
        return inventory.list_subscriptions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list subscriptions: {e}")


@app.get("/api/resource-groups/{subscription_id}")
def get_resource_groups(subscription_id: str):
    try:
        return inventory.list_resource_groups(subscription_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list resource groups: {e}")


@app.post("/api/report")
def generate_report(req: ReportRequest):
    """Collect governance data and render a PDF. Returns a download URL."""
    try:
        data = report_mod.build_report(
            req.subscription_id, req.resource_group, req.subscription_name
        )
        fname = f"report_{uuid.uuid4().hex[:8]}.pdf"
        out_path = os.path.join(REPORTS_DIR, fname)
        pdf_report.render_pdf(data, out_path)
        # Surface which sections had collection errors, with their messages.
        warnings = []
        for k in ("inventory", "backup", "patching", "nsg", "advisor",
                  "cost", "storage", "policy", "health"):
            sec = data.get(k)
            if sec is None:
                continue
            if sec.get("status") != "ok":
                warnings.append({"section": k,
                                 "error": str(sec.get("error", ""))[:400]})
        # Store for the chatbot and surface AI availability.
        session_id = _store_report(data)
        try:
            ai_ok, ai_reason = ai_insights.available()
        except Exception:
            ai_ok, ai_reason = False, "AI module unavailable"
        return {"download_url": f"/api/report/{fname}", "warnings": warnings,
                "session_id": session_id, "chat_available": ai_ok,
                "chat_reason": ai_reason}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("=" * 70)
        print("REPORT GENERATION FAILED:")
        print(tb)
        print("=" * 70)
        # Return the last line of the traceback to the UI for quick diagnosis.
        raise HTTPException(status_code=500,
                            detail=f"Report generation failed: {e} | {tb.strip().splitlines()[-1] if tb else ''}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {e}")


# ---------------------------------------------------------------------------
# Monitoring (autonomous scheduled runs + dashboard)
# ---------------------------------------------------------------------------
class WatchListRequest(BaseModel):
    subscriptions: list[dict]  # [{subscription_id, subscription_name}]


@app.get("/api/monitor/watchlist")
def get_watchlist():
    return {"watch_list": monitoring.get_watch_list()}


@app.post("/api/monitor/watchlist")
def update_watchlist(req: WatchListRequest):
    wl = monitoring.set_watch_list(req.subscriptions)
    return {"watch_list": wl}


@app.get("/api/monitor/dashboard")
def get_dashboard():
    return monitoring.dashboard_data()


@app.post("/api/monitor/run-now")
def run_now():
    """Trigger a monitoring run of all watched subscriptions immediately."""
    try:
        results = monitoring.run_all_watched()
        return {"ran": len(results), "results": results}
    except Exception as e:
        import traceback
        print("=" * 70); print("MONITOR RUN FAILED:"); print(traceback.format_exc()); print("=" * 70)
        raise HTTPException(status_code=500, detail=f"Monitoring run failed: {e}")


class RunOneRequest(BaseModel):
    subscription_id: str
    subscription_name: str | None = None


@app.post("/api/monitor/run-one")
def run_one_endpoint(req: RunOneRequest):
    """Run monitoring for a single watched subscription on demand."""
    try:
        # Resolve the name from the watch list if not provided.
        name = req.subscription_name
        if not name:
            for w in monitoring.get_watch_list():
                if w["subscription_id"] == req.subscription_id:
                    name = w["subscription_name"]
                    break
        result = monitoring.run_one(req.subscription_id, name or req.subscription_id)
        return {"result": result}
    except Exception as e:
        import traceback
        print("=" * 70); print("MONITOR RUN-ONE FAILED:"); print(traceback.format_exc()); print("=" * 70)
        raise HTTPException(status_code=500, detail=f"Run failed: {e}")


@app.get("/api/monitor/settings")
def get_monitor_settings():
    return {"interval_hours": monitoring.get_setting("interval_hours", "24"),
            "last_run_utc": monitoring.get_setting("last_run_utc")}


class IntervalRequest(BaseModel):
    interval_hours: float


@app.post("/api/monitor/settings")
def set_monitor_settings(req: IntervalRequest):
    monitoring.set_setting("interval_hours", req.interval_hours)
    return {"interval_hours": monitoring.get_setting("interval_hours")}


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Answer a question grounded in a previously generated report."""
    data = _load_report(req.session_id)
    if data is None:
        raise HTTPException(status_code=404,
                            detail="Report session not found. Generate a report first.")
    try:
        answer, err = ai_insights.chat_answer(data, req.question, req.history)
    except Exception as e:
        import traceback
        print("=" * 70)
        print("CHAT FAILED:")
        print(traceback.format_exc())
        print("=" * 70)
        raise HTTPException(status_code=500, detail=f"Chat error: {e}")
    if err:
        raise HTTPException(status_code=500, detail=err)
    return {"answer": answer}


@app.get("/api/iac/check")
def iac_check():
    """Report whether aztfexport is installed."""
    ok, info = terraform_export.aztfexport_available()
    return {"available": ok, "info": info}


@app.post("/api/iac")
def generate_iac(req: ReportRequest):
    """Export Terraform IaC for the scope and return a zip download URL."""
    ok, info = terraform_export.aztfexport_available()
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=f"aztfexport is not available: {info}. See the README for install steps.")
    try:
        run_id = uuid.uuid4().hex[:8]
        out_root = os.path.join(IAC_DIR, run_id)
        summary = terraform_export.export_scope(
            req.subscription_id, req.subscription_name or req.subscription_id,
            req.resource_group, out_root)

        # Zip the whole export for download.
        zip_path = os.path.join(IAC_DIR, f"iac_{run_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(out_root):
                for fn in files:
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, out_root)
                    zf.write(full, arc)

        return {
            "download_url": f"/api/iac/iac_{run_id}.zip",
            "scope": summary["scope"],
            "rg_count": summary["rg_count"],
            "success_count": summary["success_count"],
            "total_resources": sum(r["resource_total"] for r in summary["exported"]),
            "errors": summary["errors"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"IaC export failed: {e}")


@app.get("/api/iac/{fname}")
def download_iac(fname: str):
    safe = os.path.basename(fname)
    path = os.path.join(IAC_DIR, safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="IaC archive not found")
    return FileResponse(
        path, media_type="application/zip", filename=safe,
        headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.get("/api/report/{fname}")
def download_report(fname: str):
    # Prevent path traversal.
    safe = os.path.basename(fname)
    path = os.path.join(REPORTS_DIR, safe)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(path, media_type="application/pdf", filename=safe)


# Serve the frontend at the root.
@app.get("/")
def root():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# (Optional) mount static dir if you add CSS/JS files later.
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
