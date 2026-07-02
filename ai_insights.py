"""
ai_insights.py
Uses the Anthropic Claude API to turn the structured governance data into:
  1. Remediation narratives  — prioritized, plain-English action plan from findings
  4. Cost / rightsizing insights — analysis of cost data + inventory + advisor

Both are optional: if no ANTHROPIC_API_KEY is set or the SDK isn't installed,
the functions return a clear "unavailable" note instead of failing the report.

Set your key once (PowerShell):
    setx ANTHROPIC_API_KEY "sk-ant-..."
then restart the terminal so the bot process inherits it.
"""
import os
import json

# Model choice: Sonnet balances quality and cost for this analysis workload.
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1500


def _client():
    """Return an Anthropic client, or (None, reason) if unavailable."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, "ANTHROPIC_API_KEY environment variable not set"
    try:
        from anthropic import Anthropic
    except ImportError:
        return None, "anthropic package not installed (pip install anthropic)"
    try:
        return Anthropic(api_key=key), None
    except Exception as e:
        return None, f"Failed to init Anthropic client: {e}"


def _ask(system, user_prompt):
    """Single-turn call to Claude. Returns (text, error)."""
    client, reason = _client()
    if client is None:
        return None, reason
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        # Concatenate any text blocks in the response.
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        return ("\n".join(parts).strip() or "(empty response)"), None
    except Exception as e:
        return None, f"Claude API call failed: {e}"


# ----------------------------------------------------------------------------
# Compact the big report dict into a small findings summary for the prompt.
# Keeps token usage low and avoids sending raw resource IDs.
# ----------------------------------------------------------------------------
def _summarize_findings(report):
    f = {}

    b = report.get("backup", {})
    if b.get("status") == "ok":
        d = b["data"]
        f["backup"] = {
            "total_vms": d["total_vms"],
            "unprotected_count": d["unprotected_count"],
            "unprotected_vms": d["unprotected"][:20],
        }

    p = report.get("patching", {})
    if p.get("status") == "ok":
        vms = p["data"]["vms"]
        unassessed = [v["name"] for v in vms
                      if "no assessment" in str(v.get("last_assessed", "")).lower()]
        high_crit = [{"vm": v["name"], "crit_sec": v.get("critical_and_security")}
                     for v in vms
                     if isinstance(v.get("critical_and_security"), int)
                     and v["critical_and_security"] > 0]
        f["patching"] = {"vms_without_assessment": unassessed[:20],
                         "vms_with_pending_critical": high_crit[:20]}

    n = report.get("nsg", {})
    if n.get("status") == "ok":
        d = n["data"]
        f["nsg"] = {
            "internet_open_rules": [
                {"nsg": r["nsg"], "rule": r["rule"], "ports": str(r["ports"]),
                 "protocol": r["protocol"]} for r in d["internet_open"][:25]],
            "any_any_rules": [
                {"nsg": r["nsg"], "rule": r["rule"]} for r in d["any_any"][:25]],
        }

    a = report.get("advisor", {})
    if a.get("status") == "ok":
        d = a["data"]
        f["advisor"] = {
            "security": [{"problem": r["problem"], "impact": r["impact"]}
                         for r in d["security"][:15]],
            "reliability": [{"problem": r["problem"], "impact": r["impact"]}
                            for r in d["reliability"][:15]],
            "performance": [{"problem": r["problem"], "impact": r["impact"]}
                            for r in d["performance"][:15]],
        }

    s = report.get("storage", {})
    if s.get("status") == "ok":
        d = s["data"]
        unprotected_shares = []
        for acct in d["accounts"]:
            for sh in acct.get("shares", []):
                if not sh["backed_up"]:
                    unprotected_shares.append({"account": acct["name"], "share": sh["name"]})
        f["storage"] = {"file_shares_without_backup": unprotected_shares[:20]}

    return f


def _cost_context(report):
    c = report.get("cost", {})
    inv = report.get("inventory", {})
    adv = report.get("advisor", {})
    ctx = {}
    if c.get("status") == "ok":
        d = c["data"]
        ctx["currency"] = d["currency"]
        ctx["total"] = d["total"]
        ctx["top_resources"] = [
            {"resource": r["resource"], "type": r["type"], "cost": r["cost"]}
            for r in d["rows"][:30]
        ]
    if inv.get("status") == "ok":
        ctx["resource_type_counts"] = dict(inv["data"]["by_type"][:25])
    if adv.get("status") == "ok":
        ctx["advisor_cost_and_perf"] = {
            "performance": [r["problem"] for r in adv["data"]["performance"][:15]],
        }
    return ctx


# ----------------------------------------------------------------------------
# 1. Remediation narrative
# ----------------------------------------------------------------------------
REMEDIATION_SYSTEM = (
    "You are an Azure cloud security and governance expert. You are given a "
    "JSON summary of findings from an automated tenant scan. Produce a concise, "
    "prioritized remediation plan for an engineering team. Rules:\n"
    "- Order issues by real-world risk (security exposure and data-loss risk first).\n"
    "- For each issue: one line stating the problem, one line on why it matters, "
    "and a concrete fix including an example `az` CLI command or Terraform snippet.\n"
    "- Be specific to the named resources where possible.\n"
    "- Use short Markdown: '### Priority N — <title>' headers and tight bullets.\n"
    "- If a category has no findings, omit it. Do not invent issues not in the data.\n"
    "- Keep the whole plan under ~500 words."
)


def generate_remediation(report):
    findings = _summarize_findings(report)
    if not any(findings.values()):
        return {"status": "ok", "text": "No actionable findings to remediate — "
                "the scanned scope looks clean across backup, patching, NSG, "
                "Advisor, and storage checks."}
    prompt = ("Here is the findings summary as JSON:\n\n"
              f"{json.dumps(findings, indent=2)}\n\n"
              "Write the prioritized remediation plan.")
    text, err = _ask(REMEDIATION_SYSTEM, prompt)
    if err:
        return {"status": "error", "error": err}
    return {"status": "ok", "text": text}


# ----------------------------------------------------------------------------
# 4. Cost / rightsizing insights
# ----------------------------------------------------------------------------
COST_SYSTEM = (
    "You are an Azure FinOps analyst. You are given JSON with last month's "
    "per-resource amortized cost, resource-type counts, and Advisor performance "
    "recommendations. Produce a concise cost-optimization analysis. Rules:\n"
    "- Identify the biggest cost drivers and any that look disproportionate.\n"
    "- Call out likely rightsizing / cleanup opportunities (oversized VMs, idle "
    "resources, possible duplicates by naming, untagged spend) — but mark anything "
    "speculative as 'verify' rather than asserting it as fact.\n"
    "- Where Advisor performance items relate to a costly resource, connect them.\n"
    "- Give a short prioritized list of cost actions with rough rationale.\n"
    "- Use short Markdown with '### ' headers and tight bullets. Under ~450 words.\n"
    "- Do not invent costs or resources not present in the data."
)


def generate_cost_insights(report):
    ctx = _cost_context(report)
    if not ctx.get("top_resources"):
        return {"status": "ok", "text": "No cost data available to analyze for "
                "this scope (the cost section may be unavailable)."}
    prompt = ("Here is the cost and inventory context as JSON:\n\n"
              f"{json.dumps(ctx, indent=2)}\n\n"
              "Write the cost-optimization analysis.")
    text, err = _ask(COST_SYSTEM, prompt)
    if err:
        return {"status": "error", "error": err}
    return {"status": "ok", "text": text}


def available():
    """Quick check used by the report endpoint / UI."""
    client, reason = _client()
    return (client is not None), (reason or "ready")


# ----------------------------------------------------------------------------
# Policy compliance + exemption risk analysis
# ----------------------------------------------------------------------------
POLICY_SYSTEM = (
    "You are an Azure Policy and compliance expert. You are given a subscription's "
    "policy compliance summary, its top non-compliant policies, and the list of "
    "policy EXEMPTIONS currently in place. Produce a concise analysis. Rules:\n"
    "- Start with a one-line compliance posture summary.\n"
    "- Then focus on EXEMPTIONS: for each notable exemption, explain in plain "
    "English what protection it removes and how it could harm the environment "
    "(security gap, data exposure, drift, audit risk). Pay special attention to "
    "'Waiver' exemptions (which suppress without remediation) and exemptions with "
    "no expiry date (which never lapse).\n"
    "- Flag the single riskiest exemption explicitly.\n"
    "- Recommend concrete actions (review, add expiry, scope down, remove).\n"
    "- Use short Markdown with '### ' headers and tight bullets. Under ~450 words.\n"
    "- Do not invent exemptions or policies not present in the data."
)


def generate_policy_insights(report):
    pol = report.get("policy", {})
    if pol.get("status") != "ok":
        return {"status": "ok", "text": "Policy compliance data was not available "
                "for this scope, so no exemption analysis could be produced."}
    data = pol["data"]
    ctx = {
        "compliance": data.get("compliance"),
        "top_noncompliant_policies": data.get("noncompliant_policies", [])[:15],
        "exemptions": [
            {"name": e["display_name"], "category": e["category"],
             "expires_on": e["expires_on"], "assignment": e["policy_assignment_id"],
             "description": e["description"]}
            for e in data.get("exemptions", [])[:40]
        ],
        "exemption_count": data.get("exemption_count", 0),
    }
    if not ctx["exemptions"] and not ctx["compliance"]:
        return {"status": "ok", "text": "No policy compliance data or exemptions found "
                "for this scope."}
    prompt = ("Here is the policy compliance and exemption context as JSON:\n\n"
              f"{json.dumps(ctx, indent=2)}\n\n"
              "Write the compliance and exemption-risk analysis.")
    text, err = _ask(POLICY_SYSTEM, prompt)
    if err:
        return {"status": "error", "error": err}
    return {"status": "ok", "text": text}


# ----------------------------------------------------------------------------
# Chatbot: answer questions grounded in a specific generated report.
# We pass a compact-but-complete text view of the report as context (no RAG
# needed at this size — the whole report fits in context for better accuracy).
# ----------------------------------------------------------------------------
def report_to_context(report):
    """Render the report dict into a compact text block for the LLM context."""
    lines = []
    m = report.get("meta", {})
    lines.append("GOVERNANCE REPORT")
    lines.append(f"Subscription: {m.get('subscription_name')} ({m.get('subscription_id')})")
    lines.append(f"Scope: {m.get('scope')}")
    lines.append(f"Generated: {m.get('generated_utc')}")
    lines.append("")

    def ok(key):
        b = report.get(key, {})
        return b.get("data") if b.get("status") == "ok" else None

    inv = ok("inventory")
    if inv:
        lines.append(f"INVENTORY: {inv['total']} resources total.")
        for t, c in inv["by_type"][:40]:
            lines.append(f"  - {t}: {c}")
        lines.append("")

    b = ok("backup")
    if b:
        lines.append(f"BACKUP: {b['protected_count']}/{b['total_vms']} VMs protected "
                     f"across {b['vault_count']} vault(s).")
        if b["unprotected"]:
            lines.append(f"  Unprotected VMs: {', '.join(b['unprotected'][:50])}")
        lines.append("")

    p = ok("patching")
    if p:
        lines.append("PATCHING (Azure Update Manager):")
        for v in p["vms"][:60]:
            lines.append(f"  - {v['name']}: assessed={v.get('last_assessed')}, "
                         f"crit+sec={v.get('critical_and_security')}, "
                         f"status={v.get('assessment_status','')[:60]}")
        lines.append("")

    n = ok("nsg")
    if n:
        lines.append(f"NSG INTERNET EXPOSURE: {n['internet_open_count']} inbound rule(s) "
                     f"open to Internet/Any; {n['any_any_count']} any-any rule(s).")
        for r in n["internet_open"][:40]:
            lines.append(f"  - {r['nsg']}/{r['rule']} ports={r['ports']} proto={r['protocol']}")
        lines.append("")

    a = ok("advisor")
    if a:
        lines.append(f"ADVISOR: security={a['security_count']}, "
                     f"reliability={a['reliability_count']}, performance={a['performance_count']}.")
        for cat in ("security", "reliability", "performance"):
            for r in a[cat][:15]:
                lines.append(f"  [{cat}] ({r['impact']}) {r['problem']}")
        lines.append("")

    c = ok("cost")
    if c:
        lines.append(f"COST (last month, amortized): total {c['total']} {c['currency']} "
                     f"across {c['count']} resources. Top resources:")
        for r in c["rows"][:40]:
            lines.append(f"  - {r['resource']} ({r['type']}): {r['cost']} {c['currency']}")
        lines.append("")

    s = ok("storage")
    if s:
        lines.append(f"STORAGE: {s['count']} account(s).")
        for acct in s["accounts"][:40]:
            shares = (f"{acct['shares_protected']}/{acct['share_count']} shares backed up"
                      if acct["has_file_shares"] else "no file shares")
            lines.append(f"  - {acct['name']} (rg={acct['rg']}, {acct['kind']}): {shares}")
        lines.append("")

    h = ok("health")
    if h:
        lines.append(f"HEALTH: {h['planned_count']} planned maintenance, "
                     f"{h['retirement_count']} retirement notice(s) (next 3 months).")
        for x in h["planned_maintenance"][:15]:
            lines.append(f"  [maintenance] {x['title']} starts {x.get('impact_start','')}")
        for x in h["retirements"][:15]:
            lines.append(f"  [retirement] {x['title']}")
        lines.append("")

    pol = ok("policy")
    if pol:
        comp = pol.get("compliance")
        if comp:
            lines.append(f"POLICY COMPLIANCE: {comp.get('compliance_pct')}% compliant "
                         f"({comp.get('compliant_resources')}/{comp.get('evaluated_resources')}); "
                         f"{comp.get('non_compliant_resources')} non-compliant resources across "
                         f"{comp.get('non_compliant_policies')} policies.")
        for p in pol.get("noncompliant_policies", [])[:10]:
            lines.append(f"  [non-compliant] {p['policy']}: {p['noncompliant_resources']} resources")
        lines.append(f"POLICY EXEMPTIONS: {pol.get('exemption_count',0)} total.")
        for e in pol.get("exemptions", [])[:20]:
            lines.append(f"  - {e['display_name']} (type={e['category']}, "
                         f"expires={e['expires_on']}, assignment={e['policy_assignment_id']})")
        lines.append("")

    air = report.get("ai_remediation")
    if air and air.get("status") == "ok":
        lines.append("AI REMEDIATION PLAN (already generated):")
        lines.append(air["text"])
        lines.append("")

    return "\n".join(lines)


CHAT_SYSTEM = (
    "You are an Azure governance assistant. You answer questions strictly based on "
    "the governance report provided below. Rules:\n"
    "- Answer ONLY from the report data. If the report does not contain the answer, "
    "say so plainly rather than guessing.\n"
    "- Be concise and specific; cite the actual resource names, counts, and values "
    "from the report.\n"
    "- When asked for advice, give practical Azure-specific guidance, and include "
    "an example `az` CLI command where useful.\n"
    "- Use short Markdown (bullets, bold) for readability.\n\n"
    "===== GOVERNANCE REPORT =====\n{context}\n===== END REPORT ====="
)


def chat_answer(report, question, history=None):
    """
    Answer a single chat question grounded in the report.
    `history` is a list of {role, content} dicts for multi-turn context.
    Returns (answer_text, error).
    """
    client, reason = _client()
    if client is None:
        return None, reason
    context = report_to_context(report)
    system = CHAT_SYSTEM.format(context=context)

    messages = []
    if history:
        for turn in history[-8:]:
            role = turn.get("role")
            content = turn.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            system=system,
            messages=messages,
        )
        parts = [blk.text for blk in msg.content if getattr(blk, "type", "") == "text"]
        return ("\n".join(parts).strip() or "(empty response)"), None
    except Exception as e:
        return None, f"Claude API call failed: {e}"
