"""
report.py
Collects the eight governance observations for a subscription (optionally
scoped to a single resource group). Every collector is wrapped so that an
API failure or missing data degrades to a clear "unavailable" note instead
of crashing the whole report.

Returns a single dict consumed by pdf_report.py.
"""
from datetime import datetime, timezone, timedelta
import traceback

from azure_clients import (
    compute_client,
    network_client,
    resource_client,
    advisor_client,
    get_credential,
)
from azure.mgmt.recoveryservicesbackup.activestamp import RecoveryServicesBackupClient
from azure.mgmt.recoveryservices import RecoveryServicesClient


# ----------------------------------------------------------------------------
# Helper: run a collector safely and capture errors as data, not crashes.
# ----------------------------------------------------------------------------
def _safe(label, fn, *args, **kwargs):
    try:
        return {"status": "ok", "data": fn(*args, **kwargs)}
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "trace": traceback.format_exc(limit=2),
            "note": f"Could not collect '{label}'. See error.",
        }


def _vm_iter(subscription_id, resource_group):
    """Yield VMs in scope (all in sub, or just one RG)."""
    cc = compute_client(subscription_id)
    if resource_group:
        return cc.virtual_machines.list(resource_group)
    return cc.virtual_machines.list_all()


# ----------------------------------------------------------------------------
# 1. Inventory
# ----------------------------------------------------------------------------
def collect_inventory(subscription_id, resource_group):
    rc = resource_client(subscription_id)
    if resource_group:
        items = rc.resources.list_by_resource_group(resource_group)
    else:
        items = rc.resources.list()
    by_type = {}
    total = 0
    rows = []
    for r in items:
        total += 1
        by_type[r.type] = by_type.get(r.type, 0) + 1
        rows.append({"name": r.name, "type": r.type, "location": r.location})
    summary = sorted(by_type.items(), key=lambda kv: -kv[1])
    return {"total": total, "by_type": summary, "rows": rows}


# ----------------------------------------------------------------------------
# 2. Backup coverage (Recovery Services vaults)
#    Strategy: find protected VM items across vaults in the subscription,
#    then compare to the full VM list to find unprotected VMs.
# ----------------------------------------------------------------------------
def collect_backup(subscription_id, resource_group):
    cred = get_credential()
    rsv = RecoveryServicesClient(cred, subscription_id)
    backup = RecoveryServicesBackupClient(cred, subscription_id)

    # Collect protected VM resource IDs across all vaults.
    protected_ids = set()
    vault_count = 0
    for vault in rsv.vaults.list_by_subscription_id():
        vault_count += 1
        vault_name = vault.name
        vault_rg = vault.id.split("/resourceGroups/")[1].split("/")[0]
        try:
            items = backup.backup_protected_items.list(vault_name, vault_rg)
            for it in items:
                props = it.properties
                vmid = getattr(props, "virtual_machine_id", None) or \
                       getattr(props, "source_resource_id", None)
                if vmid:
                    protected_ids.add(vmid.lower())
        except Exception:
            # A vault we can't read shouldn't kill the whole check.
            continue

    # Full VM list in scope.
    all_vms = []
    for vm in _vm_iter(subscription_id, resource_group):
        all_vms.append({"name": vm.name, "id": vm.id.lower()})

    protected, unprotected = [], []
    for vm in all_vms:
        if vm["id"] in protected_ids:
            protected.append(vm["name"])
        else:
            unprotected.append(vm["name"])

    return {
        "vault_count": vault_count,
        "total_vms": len(all_vms),
        "protected_count": len(protected),
        "unprotected_count": len(unprotected),
        "protected": protected,
        "unprotected": unprotected,
        "all_backed_up": len(unprotected) == 0 and len(all_vms) > 0,
    }


