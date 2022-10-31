"""Ledger configuration."""

from collections import OrderedDict
import logging
import re
import sys
from typing import Optional
import uuid

import markdown
import prompt_toolkit
from prompt_toolkit.eventloop.defaults import use_asyncio_event_loop
from prompt_toolkit.formatted_text import HTML

from ..config.settings import Settings
from ..core.profile import Profile
from ..ledger.base import BaseLedger
from ..ledger.endpoint_type import EndpointType
from ..ledger.error import LedgerError
from ..utils.http import fetch, FetchError
from ..wallet.base import BaseWallet

from .base import ConfigError

LOGGER = logging.getLogger(__name__)


async def fetch_genesis_transactions(genesis_url: str) -> str:
    """Get genesis transactions."""
    headers = {}
    headers["Content-Type"] = "application/json"
    LOGGER.info("Fetching genesis transactions from: %s", genesis_url)
    try:
        # Fetch from --genesis-url likely to fail in composed container setup
        # https://github.com/hyperledger/aries-cloudagent-python/issues/1745
        return await fetch(genesis_url, headers=headers, max_attempts=20)
    except FetchError as e:
        raise ConfigError("Error retrieving ledger genesis transactions") from e


async def get_genesis_transactions(settings: Settings) -> str:
    """Fetch genesis transactions if necessary."""

    txns = settings.get("ledger.genesis_transactions")
    if not txns:
        if settings.get("ledger.genesis_url"):
            txns = await fetch_genesis_transactions(settings["ledger.genesis_url"])
        elif settings.get("ledger.genesis_file"):
            try:
                genesis_path = settings["ledger.genesis_file"]
                LOGGER.info(
                    "Reading ledger genesis transactions from: %s", genesis_path
                )
                with open(genesis_path, "r") as genesis_file:
                    txns = genesis_file.read()
            except IOError as e:
                raise ConfigError("Error reading ledger genesis transactions") from e
        if txns:
            settings["ledger.genesis_transactions"] = txns
    return txns


async def load_multiple_genesis_transactions_from_config(settings: Settings):
    """Fetch genesis transactions for multiple ledger configuration."""

    ledger_config_list = settings.get("ledger.ledger_config_list")
    ledger_txns_list = []
    write_ledger_set = False
    for config in ledger_config_list:
        txns = None
        if "genesis_transactions" in config:
            txns = config.get("genesis_transactions")
        if not txns:
            if "genesis_url" in config:
                txns = await fetch_genesis_transactions(config.get("genesis_url"))
            elif "genesis_file" in config:
                try:
                    genesis_path = config.get("genesis_file")
                    LOGGER.info(
                        "Reading ledger genesis transactions from: %s", genesis_path
                    )
                    with open(genesis_path, "r") as genesis_file:
                        txns = genesis_file.read()
                except IOError as e:
                    raise ConfigError(
                        "Error reading ledger genesis transactions"
                    ) from e
        is_write_ledger = (
            False if config.get("is_write") is None else config.get("is_write")
        )
        ledger_id = config.get("id") or str(uuid.uuid4())
        if is_write_ledger and write_ledger_set:
            raise ConfigError("Only a single ledger can be is_write")
        elif is_write_ledger:
            write_ledger_set = True
        ledger_txns_list.append(
            {
                "id": ledger_id,
                "is_production": (
                    True
                    if config.get("is_production") is None
                    else config.get("is_production")
                ),
                "is_write": is_write_ledger,
                "genesis_transactions": txns,
                "keepalive": int(config.get("keepalive", 5)),
                "read_only": bool(config.get("read_only", False)),
                "socks_proxy": config.get("socks_proxy"),
                "pool_name": config.get("pool_name", ledger_id),
            }
        )
    if not write_ledger_set and not (
        settings.get("ledger.genesis_transactions")
        or settings.get("ledger.genesis_file")
        or settings.get("ledger.genesis_url")
    ):
        raise ConfigError(
            "No is_write ledger set and no genesis_url,"
            " genesis_file and genesis_transactions provided."
        )
    settings["ledger.ledger_config_list"] = ledger_txns_list


