"""Tenant-scoped command replay with rejection and payload conflicts."""

from .model import Family, case, source


def commands(seed: int) -> Family:
    contract = """# Account command processor

Process `commands` in arrival order against integer `balances` keyed by
`tenant/account`. Each command contains tenant, account, id, and signed integer
amount. Missing accounts begin at zero. A command making the balance negative
returns `rejected` and has no effect, including on replay tracking.

A successfully applied command reserves its id within its tenant. Repeating the
same account and amount under that tenant/id returns `duplicate` without applying
it again. Reusing that tenant/id for a different account or amount returns
`conflict`, with no effect. Other tenants may independently use the same id.
Return {"balances": updated_balances, "results": statuses_in_input_order}.
Do not create a missing account on rejection. All fields have the declared types.

Retries and declined commands currently produce inconsistent account exports.
Repair the processing path while retaining its state-store interface.
Run `python public_checks.py` for the public examples.
"""
    entry = source("""
        import json
        import sys
        from ledger import Ledger
        from operations import execute

        def process(batch):
            ledger = Ledger(batch["balances"])
            results = [execute(ledger, command) for command in batch["commands"]]
            return {"balances": ledger.balances, "results": results}

        if __name__ == "__main__":
            request = json.load(sys.stdin)
            json.dump(process(request), sys.stdout)
    """)
    ledger = source("""
        class Ledger:
            def __init__(self, balances):
                self.balances = dict(balances)
                self.applied = {}

            def previous(self, key):
                return self.applied.get(key)

            def record(self, key, payload):
                self.applied[key] = payload

            def change(self, account, amount):
                updated = self.balances.get(account, 0) + amount
                if updated < 0:
                    return False
                self.balances[account] = updated
                return True
    """)
    operations = source("""
        def execute(ledger, command):
            account = command["tenant"] + "/" + command["account"]
            key = command["id"]
            payload = (account, command["amount"])
            ledger.record(key, payload)
            return "applied" if ledger.change(account, command["amount"]) else "rejected"
    """)
    partial = operations.replace(
        "    ledger.record(key, payload)",
        '    if ledger.previous(key) is not None:\n        return "duplicate"\n    ledger.record(key, payload)',
    )
    repaired = source("""
        def execute(ledger, command):
            account = command["tenant"] + "/" + command["account"]
            key = (command["tenant"], command["id"])
            payload = (account, command["amount"])
            previous = ledger.previous(key)
            if previous is not None:
                return "duplicate" if previous == payload else "conflict"
            if not ledger.change(account, command["amount"]):
                return "rejected"
            ledger.record(key, payload)
            return "applied"
    """)

    def command(identifier, amount, tenant="a", account="cash"):
        return {"id": identifier, "amount": amount, "tenant": tenant, "account": account}

    amount = seed + 3
    return Family(
        "idempotent-commands",
        "learning",
        "account-command-log-v1",
        contract,
        {"app.py": entry, "ledger.py": ledger, "operations.py": operations},
        {"operations.py": repaired},
        {"operations.py": partial},
        [
            case(
                "public-retry",
                {"balances": {}, "commands": [command("credit", amount)] * 2},
                {"balances": {"a/cash": amount}, "results": ["applied", "duplicate"]},
            ),
            case(
                "public-decline",
                {"balances": {}, "commands": [command("debit", -amount)]},
                {"balances": {}, "results": ["rejected"]},
            ),
        ],
        [
            case(
                "private-tenant",
                {
                    "balances": {},
                    "commands": [command("same", amount), command("same", amount, tenant="b")],
                },
                {
                    "balances": {"a/cash": amount, "b/cash": amount},
                    "results": ["applied", "applied"],
                },
            ),
            case(
                "private-retry-after-credit",
                {
                    "balances": {},
                    "commands": [
                        command("debit", -2),
                        command("credit", amount),
                        command("debit", -2),
                    ],
                },
                {"balances": {"a/cash": amount - 2}, "results": ["rejected", "applied", "applied"]},
            ),
            case(
                "private-conflicting-payload",
                {"balances": {}, "commands": [command("same", amount), command("same", 1)]},
                {"balances": {"a/cash": amount}, "results": ["applied", "conflict"]},
            ),
            case(
                "private-conflicting-account",
                {
                    "balances": {},
                    "commands": [
                        command("same", amount),
                        command("same", amount, account="reserve"),
                    ],
                },
                {"balances": {"a/cash": amount}, "results": ["applied", "conflict"]},
            ),
        ],
        mechanism_cluster="tenant-idempotency-ledger",
    )