# ----------------------------------------------------------------------------
# 3. Patch status (Azure Update Manager - read last assessment, no active scan)
#    Reads the VM instance view patch assessment summary.
# ----------------------------------------------------------------------------
def collect_patching(subscription_id, resource_group):
    cc = compute_client(subscription_id)
    rows = []
    for vm in _vm_iter(subscription_id, resource_group):
        vm_rg = vm.id.split("/resourceGroups/")[1].split("/")[0]
        entry = {"name": vm.name, "rg": vm_rg}
        try:
            iv = cc.virtual_machines.instance_view(vm_rg, vm.name)
            ps = getattr(iv, "patch_status", None)
            if ps and getattr(ps, "available_patch_summary", None):
                aps = ps.available_patch_summary
                entry.update({
                    "last_assessed": str(getattr(aps, "last_modified_time", "")) or "unknown",
                    "critical_and_security": getattr(aps, "critical_and_security_patch_count", None),
                    "other": getattr(aps, "other_patch_count", None),
                    "assessment_status": str(getattr(aps, "status", "")) or "unknown",
                })
            else:
                entry.update({
                    "last_assessed": "no assessment data",
                    "assessment_status": "Update Manager assessment not found - "
                                         "enable periodic assessment on this VM",
                })
        except Exception as e:
            entry.update({"last_assessed": "error", "assessment_status": str(e)[:120]})
        rows.append(entry)
    return {"vms": rows, "count": len(rows)}


# ----------------------------------------------------------------------------
# 4 & 5. NSG analysis: internet-open rules + broad any-any rules
# ----------------------------------------------------------------------------
_ANY_SOURCES = {"*", "internet", "0.0.0.0/0", "any", "<nw>/0"}


def _is_broad(addr):
    if not addr:
        return False
    a = str(addr).strip().lower()
    return a in _ANY_SOURCES or a.endswith("/0")


def collect_nsg(subscription_id, resource_group):
    nc = network_client(subscription_id)
    if resource_group:
        nsgs = nc.network_security_groups.list(resource_group)
    else:
        nsgs = nc.network_security_groups.list_all()

    internet_open = []   # inbound allow from internet/any
    any_any = []         # any source AND any dest, any port
    findings_count = 0

    for nsg in nsgs:
        nsg_rg = nsg.id.split("/resourceGroups/")[1].split("/")[0]
        rules = list(nsg.security_rules or [])
        for rule in rules:
            if str(rule.direction).lower() != "inbound":
                continue
            if str(rule.access).lower() != "allow":
                continue

            srcs = list(rule.source_address_prefixes or [])
            if rule.source_address_prefix:
                srcs.append(rule.source_address_prefix)
            dsts = list(rule.destination_address_prefixes or [])
            if rule.destination_address_prefix:
                dsts.append(rule.destination_address_prefix)

            src_broad = any(_is_broad(s) for s in srcs)
            dst_broad = any(_is_broad(d) for d in dsts)
            port = rule.destination_port_range or ",".join(rule.destination_port_ranges or [])
            port_broad = str(port).strip() in ("*", "0-65535")

            if src_broad:
                internet_open.append({
                    "nsg": nsg.name, "rg": nsg_rg, "rule": rule.name,
                    "ports": port, "protocol": str(rule.protocol),
                    "priority": rule.priority,
                })
                findings_count += 1

            if src_broad and dst_broad and port_broad:
                any_any.append({
                    "nsg": nsg.name, "rg": nsg_rg, "rule": rule.name,
                    "protocol": str(rule.protocol), "priority": rule.priority,
                })

    return {
        "internet_open": internet_open,
        "internet_open_count": len(internet_open),
        "any_any": any_any,
        "any_any_count": len(any_any),
        "has_internet_exposure": len(internet_open) > 0,
        "has_any_any": len(any_any) > 0,
    }


# ----------------------------------------------------------------------------
# 6. Azure Advisor recommendations - Security, Reliability, Performance
# ----------------------------------------------------------------------------
def _advisor_in_rg(props, resource_group):
    if not resource_group:
        return True
    rid = getattr(props, "resource_metadata", None)
    if rid and getattr(rid, "resource_id", None):
        return f"/resourcegroups/{resource_group.lower()}/" in rid.resource_id.lower()
    return True  # keep if we can't tell, rather than drop silently


