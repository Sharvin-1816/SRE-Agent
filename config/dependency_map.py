"""
config/dependency_map.py

Defines which services depend on which.
Used by the blast radius estimator to calculate cascading failure probability.

Reading: "if X fails, these services are directly impacted"
"""

# Direct dependency graph
# key   = service that is failing
# value = list of services that directly depend on it
DEPENDENCY_GRAPH = {
    "gateway_service": [
        "payment_service",
        "cart_service",
        "notification_service",
        "auth_service",
        "inventory_service",
    ],
    "auth_service": [
        "payment_service",
        "cart_service",
        "notification_service",
        "inventory_service",
    ],
    "payment_service": [
        "cart_service",
        "notification_service",
    ],
    "cart_service": [
        "inventory_service",
        "notification_service",
    ],
    "inventory_service": [],
    "notification_service": [],
}

# Base failure propagation probability per dependency tier
# These are starting estimates — agent adjusts based on historical correlation
TIER_PROBABILITIES = {
    1: 0.80,   # direct dependency — high chance of impact
    2: 0.45,   # one hop away
    3: 0.20,   # two hops away
}

# Human-readable descriptions of what each service does
# Helps the LLM reason about business impact
SERVICE_DESCRIPTIONS = {
    "payment_service":      "Handles all payment transactions and checkout flows",
    "cart_service":         "Manages shopping cart state and order creation",
    "notification_service": "Sends emails, SMS, and push notifications to users",
    "auth_service":         "Authenticates all user sessions across every service",
    "inventory_service":    "Tracks product stock levels and availability",
    "gateway_service":      "API gateway — all external traffic passes through here",
}


def get_dependents(service_name: str, visited: set = None, tier: int = 1) -> list[dict]:
    """
    Recursively get all services impacted if service_name fails.
    Returns list of {service, tier, base_probability, description}
    """
    if visited is None:
        visited = set()

    if service_name not in DEPENDENCY_GRAPH:
        return []

    results = []
    for dependent in DEPENDENCY_GRAPH.get(service_name, []):
        if dependent in visited:
            continue
        visited.add(dependent)

        prob = TIER_PROBABILITIES.get(tier, 0.10)
        results.append({
            "service":          dependent,
            "tier":             tier,
            "base_probability": prob,
            "description":      SERVICE_DESCRIPTIONS.get(dependent, ""),
        })

        # Recurse for indirect dependencies
        if tier < 3:
            results.extend(get_dependents(dependent, visited, tier + 1))

    return results


def get_dependency_summary(service_name: str) -> str:
    """
    Returns a plain-text summary of the blast radius for LLM context.
    Example:
        If gateway_service fails:
          - payment_service (direct, 80% base risk): Handles all payment transactions
          - cart_service (direct, 80% base risk): Manages shopping cart state
          ...
    """
    dependents = get_dependents(service_name)
    if not dependents:
        return f"{service_name} has no known downstream dependencies."

    lines = [f"If {service_name} fails, the following services are at risk:"]
    for d in dependents:
        tier_label = "direct" if d["tier"] == 1 else f"tier-{d['tier']}"
        lines.append(
            f"  - {d['service']} ({tier_label}, {int(d['base_probability']*100)}% base risk): "
            f"{d['description']}"
        )
    return "\n".join(lines)