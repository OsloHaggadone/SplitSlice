"""Entry point. Run with: python main.py (from this directory)."""

from agents.orchestrator import Orchestrator
from models import RetrievalMode
from providers.dominos import DominosProvider


def main():
    provider = DominosProvider()

    # --- Replace these placeholders with real details ---
    customer_info = {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane.doe@example.com",
        "phone": "5555555555",
    }
    store, address_obj = provider.get_store("351A Western Dr", "Santa Cruz", "CA", "95060")
    print(f"Nearest store: {store}")

    orchestrator = Orchestrator(provider)
    orchestrator.run(
        user_id="jane_doe",
        store=store,
        customer_info=customer_info,
        address_obj=address_obj,
        mode=RetrievalMode.STANDARD,  # the only mode implemented so far
    )


if __name__ == "__main__":
    main()