def collect_advisor(subscription_id, resource_group):
    """Collect Advisor recommendations split by category."""
    ac = advisor_client(subscription_id)
    buckets = {"security": [], "reliability": [], "performance": [],
               "cost": [], "operationalexcellence": []}
    for r in ac.recommendations.list():
        category = str(getattr(r, "category", "")).lower().replace(" ", "")
        if category not in buckets:
            continue
        if not _advisor_in_rg(r, resource_group):
            continue
        sd = getattr(r, "short_description", None)
        buckets[category].append({
            "problem": getattr(sd, "problem", "") if sd else "",
            "solution": getattr(sd, "solution", "") if sd else "",
            "impact": str(getattr(r, "impact", "")),
            "impacted_resource": getattr(r, "impacted_value", "") or "",
        })
    return {
        "security": buckets["security"],
        "reliability": buckets["reliability"],
        "performance": buckets["performance"],
        "security_count": len(buckets["security"]),
        "reliability_count": len(buckets["reliability"]),
        "performance_count": len(buckets["performance"]),
    }


# ----------------------------------------------------------------------------
# 6b. Cost per resource - last full month, amortized (Cost Management Query API)
# ----------------------------------------------------------------------------
def _col_index(cols, *candidates):
    """Find a column index case-insensitively from a list of candidate names."""
    lower = [c.lower() for c in cols]
    for cand in candidates:
        if cand.lower() in lower:
            return lower.index(cand.lower())
    return None


def collect_cost(subscription_id, resource_group):
    import requests
    cred = get_credential()
    token = cred.get_token("https://management.azure.com/.default").token
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    scope = f"/subscriptions/{subscription_id}"
    if resource_group:
        scope += f"/resourceGroups/{resource_group}"

    url = (f"https://management.azure.com{scope}"
           f"/providers/Microsoft.CostManagement/query?api-version=2024-08-01")

    # Compute the previous full calendar month as an explicit custom range,
    # because some scopes reject the "TheLastMonth" named timeframe.
    today = datetime.now(timezone.utc)
    first_this_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = first_this_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    time_period = {
        "from": last_month_start.strftime("%Y-%m-%dT00:00:00+00:00"),
        "to": last_month_end.strftime("%Y-%m-%dT23:59:59+00:00"),
    }

    body = {
        "type": "AmortizedCost",
        "timeframe": "Custom",
        "timePeriod": time_period,
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceId"}],
        },
    }

    def _run(req_body):
        r = requests.post(url, headers=headers, json=req_body, timeout=90)
        if r.status_code == 429:
            raise RuntimeError("Cost Management API throttled (429). Try again shortly.")
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                msg = err.get("message") or r.text[:300]
                code = err.get("code", r.status_code)
            except Exception:
                code, msg = r.status_code, r.text[:300]
            raise RuntimeError(f"Cost API {code}: {msg}")
        return r.json()

    rows, currency = [], ""
    payload = None
    try:
        payload = _run(body)
    except RuntimeError as e:
        # Retry with ActualCost + Cost metric, same custom range, on rejection.
        if "Invalid" in str(e) or "PreTaxCost" in str(e) or "not supported" in str(e):
            fallback = {
                "type": "ActualCost",
                "timeframe": "Custom",
                "timePeriod": time_period,
                "dataset": {
                    "granularity": "None",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                    "grouping": [{"type": "Dimension", "name": "ResourceId"}],
                },
            }
            payload = _run(fallback)
        else:
            raise

    pages = 0
    while payload and pages < 25:
        props = payload.get("properties", {})
        cols = [c["name"] for c in props.get("columns", [])]
        cost_i = _col_index(cols, "PreTaxCost", "Cost", "CostUSD")
        rid_i = _col_index(cols, "ResourceId")
        cur_i = _col_index(cols, "Currency", "BillingCurrency", "CurrencyCode")
        if cost_i is None or rid_i is None:
            break
        for row in props.get("rows", []):
            rid = row[rid_i] or "(unassigned)"
            name = rid.split("/")[-1] if "/" in rid else rid
            rtype = ("/".join(rid.split("/providers/")[-1].split("/")[:2])
                     if "/providers/" in rid else "")
            if cur_i is not None and not currency:
                currency = row[cur_i]
            try:
                cost_val = round(float(row[cost_i]), 2)
            except (TypeError, ValueError):
                cost_val = 0.0
            rows.append({"resource": name, "type": rtype, "cost": cost_val, "id": rid})

        nl = props.get("nextLink")
        if nl:
            r = requests.post(nl, headers=headers, json=body, timeout=90)
            if r.status_code >= 400:
                break
            payload = r.json()
            pages += 1
        else:
            break

    rows.sort(key=lambda r: -r["cost"])
    total = round(sum(r["cost"] for r in rows), 2)
    return {"rows": rows, "total": total, "currency": currency or "USD",
            "count": len(rows)}


