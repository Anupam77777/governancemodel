"""
inventory.py
Lists subscriptions and resource groups for the UI dropdowns.
"""
from azure_clients import subscription_client, resource_client


def list_subscriptions():
    """Return all subscriptions the logged-in identity can see."""
    client = subscription_client()
    subs = []
    for s in client.subscriptions.list():
        subs.append({
            "subscription_id": s.subscription_id,
            "display_name": s.display_name,
            "state": str(s.state),
        })
    return subs


def list_resource_groups(subscription_id: str):
    """Return all resource groups in a subscription."""
    client = resource_client(subscription_id)
    rgs = []
    for rg in client.resource_groups.list():
        rgs.append({
            "name": rg.name,
            "location": rg.location,
        })
    return rgs


def list_resources(subscription_id: str, resource_group: str | None = None):
    """Return resources for a subscription, optionally filtered to one RG."""
    client = resource_client(subscription_id)
    items = []
    if resource_group:
        iterator = client.resources.list_by_resource_group(resource_group)
    else:
        iterator = client.resources.list()
    for r in iterator:
        items.append({
            "name": r.name,
            "type": r.type,
            "location": r.location,
            "id": r.id,
        })
    return items
