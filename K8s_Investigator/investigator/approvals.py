"""Human-in-the-loop gates for propose_fix. An approver takes the proposal dict and returns bool."""
import json

from .tools import is_safe_namespace


def describe(proposal: dict) -> str:
    return (f"\n=== PROPOSED CHANGE (needs your approval) ===\n"
            f"namespace : {proposal['namespace']}\n"
            f"deployment: {proposal['deployment']}\n"
            f"reason    : {proposal['reason']}\n"
            f"patch     : {json.dumps(proposal['patch'], indent=2)}\n")


def ask_terminal(proposal: dict) -> bool:
    print(describe(proposal))
    return input("Apply this change? [y/N] ").strip().lower() in {"y", "yes"}


def deny_all(proposal: dict) -> bool:
    print(describe(proposal) + "--> auto-DENIED (read-only run)\n")
    return False


def auto_sandbox(proposal: dict) -> bool:
    """Lab/testing only: approves without asking, and only inside sandbox namespaces."""
    ok = is_safe_namespace(proposal["namespace"])
    print(describe(proposal) + f"--> {'auto-approved (sandbox lab mode)' if ok else 'denied'}\n")
    return ok


APPROVERS = {"ask": ask_terminal, "deny": deny_all, "auto-sandbox": auto_sandbox}