# ----------------------------------------------------------------------------
# 6c. Storage accounts -> file shares -> file-share backup status
# ----------------------------------------------------------------------------
def collect_storage(subscription_id, resource_group):
    from azure.mgmt.storage import StorageManagementClient
    cred = get_credential()
    sm = StorageManagementClient(cred, subscription_id)
    backup = RecoveryServicesBackupClient(cred, subscription_id)
    rsv = RecoveryServicesClient(cred, subscription_id)

    # Build set of backed-up file shares (protected item friendly names).
    protected_shares = set()
    try:
        for vault in rsv.vaults.list_by_subscription_id():
            vname = vault.name
            vrg = vault.id.split("/resourceGroups/")[1].split("/")[0]
            try:
                for it in backup.backup_protected_items.list(vname, vrg):
                    p = it.properties
                    wtype = str(getattr(p, "workload_type", "") or
                                getattr(p, "backup_management_type", ""))
                    if "azurefileshare" in wtype.lower() or "azurestorage" in wtype.lower():
                        fn = getattr(p, "friendly_name", None)
                        if fn:
                            protected_shares.add(fn.lower())
            except Exception:
                continue
    except Exception:
        pass  # backup enumeration optional; shares still listed

    if resource_group:
        accounts = sm.storage_accounts.list_by_resource_group(resource_group)
    else:
        accounts = sm.storage_accounts.list()

    acct_rows = []
    for acct in accounts:
        acct_rg = acct.id.split("/resourceGroups/")[1].split("/")[0]
        shares = []
        try:
            for sh in sm.file_shares.list(acct_rg, acct.name):
                backed = sh.name.lower() in protected_shares
                shares.append({"name": sh.name, "backed_up": backed})
        except Exception:
            # Account may have file service disabled or be inaccessible.
            shares = []
        acct_rows.append({
            "name": acct.name,
            "rg": acct_rg,
            "kind": str(acct.kind),
            "has_file_shares": len(shares) > 0,
            "share_count": len(shares),
            "shares": shares,
            "shares_protected": sum(1 for s in shares if s["backed_up"]),
        })
    return {"accounts": acct_rows, "count": len(acct_rows)}


