"""
Account setup — First-Run Intake per setup.md.
Generates Agent EOA, creates account, persists credentials.
Supports both interactive (local) and non-interactive (Railway/Docker) modes.

IMPORTANT: On Railway, env vars persist across restarts but dev-agent/ does not.
If env vars already have credentials (API_KEY, AGENT_PRIVATE_KEY, etc),
we restore from them instead of generating new wallets.

Multi-agent mode: Each agent has its own credentials stored in dev-agent/{agent_name}/.
Shared owner EOA is generated once and reused for all agents (whitelist + SC wallet).
"""
import os
import sys
import asyncio
from bot.api_client import MoltyAPI, APIError
from bot.credentials import (
    is_first_run, save_credentials, save_owner_intake,
    save_agent_wallet, save_owner_wallet, load_credentials,
    load_agent_wallet, load_owner_wallet, update_env_file,
    load_agent_wallet_by_name, save_agent_wallet_by_name,
    load_credentials_by_name, save_credentials_by_name, get_agent_dir,
)
from bot.web3.wallet_manager import generate_agent_wallet, generate_owner_wallet
from bot.config import ADVANCED_MODE, AGENT_NAME, OWNER_EOA, OWNER_PRIVATE_KEY
from bot.utils.logger import get_logger

log = get_logger(__name__)

# Shared owner wallet (generated once, reused for all agents)
_shared_owner_wallet = None


def _is_interactive() -> bool:
    """Check if stdin is a TTY (terminal). False on Railway/Docker."""
    return sys.stdin.isatty()


def _ask_or_env(prompt: str, env_value: str, default: str = "") -> str:
    """Read from env var first, then ask interactively, then fall back to default."""
    if env_value:
        return env_value
    if _is_interactive():
        val = input(prompt).strip()
        if val:
            return val
    if default:
        log.info("Using default: %s", default)
    return default


def _get_per_agent_env(agent_name: str, base_key: str, fallback: str = "") -> str:
    """Get per-agent env var, e.g., AGUS_API_KEY, with global fallback."""
    prefixed = f"{agent_name.upper()}_{base_key}"
    val = os.getenv(prefixed, "")
    if val:
        return val
    return os.getenv(base_key, fallback)


def _restore_shared_owner() -> dict:
    """
    Restore or generate shared owner EOA (used by all agents).
    Only one owner EOA needed for all agents' whitelist + identity.
    """
    global _shared_owner_wallet

    # Check env first
    owner_addr = os.getenv("OWNER_EOA", "")
    owner_pk = os.getenv("OWNER_PRIVATE_KEY", "")

    if owner_addr and owner_pk:
        log.info("♻️ Restoring shared Owner EOA from env: %s", owner_addr[:12] + "...")
        save_owner_wallet(owner_addr, owner_pk)
        _shared_owner_wallet = {"address": owner_addr, "privateKey": owner_pk}
        return _shared_owner_wallet

    # Check existing wallet file
    existing = load_owner_wallet()
    if existing and existing.get("address") and existing.get("privateKey"):
        log.info("♻️ Restored shared Owner EOA from file: %s", existing["address"][:12] + "...")
        _shared_owner_wallet = existing
        return existing

    # Generate new owner (ADVANCED_MODE only)
    if not ADVANCED_MODE:
        raise ValueError("OWNER_EOA required. Set OWNER_EOA env var or use ADVANCED_MODE=true")

    log.info("Generating shared Owner EOA (used for all agents)...")
    addr, pk = generate_owner_wallet()
    save_owner_wallet(addr, pk)
    update_env_file("OWNER_EOA", addr)
    update_env_file("OWNER_PRIVATE_KEY", pk)
    _shared_owner_wallet = {"address": addr, "privateKey": pk}
    log.info("Shared Owner EOA generated: %s", addr[:12] + "...")
    return _shared_owner_wallet


