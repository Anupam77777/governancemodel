# Azure Governance Bot

A local web bot that, for a selected Azure subscription or resource group:
1. Exports existing deployments as Terraform IaC and pushes to Azure DevOps
2. Generates a PDF governance report

Runs entirely on your Windows laptop using your `az login` session.

---

## Step 1 — Scaffold, auth, and inventory (current)

### Prerequisites (install once)

1. **Python 3.11+** — https://www.python.org/downloads/ (check "Add to PATH")
2. **Azure CLI** — https://learn.microsoft.com/cli/azure/install-azure-cli-windows
   After install, open a new terminal and run:
   ```
   az login
   ```
   This opens a browser; sign in. The bot reuses this session.

> Terraform and aztfexport are only needed from Step 3 — skip for now.

### Setup

Open PowerShell in the `backend` folder:

```powershell
cd azure-gov-bot\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If activation is blocked by execution policy, run once:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### Run

```powershell
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 in your browser.

### What success looks like

- The **Subscription** dropdown fills with your real subscriptions.
- Pick one → the **Resource Group** dropdown fills with that sub's RGs.
- The **Submit** button confirms your selected scope (full pipeline comes in Step 5).

If the subscription dropdown says "Failed to load," your `az login` likely
expired or didn't complete — re-run `az login` and refresh.

---

## Coming next
- Step 2: Governance report (data collection + PDF)
- Step 3: Terraform export via aztfexport
- Step 4: Azure DevOps push with subscription/RG folder structure
- Step 5: Wire the Submit button to orchestrate everything

---

## Step 3 — Terraform IaC export (current)

### Install Terraform + aztfexport on Windows

**Terraform** (via winget):
```powershell
winget install HashiCorp.Terraform
```
Or download from https://developer.hashicorp.com/terraform/install and add to PATH.

**aztfexport** (Microsoft Azure Export for Terraform):
```powershell
winget install Microsoft.Azure.AztfExport
```
Or see https://github.com/Azure/aztfexport#install. Verify both:
```powershell
terraform -version
aztfexport --version
```

Restart the bot terminal after installing so the new PATH is picked up.

### Using it
- Click **Generate Terraform IaC** to export the selected scope. The bot runs
  `aztfexport` per resource group, splits resources into `network.tf`,
  `compute.tf`, `storage.tf`, etc., adds a `providers.tf` (Azure Storage remote
  backend) and a `README.md`, then gives you a downloadable `.zip`.
- Click **Generate PDF Report** for the governance report (independent button).

### Important: what Terraform restores
The exported code rebuilds **infrastructure configuration**, not **data**.
A `terraform apply` into a fresh subscription recreates VNets, NSGs, VM
definitions, storage accounts, etc. It does NOT restore VM disk contents,
storage blobs/files, or database data — use Azure Backup for those. Each
generated folder's README explains this and includes Azure DevOps pipeline
steps for init/plan/apply.

### Note on remote state
`providers.tf` uses an Azure Storage backend with placeholder values
(`REPLACE_tfstate_rg`, `REPLACE_tfstate_storage`). Put that state storage in a
DIFFERENT subscription so it survives if the target subscription is deleted.
