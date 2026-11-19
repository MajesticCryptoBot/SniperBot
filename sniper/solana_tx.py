from __future__ import annotations

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

from sniper.errors import UserFacingError


def build_sol_transfer(
    secret_key: bytes,
    destination: str,
    lamports: int,
    blockhash: str,
    priority_fee_lamports: int = 0,
) -> bytes:
    if lamports <= 0:
        raise UserFacingError("Withdrawal amount must be greater than zero.")
    try:
        keypair = Keypair.from_bytes(secret_key)
        dest = Pubkey.from_string(destination)
        recent = Hash.from_string(blockhash)
    except UserFacingError:
        raise
    except Exception as exc:
        raise UserFacingError("The withdrawal transaction could not be prepared. Verify the destination address and try again.") from exc
    if dest == keypair.pubkey():
        raise UserFacingError("The destination is the connected wallet itself. No withdrawal was created.")
    instructions = []
    if priority_fee_lamports > 0:
        limit = 1_000
        micro = int(priority_fee_lamports) * 1_000_000 // limit
        instructions.append(set_compute_unit_limit(limit))
        instructions.append(set_compute_unit_price(micro))
    instructions.append(
        transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=dest, lamports=int(lamports)))
    )
    message = Message.new_with_blockhash(instructions, keypair.pubkey(), recent)
    tx = Transaction.new_unsigned(message)
    tx.sign([keypair], recent)
    return bytes(tx)