def _restore_agent_from_env(agent_name: str) -> dict | None:
    """
    Check if we have existing credentials in env vars for this agent.
    Multi-agent pattern: {NAME}_API_KEY, {NAME}_AGENT_WALLET_ADDRESS, etc.
    """
    api_key = _get_per_agent_env(agent_name, "API_KEY", "")
    agent_pk = _get_per_agent_env(agent_name, "AGENT_PRIVATE_KEY", "")
    agent_addr = _get_per_agent_env(agent_name, "AGENT_WALLET_ADDRESS", "")
    agent_display = _get_per_agent_env(agent_name, "AGENT_NAME", agent_name)

    if not api_key or not agent_pk:
        return None

    log.info("♻️ Restoring credentials for agent '%s' from env vars...", agent_name)

    # Save to per-agent directory
    save_agent_wallet_by_name(agent_name, agent_addr, agent_pk)

    creds = {
        "api_key": api_key,
        "agent_name": agent_display,
        "agent_wallet_address": agent_addr,
    }
    save_credentials_by_name(agent_name, creds)

    log.info("  Restored %s: addr=%s...", agent_name, agent_addr[:12] + "...")
    return creds


async def _run_agent_first_run(agent_name: str) -> dict:
    """
    First-run intake for a single agent in multi-agent mode.
    Uses shared owner EOA (already restored/generated once).
    """
    global _shared_owner_wallet

    # Ensure shared owner exists
    if _shared_owner_wallet is None:
        _shared_owner_wallet = _restore_shared_owner()

    owner_eoa = _shared_owner_wallet["address"]

    # Step 1: Get agent name (check per-agent env first)
    display_name = _get_per_agent_env(agent_name, "AGENT_NAME", agent_name)
    if len(display_name) > 50:
        display_name = display_name[:50]

    # Step 2: Generate or restore agent wallet
    agent_addr = _get_per_agent_env(agent_name, "AGENT_WALLET_ADDRESS", "")
    agent_pk = _get_per_agent_env(agent_name, "AGENT_PRIVATE_KEY", "")

    if not agent_addr or not agent_pk:
        log.info("Generating Agent EOA for '%s'...", agent_name)
        agent_addr, agent_pk = generate_agent_wallet()

    save_agent_wallet_by_name(agent_name, agent_addr, agent_pk)
    update_env_file(f"{agent_name.upper()}_AGENT_WALLET_ADDRESS", agent_addr)
    update_env_file(f"{agent_name.upper()}_AGENT_PRIVATE_KEY", agent_pk)

    # Step 3: Create account via API
    log.info("Creating account for '%s' via POST /accounts...", display_name)
    api = MoltyAPI()
    try:
        result = await api.create_account(display_name, agent_addr)
    except APIError as e:
        if e.code == "CONFLICT":
            log.warning("Wallet already registered for %s. Loading credentials.", agent_name)
            existing = load_credentials_by_name(agent_name)
            if existing:
                return existing
        raise
    finally:
        await api.close()

    api_key = result.get("apiKey", "")
    account_id = result.get("accountId", "")
    public_id = result.get("publicId", "")

    if not api_key:
        raise RuntimeError(f"No apiKey returned for agent {agent_name}!")

    log.info("✅ Account created for %s! apiKey=%s...", display_name, api_key[:15])

    creds = {
        "api_key": api_key,
        "agent_name": display_name,
        "account_id": account_id,
        "public_id": public_id,
        "agent_wallet_address": agent_addr,
        "owner_eoa": owner_eoa,
    }
    save_credentials_by_name(agent_name, creds)
    update_env_file(f"{agent_name.upper()}_API_KEY", api_key)
    update_env_file(f"{agent_name.upper()}_AGENT_NAME", display_name)

    return creds