# ----------------------------------------------------------------------------
# 7 & 8. Planned maintenance + service retirements (Service Health / advisories)
#    These come from Microsoft.ResourceHealth events. Fiddly + partial.
# ----------------------------------------------------------------------------
def collect_health_events(subscription_id, resource_group):
    """
    Pulls Service Health events via the ResourceHealth events API.
    Filters to planned-maintenance and health-advisory (which covers many
    service-retirement notices) with an impact window in the next ~3 months.
    """
    import requests
    cred = get_credential()
    token = cred.get_token("https://management.azure.com/.default").token
    headers = {"Authorization": f"Bearer {token}"}

    url = (f"https://management.azure.com/subscriptions/{subscription_id}"
           f"/providers/Microsoft.ResourceHealth/events"
           f"?api-version=2022-10-01&queryStartTime="
           f"{(datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')}")

    planned, retirements, advisories = [], [], []
    horizon = datetime.now(timezone.utc) + timedelta(days=92)

    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    for ev in resp.json().get("value", []):
        p = ev.get("properties", {})
        etype = (p.get("eventType") or "").lower()
        title = p.get("title", "")
        summary = (p.get("summary", "") or "")[:300]
        impact_start = p.get("impactStartTime", "")
        rec = {"title": title, "summary": summary, "impact_start": impact_start,
               "status": p.get("status", "")}

        if etype == "plannedmaintenance":
            planned.append(rec)
        elif etype in ("healthadvisory", "securityadvisory"):
            # Service retirements typically arrive as health advisories.
            if "retire" in (title + summary).lower() or "deprecat" in (title + summary).lower():
                retirements.append(rec)
            else:
                advisories.append(rec)

    return {
        "planned_maintenance": planned,
        "planned_count": len(planned),
        "retirements": retirements,
        "retirement_count": len(retirements),
        "other_advisories": advisories,
        "advisory_count": len(advisories),
        "note": "Health/advisory data depends on Service Health publishing events "
                "for this subscription; absence here does not guarantee none exist.",
    }


# ----------------------------------------------------------------------------
# Azure Policy: compliance summary + exemptions
# ----------------------------------------------------------------------------
def collect_policy(subscription_id, resource_group):
    """
    Returns policy compliance summary (compliant/non-compliant counts, top
    non-compliant policies) and the list of policy exemptions in scope.
    """
    from azure.mgmt.policyinsights import PolicyInsightsClient
    from azure.mgmt.resource import PolicyClient
    cred = get_credential()

    result = {"compliance": None, "exemptions": [], "noncompliant_policies": []}

    # ---- Compliance summary via Policy Insights ----
    try:
        pic = PolicyInsightsClient(cred, subscription_id)
        summary = pic.policy_states.summarize_for_subscription(
            policy_states_summary_resource="latest",
            subscription_id=subscription_id)
        # summary.value[0].results has the headline counts.
        val = list(summary.value or [])
        if val:
            res = val[0].results
            non_compliant_resources = getattr(res, "non_compliant_resources", 0) or 0
            non_compliant_policies = getattr(res, "non_compliant_policies", 0) or 0
            # Resource compliance breakdown.
            compliant = noncompliant = 0
            for d in (getattr(res, "resource_details", None) or []):
                st = str(getattr(d, "compliance_state", "")).lower()
                cnt = getattr(d, "count", 0) or 0
                if st == "compliant":
                    compliant += cnt
                elif st == "noncompliant":
                    noncompliant += cnt
            total = compliant + noncompliant
            result["compliance"] = {
                "non_compliant_resources": non_compliant_resources,
                "non_compliant_policies": non_compliant_policies,
                "compliant_resources": compliant,
                "evaluated_resources": total,
                "compliance_pct": round(100 * compliant / total, 1) if total else None,
            }
    except Exception as e:
        result["compliance_error"] = str(e)[:200]

    # ---- Top non-compliant policy assignments (via query results) ----
    try:
        pic = PolicyInsightsClient(cred, subscription_id)
        try:
            from azure.mgmt.policyinsights.models import QueryOptions
            qopts = QueryOptions(filter="ComplianceState eq 'NonCompliant'", top=500)
            states = pic.policy_states.list_query_results_for_subscription(
                policy_states_resource="latest",
                subscription_id=subscription_id,
                query_options=qopts)
        except Exception:
            # Fallback: no filter object, filter in Python.
            states = pic.policy_states.list_query_results_for_subscription(
                policy_states_resource="latest",
                subscription_id=subscription_id)
        by_policy = {}
        for s in states:
            state = str(getattr(s, "compliance_state", "")).lower()
            if state and state != "noncompliant":
                continue
            name = (getattr(s, "policy_definition_name", None)
                    or getattr(s, "policy_assignment_name", None) or "unknown")
            disp = getattr(s, "policy_definition_action", "") or ""
            by_policy.setdefault(name, {"count": 0, "effect": disp})
            by_policy[name]["count"] += 1
        top = sorted(by_policy.items(), key=lambda kv: -kv[1]["count"])[:15]
        result["noncompliant_policies"] = [
            {"policy": k, "noncompliant_resources": v["count"], "effect": v["effect"]}
            for k, v in top]
    except Exception as e:
        result["noncompliant_detail_error"] = str(e)[:200]

    # ---- Policy exemptions ----
    try:
        pc = PolicyClient(cred, subscription_id)
        if resource_group:
            exemptions = pc.policy_exemptions.list_for_resource_group(resource_group)
        else:
            exemptions = pc.policy_exemptions.list()
        for ex in exemptions:
            p = ex
            result["exemptions"].append({
                "name": getattr(p, "name", ""),
                "display_name": getattr(p, "display_name", "") or getattr(p, "name", ""),
                "category": str(getattr(p, "exemption_category", "")),  # Waiver / Mitigated
                "description": (getattr(p, "description", "") or "")[:300],
                "expires_on": str(getattr(p, "expires_on", "") or "no expiry"),
                "policy_assignment_id": (getattr(p, "policy_assignment_id", "") or "").split("/")[-1],
                "scope": (getattr(p, "id", "") or "").split("/providers/Microsoft.Authorization")[0],
            })
    except Exception as e:
        result["exemptions_error"] = str(e)[:200]

    result["exemption_count"] = len(result["exemptions"])
    return result