async def ledger_config(
    profile: Profile, public_did: str, provision: bool = False
) -> bool:
    """Perform Indy ledger configuration."""

    session = await profile.session()

    ledger = session.inject_or(BaseLedger)
    if not ledger:
        LOGGER.info("Ledger instance not provided")
        return False

    async with ledger:
        # Check transaction author agreement acceptance
        if not ledger.read_only:
            taa_info = await ledger.get_txn_author_agreement()
            if taa_info["taa_required"] and public_did:
                taa_accepted = await ledger.get_latest_txn_author_acceptance()
                if (
                    not taa_accepted
                    or taa_info["taa_record"]["digest"] != taa_accepted["digest"]
                ):
                    if not await accept_taa(ledger, profile, taa_info, provision):
                        return False

        # Publish endpoints if necessary - skipped if TAA is required but not accepted
        endpoint = session.settings.get("default_endpoint")
        if public_did:
            wallet = session.inject(BaseWallet)
            try:
                await wallet.set_did_endpoint(public_did, endpoint, ledger)
            except LedgerError as x_ledger:
                raise ConfigError(x_ledger.message) from x_ledger  # e.g., read-only

            # Publish profile endpoint if ledger is NOT read-only
            profile_endpoint = session.settings.get("profile_endpoint")
            if profile_endpoint and not ledger.read_only:
                await ledger.update_endpoint_for_did(
                    public_did, profile_endpoint, EndpointType.PROFILE
                )

    return True


async def select_aml_tty(taa_info, provision: bool = False) -> Optional[str]:
    """Select acceptance mechanism from AML."""
    mechanisms = taa_info["aml_record"]["aml"]
    allow_opts = OrderedDict(
        [
            (
                "wallet_agreement",
                (
                    "Accept the transaction author agreement and store the "
                    "acceptance in the wallet"
                ),
            ),
            (
                "on_file",
                (
                    "Acceptance of the transaction author agreement is on file "
                    "in my organization"
                ),
            ),
        ]
    )
    if not provision:
        allow_opts["for_session"] = (
            "Accept the transaction author agreement for the duration of "
            "the current session"
        )

    found = []
    for opt in allow_opts:
        if opt in mechanisms:
            found.append(opt)

    md = markdown.Markdown()
    taa_html = md.convert(taa_info["taa_record"]["text"])
    taa_html = re.sub(
        r"<h[1-6]>(.*?)</h[1-6]>", r"<p><strong>\1</strong></p>\n", taa_html
    )
    taa_html = re.sub(r"<li>(.*?)</li>", r" - \1", taa_html)

    taa_html = (
        "\n<strong>Transaction Author Agreement version "
        + taa_info["taa_record"]["version"]
        + "</strong>\n\n"
        + taa_html
    )

    # setup for prompt_toolkit
    use_asyncio_event_loop()

    prompt_toolkit.print_formatted_text(HTML(taa_html))

    opts = []
    num_mechanisms = {}
    for idx, opt in enumerate(found):
        num_mechanisms[str(idx + 1)] = opt
        opts.append(f" {idx+1}. {allow_opts[opt]}")
    opts.append(" X. Skip the transaction author agreement")
    opts_text = "\nPlease select an option:\n" + "\n".join(opts) + "\n[1]> "

    while True:
        try:
            opt = await prompt_toolkit.prompt(opts_text, async_=True)
        except EOFError:
            return None
        if not opt:
            opt = "1"
        opt = opt.strip()
        if opt in ("x", "X"):
            return None
        if opt in num_mechanisms:
            mechanism = num_mechanisms[opt]
            break

    return mechanism


async def accept_taa(
    ledger: BaseLedger,
    profile: Profile,
    taa_info,
    provision: bool = False,
) -> bool:
    """Perform TAA acceptance."""

    mechanisms = taa_info["aml_record"]["aml"]
    mechanism = None

    taa_acceptance_mechanism = profile.settings.get("ledger.taa_acceptance_mechanism")
    taa_acceptance_version = profile.settings.get("ledger.taa_acceptance_version")

    # If configured, accept the TAA automatically
    if taa_acceptance_mechanism:
        taa_record_version = taa_info["taa_record"]["version"]
        if taa_acceptance_version != taa_record_version:
            raise LedgerError(
                f"TAA version ({taa_record_version}) is different from TAA accept "
                f"version ({taa_acceptance_version}) from configuration. Update the "
                "TAA version in the config to accept the TAA."
            )

        if taa_acceptance_mechanism not in mechanisms:
            raise LedgerError(
                f"TAA acceptance mechanism '{taa_acceptance_mechanism}' is not a "
                "valid acceptance mechanism. Valid mechanisms are: "
                + str(list(mechanisms.keys()))
            )

        mechanism = taa_acceptance_mechanism
    # If tty is available use it (allows to accept newer TAA than configured)
    elif sys.stdout.isatty():
        mechanism = await select_aml_tty(taa_info, provision)
    else:
        LOGGER.warning(
            "Cannot accept TAA without interactive terminal or taa accept config"
        )

    if not mechanism:
        return False

    LOGGER.debug(f"Accepting the TAA using mechanism '{mechanism}'")
    await ledger.accept_txn_author_agreement(taa_info["taa_record"], mechanism)
    return True
