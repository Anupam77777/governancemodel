"""
azure_clients.py
Centralized Azure authentication and client factory.
Uses DefaultAzureCredential, which picks up your `az login` session automatically.
"""
from azure.identity import DefaultAzureCredential
from azure.mgmt.subscription import SubscriptionClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.advisor import AdvisorManagementClient

# Single shared credential. DefaultAzureCredential will use your az CLI login.
_credential = None


def get_credential():
    global _credential
    if _credential is None:
        # exclude_interactive_browser keeps it from popping a browser during API calls;
        # it relies on your existing `az login`.
        _credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _credential


def subscription_client():
    return SubscriptionClient(get_credential())


def resource_client(subscription_id: str):
    return ResourceManagementClient(get_credential(), subscription_id)


def compute_client(subscription_id: str):
    return ComputeManagementClient(get_credential(), subscription_id)


def network_client(subscription_id: str):
    return NetworkManagementClient(get_credential(), subscription_id)


def advisor_client(subscription_id: str):
    return AdvisorManagementClient(get_credential(), subscription_id)
