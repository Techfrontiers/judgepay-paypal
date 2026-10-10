"""JudgePay PayPal Edition — conditional escrow where an AI judge decides
when money moves. PayPal rails version of the original JudgePay (USDC/Base)."""

from .paypal_client import PayPalClient
from .store import TaskStore
from .judge import AIJudge
from .escrow import EscrowService

__all__ = ["PayPalClient", "TaskStore", "AIJudge", "EscrowService"]
__version__ = "1.0.0"