# ----------------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------------
def build_report(subscription_id, resource_group=None, subscription_name=None):
    scope = f"Resource Group: {resource_group}" if resource_group else "Entire Subscription"
    data = {
        "meta": {
            "subscription_id": subscription_id,
            "subscription_name": subscription_name or subscription_id,
            "resource_group": resource_group,
            "scope": scope,
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
        "inventory":  _safe("inventory", collect_inventory, subscription_id, resource_group),
        "backup":     _safe("backup", collect_backup, subscription_id, resource_group),
        "patching":   _safe("patching", collect_patching, subscription_id, resource_group),
        "nsg":        _safe("nsg", collect_nsg, subscription_id, resource_group),
        "advisor":    _safe("advisor", collect_advisor, subscription_id, resource_group),
        "cost":       _safe("cost", collect_cost, subscription_id, resource_group),
        "storage":    _safe("storage", collect_storage, subscription_id, resource_group),
        "policy":     _safe("policy compliance", collect_policy, subscription_id, resource_group),
        "health":     _safe("health events", collect_health_events, subscription_id, resource_group),
    }

    # AI-generated sections (optional; degrade gracefully if no API key/SDK).
    # These functions already return a {"status": ..., "text"/"error": ...} dict,
    # so we call them directly rather than through _safe (which would double-wrap).
    try:
        import ai_insights
        try:
            data["ai_remediation"] = ai_insights.generate_remediation(data)
        except Exception as e:
            data["ai_remediation"] = {"status": "error", "error": str(e)}
        try:
            data["ai_cost"] = ai_insights.generate_cost_insights(data)
        except Exception as e:
            data["ai_cost"] = {"status": "error", "error": str(e)}
        try:
            data["ai_policy"] = ai_insights.generate_policy_insights(data)
        except Exception as e:
            data["ai_policy"] = {"status": "error", "error": str(e)}
    except Exception as e:
        data["ai_remediation"] = {"status": "error", "error": str(e)}
        data["ai_cost"] = {"status": "error", "error": str(e)}
        data["ai_policy"] = {"status": "error", "error": str(e)}

    return data