async def ensure_account_ready(agent_name: str = None) -> dict:
    """
    Ensure account exists for the agent. Run first-run intake if needed.

    Single-agent mode (agent_name=None): uses old behavior
    Multi-agent mode (agent_name set): per-agent credentials
    """
    # Multi-agent mode: per-agent credential lookup
    if agent_name:
        # Try restore from env first
        restored = _restore_agent_from_env(agent_name)
        if restored:
            return restored

        # Try load from per-agent file
        existing = load_credentials_by_name(agent_name)
        if existing and existing.get("api_key"):
            log.info("Returning run for agent '%s': %s", agent_name, existing.get("agent_name", "unknown"))
            return existing

        # First run for this agent
        log.info("First-run for agent '%s'...", agent_name)
        return await _run_agent_first_run(agent_name)

    # Legacy single-agent mode
    if is_first_run():
        return await _run_first_run_intake_legacy()

    creds = load_credentials()
    if not creds or not creds.get("api_key"):
        log.warning("Credentials file exists but no api_key. Re-running intake.")
        return await _run_first_run_intake_legacy()

    log.info("Returning run: account=%s", creds.get("agent_name", "unknown"))
    return creds


async def _run_first_run_intake_legacy() -> dict:
    """Legacy single-agent first-run intake (original behavior)."""
    global _shared_owner_wallet

    log.info("═══ FIRST-RUN INTAKE ═══")
    if not _is_interactive():
        log.info("Non-interactive mode (Railway/Docker detected)")

    agent_name = _ask_or_env("Enter agent name (max 50 chars): ", AGENT_NAME, "MoltyAgent")
    if len(agent_name) > 50:
        agent_name = agent_name[:50]

    # Shared owner setup
    owner_info = _restore_shared_owner()
    owner_address = owner_info["address"]

    # Agent EOA
    log.info("Generating Agent EOA...")
    agent_address, agent_pk = generate_agent_wallet()
    save_agent_wallet(agent_address, agent_pk)
    update_env_file("AGENT_WALLET_ADDRESS", agent_address)
    update_env_file("AGENT_PRIVATE_KEY", agent_pk)

    # Create account
    log.info("Creating account via POST /accounts...")
    api = MoltyAPI()
    try:
        result = await api.create_account(agent_name, agent_address)
    except APIError as e:
        if e.code == "CONFLICT":
            log.warning("Wallet already registered. Loading existing credentials.")
            return load_credentials() or {}
        raise
    finally:
        await api.close()

    api_key = result.get("apiKey", "")
    account_id = result.get("accountId", "")
    public_id = result.get("publicId", "")

    if not api_key:
        raise RuntimeError("No apiKey returned from POST /accounts!")

    log.info("✅ Account created! apiKey=%s... accountId=%s", api_key[:15], account_id[:8])

    creds = {
        "api_key": api_key,
        "agent_name": agent_name,
        "account_id": account_id,
        "public_id": public_id,
        "agent_wallet_address": agent_address,
        "owner_eoa": owner_address,
    }
    save_credentials(creds)
    update_env_file("API_KEY", api_key)
    update_env_file("AGENT_NAME", agent_name)

    intake = {
        "agent_name": agent_name,
        "advanced_mode": ADVANCED_MODE,
        "owner_eoa": owner_address,
        "agent_wallet_generated": True,
        "owner_wallet_generated": True,
    }
    save_owner_intake(intake)

    from bot.utils.railway_sync import is_railway, sync_all_to_railway
    if is_railway():
        log.info("Detected Railway — syncing all variables...")
        await sync_all_to_railway(creds, agent_pk, owner_info["privateKey"])

    return creds


def get_shared_owner_eoa() -> str:
    """Return the shared owner EOA (call after ensure_account_ready)."""
    global _shared_owner_wallet
    if _shared_owner_wallet:
        return _shared_owner_wallet["address"]
    return os.getenv("OWNER_EOA", "")


def get_multi_agent_names() -> list[str]:
    """Parse AGENT_NAMES env var (comma-separated) into list."""
    from bot.config import AGENT_NAMES
    if not AGENT_NAMES:
        return ["agent-1", "agent-2"]
    return [n.strip() for n in AGENT_NAMES.split(",") if n.strip()]
