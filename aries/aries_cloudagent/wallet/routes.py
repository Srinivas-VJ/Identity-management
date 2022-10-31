"""Wallet admin routes."""

import json
import logging
from typing import List

from aiohttp import web
from aiohttp_apispec import docs, querystring_schema, request_schema, response_schema
from marshmallow import fields, validate

from ..admin.request_context import AdminRequestContext
from ..connections.models.conn_record import ConnRecord
from ..core.event_bus import Event, EventBus
from ..core.profile import Profile
from ..ledger.base import BaseLedger
from ..ledger.endpoint_type import EndpointType
from ..ledger.error import LedgerConfigError, LedgerError
from ..messaging.models.base import BaseModelError
from ..messaging.models.openapi import OpenAPISchema
from ..messaging.responder import BaseResponder
from ..messaging.valid import (
    DID_POSTURE,
    ENDPOINT,
    ENDPOINT_TYPE,
    INDY_DID,
    INDY_OR_KEY_DID,
    INDY_RAW_PUBLIC_KEY,
)
from ..protocols.coordinate_mediation.v1_0.route_manager import RouteManager
from ..protocols.endorse_transaction.v1_0.manager import (
    TransactionManager,
    TransactionManagerError,
)
from ..protocols.endorse_transaction.v1_0.util import (
    get_endorser_connection_id,
    is_author_role,
)
from ..storage.error import StorageError, StorageNotFoundError
from .base import BaseWallet
from .did_info import DIDInfo
from .did_method import SOV, KEY, DIDMethod, DIDMethods
from .did_posture import DIDPosture
from .error import WalletError, WalletNotFoundError
from .key_type import BLS12381G2, ED25519, KeyTypes
from .util import EVENT_LISTENER_PATTERN

LOGGER = logging.getLogger(__name__)


class WalletModuleResponseSchema(OpenAPISchema):
    """Response schema for Wallet Module."""


class DIDSchema(OpenAPISchema):
    """Result schema for a DID."""

    did = fields.Str(description="DID of interest", **INDY_OR_KEY_DID)
    verkey = fields.Str(description="Public verification key", **INDY_RAW_PUBLIC_KEY)
    posture = fields.Str(
        description=(
            "Whether DID is current public DID, "
            "posted to ledger but not current public DID, "
            "or local to the wallet"
        ),
        **DID_POSTURE,
    )
    method = fields.Str(
        description="Did method associated with the DID",
        example=SOV.method_name,
        validate=validate.OneOf(
            [method.method_name for method in [SOV, KEY]]
        ),  # TODO: support more methods
    )
    key_type = fields.Str(
        description="Key type associated with the DID",
        example=ED25519.key_type,
        validate=validate.OneOf([ED25519.key_type, BLS12381G2.key_type]),
    )


class DIDResultSchema(OpenAPISchema):
    """Result schema for a DID."""

    result = fields.Nested(DIDSchema())


class DIDListSchema(OpenAPISchema):
    """Result schema for connection list."""

    results = fields.List(fields.Nested(DIDSchema()), description="DID list")


class DIDEndpointWithTypeSchema(OpenAPISchema):
    """Request schema to set DID endpoint of particular type."""

    did = fields.Str(description="DID of interest", required=True, **INDY_DID)
    endpoint = fields.Str(
        description="Endpoint to set (omit to delete)", required=False, **ENDPOINT
    )
    endpoint_type = fields.Str(
        description=(
            f"Endpoint type to set (default '{EndpointType.ENDPOINT.w3c}'); "
            "affects only public or posted DIDs"
        ),
        required=False,
        **ENDPOINT_TYPE,
    )


class DIDEndpointSchema(OpenAPISchema):
    """Request schema to set DID endpoint; response schema to get DID endpoint."""

    did = fields.Str(description="DID of interest", required=True, **INDY_DID)
    endpoint = fields.Str(
        description="Endpoint to set (omit to delete)", required=False, **ENDPOINT
    )


class DIDListQueryStringSchema(OpenAPISchema):
    """Parameters and validators for DID list request query string."""

    did = fields.Str(description="DID of interest", required=False, **INDY_OR_KEY_DID)
    verkey = fields.Str(
        description="Verification key of interest",
        required=False,
        **INDY_RAW_PUBLIC_KEY,
    )
    posture = fields.Str(
        description=(
            "Whether DID is current public DID, "
            "posted to ledger but current public DID, "
            "or local to the wallet"
        ),
        required=False,
        **DID_POSTURE,
    )
    method = fields.Str(
        required=False,
        example=KEY.method_name,
        validate=validate.OneOf([KEY.method_name, SOV.method_name]),
        description="DID method to query for. e.g. sov to only fetch indy/sov DIDs",
    )
    key_type = fields.Str(
        required=False,
        example=ED25519.key_type,
        validate=validate.OneOf([ED25519.key_type, BLS12381G2.key_type]),
        description="Key type to query for.",
    )


class DIDQueryStringSchema(OpenAPISchema):
    """Parameters and validators for set public DID request query string."""

    did = fields.Str(description="DID of interest", required=True, **INDY_DID)


class DIDCreateOptionsSchema(OpenAPISchema):
    """Parameters and validators for create DID options."""

    key_type = fields.Str(
        required=True,
        example=ED25519.key_type,
        validate=validate.OneOf([ED25519.key_type, BLS12381G2.key_type]),
    )


class DIDCreateSchema(OpenAPISchema):
    """Parameters and validators for create DID endpoint."""

    method = fields.Str(
        required=False,
        default=SOV.method_name,
        example=SOV.method_name,
        validate=validate.OneOf([KEY.method_name, SOV.method_name]),
    )

    options = fields.Nested(
        DIDCreateOptionsSchema,
        required=False,
        description="To define a key type for a did:key",
    )

    seed = fields.Str(
        required=False,
        description=(
            "Optional seed to use for DID, Must be"
            "enabled in configuration before use."
        ),
        example="000000000000000000000000Trustee1",
    )


class CreateAttribTxnForEndorserOptionSchema(OpenAPISchema):
    """Class for user to input whether to create a transaction for endorser or not."""

    create_transaction_for_endorser = fields.Boolean(
        description="Create Transaction For Endorser's signature",
        required=False,
    )


class AttribConnIdMatchInfoSchema(OpenAPISchema):
    """Path parameters and validators for request taking connection id."""

    conn_id = fields.Str(description="Connection identifier", required=False)


class MediationIDSchema(OpenAPISchema):
    """Class for user to optionally input a mediation_id."""

    mediation_id = fields.Str(description="Mediation identifier", required=False)


def format_did_info(info: DIDInfo):
    """Serialize a DIDInfo object."""
    if info:
        return {
            "did": info.did,
            "verkey": info.verkey,
            "posture": DIDPosture.get(info.metadata).moniker,
            "key_type": info.key_type.key_type,
            "method": info.method.method_name,
        }


@docs(tags=["wallet"], summary="List wallet DIDs")
@querystring_schema(DIDListQueryStringSchema())
@response_schema(DIDListSchema, 200, description="")
async def wallet_did_list(request: web.BaseRequest):
    """
    Request handler for searching wallet DIDs.

    Args:
        request: aiohttp request object

    Returns:
        The DID list response

    """
    context: AdminRequestContext = request["context"]
    filter_did = request.query.get("did")
    filter_verkey = request.query.get("verkey")
    filter_posture = DIDPosture.get(request.query.get("posture"))
    results = []
    async with context.session() as session:
        did_methods: DIDMethods = session.inject(DIDMethods)
        filter_method: DIDMethod | None = did_methods.from_method(
            request.query.get("method")
        )
        key_types = session.inject(KeyTypes)
        filter_key_type = key_types.from_key_type(request.query.get("key_type", ""))
        wallet: BaseWallet | None = session.inject_or(BaseWallet)
        if not wallet:
            raise web.HTTPForbidden(reason="No wallet available")
        if filter_posture is DIDPosture.PUBLIC:
            public_did_info = await wallet.get_public_did()
            if (
                public_did_info
                and (not filter_verkey or public_did_info.verkey == filter_verkey)
                and (not filter_did or public_did_info.did == filter_did)
                and (not filter_method or public_did_info.method == filter_method)
                and (not filter_key_type or public_did_info.key_type == filter_key_type)
            ):
                results.append(format_did_info(public_did_info))
        elif filter_posture is DIDPosture.POSTED:
            results = []
            posted_did_infos = await wallet.get_posted_dids()
            for info in posted_did_infos:
                if (
                    (not filter_verkey or info.verkey == filter_verkey)
                    and (not filter_did or info.did == filter_did)
                    and (not filter_method or info.method == filter_method)
                    and (not filter_key_type or info.key_type == filter_key_type)
                ):
                    results.append(format_did_info(info))
        elif filter_did:
            try:
                info = await wallet.get_local_did(filter_did)
            except WalletError:
                # badly formatted DID or record not found
                info = None
            if (
                info
                and (not filter_verkey or info.verkey == filter_verkey)
                and (not filter_method or info.method == filter_method)
                and (not filter_key_type or info.key_type == filter_key_type)
                and (
                    filter_posture is None
                    or (
                        filter_posture is DIDPosture.WALLET_ONLY
                        and not info.metadata.get("posted")
                    )
                )
            ):
                results.append(format_did_info(info))
        elif filter_verkey:
            try:
                info = await wallet.get_local_did_for_verkey(filter_verkey)
            except WalletError:
                info = None
            if (
                info
                and (not filter_method or info.method == filter_method)
                and (not filter_key_type or info.key_type == filter_key_type)
                and (
                    filter_posture is None
                    or (
                        filter_posture is DID_POSTURE.WALLET_ONLY
                        and not info.metadata.get("posted")
                    )
                )
            ):
                results.append(format_did_info(info))
        else:
            dids = await wallet.get_local_dids()
            results = [
                format_did_info(info)
                for info in dids
                if (
                    filter_posture is None
                    or DIDPosture.get(info.metadata) is DIDPosture.WALLET_ONLY
                )
                and (not filter_method or info.method == filter_method)
                and (not filter_key_type or info.key_type == filter_key_type)
            ]

    results.sort(
        key=lambda info: (DIDPosture.get(info["posture"]).ordinal, info["did"])
    )

    return web.json_response({"results": results})


@docs(tags=["wallet"], summary="Create a local DID")
@request_schema(DIDCreateSchema())
@response_schema(DIDResultSchema, 200, description="")
async def wallet_create_did(request: web.BaseRequest):
    """
    Request handler for creating a new local DID in the wallet.

    Args:
        request: aiohttp request object

    Returns:
        The DID info

    """
    context: AdminRequestContext = request["context"]

    try:
        body = await request.json()
    except Exception:
        body = {}

    # set default method and key type for backwards compat

    seed = body.get("seed") or None
    if seed and not context.settings.get("wallet.allow_insecure_seed"):
        raise web.HTTPBadRequest(reason="Seed support is not enabled")
    info = None
    async with context.session() as session:
        did_methods = session.inject(DIDMethods)
        method = did_methods.from_method(body.get("method", "")) or SOV
        key_types = session.inject(KeyTypes)
        # set default method and key type for backwards compat
        key_type = (
            key_types.from_key_type(body.get("options", {}).get("key_type", ""))
            or ED25519
        )
        if not method.supports_key_type(key_type):
            raise web.HTTPForbidden(
                reason=(
                    f"method {method.method_name} does not"
                    f" support key type {key_type.key_type}"
                )
            )
        wallet = session.inject_or(BaseWallet)
        if not wallet:
            raise web.HTTPForbidden(reason="No wallet available")
        try:
            info = await wallet.create_local_did(
                method=method,
                key_type=key_type,
                seed=seed,
            )

        except WalletError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response({"result": format_did_info(info)})


@docs(tags=["wallet"], summary="Fetch the current public DID")
@response_schema(DIDResultSchema, 200, description="")
async def wallet_get_public_did(request: web.BaseRequest):
    """
    Request handler for fetching the current public DID.

    Args:
        request: aiohttp request object

    Returns:
        The DID info

    """
    context: AdminRequestContext = request["context"]
    info = None
    async with context.session() as session:
        wallet = session.inject_or(BaseWallet)
        if not wallet:
            raise web.HTTPForbidden(reason="No wallet available")
        try:
            info = await wallet.get_public_did()
        except WalletError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response({"result": format_did_info(info)})


@docs(tags=["wallet"], summary="Assign the current public DID")
@querystring_schema(DIDQueryStringSchema())
@querystring_schema(CreateAttribTxnForEndorserOptionSchema())
@querystring_schema(AttribConnIdMatchInfoSchema())
@querystring_schema(MediationIDSchema())
@response_schema(DIDResultSchema, 200, description="")
async def wallet_set_public_did(request: web.BaseRequest):
    """
    Request handler for setting the current public DID.

    Args:
        request: aiohttp request object

    Returns:
        The updated DID info

    """
    context: AdminRequestContext = request["context"]
    session = await context.session()

    outbound_handler = request["outbound_message_router"]

    create_transaction_for_endorser = json.loads(
        request.query.get("create_transaction_for_endorser", "false")
    )
    write_ledger = not create_transaction_for_endorser
    connection_id = request.query.get("conn_id")
    attrib_def = None

    # check if we need to endorse
    if is_author_role(context.profile):
        # authors cannot write to the ledger
        write_ledger = False
        create_transaction_for_endorser = True
        if not connection_id:
            # author has not provided a connection id, so determine which to use
            connection_id = await get_endorser_connection_id(context.profile)
            if not connection_id:
                raise web.HTTPBadRequest(reason="No endorser connection found")

    wallet = session.inject_or(BaseWallet)
    if not wallet:
        raise web.HTTPForbidden(reason="No wallet available")
    did = request.query.get("did")
    if not did:
        raise web.HTTPBadRequest(reason="Request query must include DID")

    info: DIDInfo = None

    mediation_id = request.query.get("mediation_id")
    profile = context.profile
    route_manager = profile.inject(RouteManager)
    mediation_record = await route_manager.mediation_record_if_id(
        profile=profile, mediation_id=mediation_id, or_default=True
    )
    routing_keys = None
    mediator_endpoint = None
    if mediation_record:
        routing_keys = mediation_record.routing_keys
        mediator_endpoint = mediation_record.endpoint

    try:
        info, attrib_def = await promote_wallet_public_did(
            context.profile,
            context,
            context.session,
            did,
            write_ledger=write_ledger,
            connection_id=connection_id,
            routing_keys=routing_keys,
            mediator_endpoint=mediator_endpoint,
        )
    except LookupError as err:
        raise web.HTTPNotFound(reason=str(err)) from err
    except PermissionError as err:
        raise web.HTTPForbidden(reason=str(err)) from err
    except WalletNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except (LedgerError, WalletError) as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    if not create_transaction_for_endorser:
        return web.json_response({"result": format_did_info(info)})

    else:
        transaction_mgr = TransactionManager(context.profile)
        try:
            transaction = await transaction_mgr.create_record(
                messages_attach=attrib_def["signed_txn"], connection_id=connection_id
            )
        except StorageError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

        # if auto-request, send the request to the endorser
        if context.settings.get_value("endorser.auto_request"):
            try:
                transaction, transaction_request = await transaction_mgr.create_request(
                    transaction=transaction,
                    # TODO see if we need to parameterize these params
                    # expires_time=expires_time,
                    # endorser_write_txn=endorser_write_txn,
                )
            except (StorageError, TransactionManagerError) as err:
                raise web.HTTPBadRequest(reason=err.roll_up) from err

            await outbound_handler(transaction_request, connection_id=connection_id)

        return web.json_response({"txn": transaction.serialize()})


async def promote_wallet_public_did(
    profile: Profile,
    context: AdminRequestContext,
    session_fn,
    did: str,
    write_ledger: bool = False,
    connection_id: str = None,
    routing_keys: List[str] = None,
    mediator_endpoint: str = None,
) -> DIDInfo:
    """Promote supplied DID to the wallet public DID."""
    info: DIDInfo = None
    endorser_did = None
    ledger = profile.inject_or(BaseLedger)
    if not ledger:
        reason = "No ledger available"
        if not context.settings.get_value("wallet.type"):
            reason += ": missing wallet-type?"
        raise PermissionError(reason)

    async with ledger:
        if not await ledger.get_key_for_did(did):
            raise LookupError(f"DID {did} is not posted to the ledger")

    # check if we need to endorse
    if is_author_role(profile):
        # authors cannot write to the ledger
        write_ledger = False

        # author has not provided a connection id, so determine which to use
        if not connection_id:
            connection_id = await get_endorser_connection_id(profile)
        if not connection_id:
            raise web.HTTPBadRequest(reason="No endorser connection found")
    if not write_ledger:
        try:
            async with profile.session() as session:
                connection_record = await ConnRecord.retrieve_by_id(
                    session, connection_id
                )
        except StorageNotFoundError as err:
            raise web.HTTPNotFound(reason=err.roll_up) from err
        except BaseModelError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

        async with profile.session() as session:
            endorser_info = await connection_record.metadata_get(
                session, "endorser_info"
            )
        if not endorser_info:
            raise web.HTTPForbidden(
                reason="Endorser Info is not set up in "
                "connection metadata for this connection record"
            )
        if "endorser_did" not in endorser_info.keys():
            raise web.HTTPForbidden(
                reason=' "endorser_did" is not set in "endorser_info"'
                " in connection metadata for this connection record"
            )
        endorser_did = endorser_info["endorser_did"]

    did_info: DIDInfo = None
    attrib_def = None
    async with session_fn() as session:
        wallet = session.inject_or(BaseWallet)
        did_info = await wallet.get_local_did(did)
        info = await wallet.set_public_did(did_info)

    if info:
        # Publish endpoint if necessary
        endpoint = did_info.metadata.get("endpoint")

        if not endpoint:
            async with session_fn() as session:
                wallet = session.inject_or(BaseWallet)
                endpoint = mediator_endpoint or context.settings.get("default_endpoint")
                attrib_def = await wallet.set_did_endpoint(
                    info.did,
                    endpoint,
                    ledger,
                    write_ledger=write_ledger,
                    endorser_did=endorser_did,
                    routing_keys=routing_keys,
                )

        # Route the public DID
        route_manager = profile.inject(RouteManager)
        await route_manager.route_public_did(profile, info.verkey)

    return info, attrib_def


@docs(
    tags=["wallet"], summary="Update endpoint in wallet and on ledger if posted to it"
)
@request_schema(DIDEndpointWithTypeSchema)
@querystring_schema(CreateAttribTxnForEndorserOptionSchema())
@querystring_schema(AttribConnIdMatchInfoSchema())
@response_schema(WalletModuleResponseSchema(), description="")
async def wallet_set_did_endpoint(request: web.BaseRequest):
    """
    Request handler for setting an endpoint for a DID.

    Args:
        request: aiohttp request object
    """
    context: AdminRequestContext = request["context"]

    outbound_handler = request["outbound_message_router"]

    body = await request.json()
    did = body["did"]
    endpoint = body.get("endpoint")
    endpoint_type = EndpointType.get(
        body.get("endpoint_type", EndpointType.ENDPOINT.w3c)
    )

    create_transaction_for_endorser = json.loads(
        request.query.get("create_transaction_for_endorser", "false")
    )
    write_ledger = not create_transaction_for_endorser
    endorser_did = None
    connection_id = request.query.get("conn_id")
    attrib_def = None

    # check if we need to endorse
    if is_author_role(context.profile):
        # authors cannot write to the ledger
        write_ledger = False
        create_transaction_for_endorser = True
        if not connection_id:
            # author has not provided a connection id, so determine which to use
            connection_id = await get_endorser_connection_id(context.profile)
            if not connection_id:
                raise web.HTTPBadRequest(reason="No endorser connection found")

    if not write_ledger:
        try:
            async with context.session() as session:
                connection_record = await ConnRecord.retrieve_by_id(
                    session, connection_id
                )
        except StorageNotFoundError as err:
            raise web.HTTPNotFound(reason=err.roll_up) from err
        except BaseModelError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

        async with context.session() as session:
            endorser_info = await connection_record.metadata_get(
                session, "endorser_info"
            )
        if not endorser_info:
            raise web.HTTPForbidden(
                reason="Endorser Info is not set up in "
                "connection metadata for this connection record"
            )
        if "endorser_did" not in endorser_info.keys():
            raise web.HTTPForbidden(
                reason=' "endorser_did" is not set in "endorser_info"'
                " in connection metadata for this connection record"
            )
        endorser_did = endorser_info["endorser_did"]

    async with context.session() as session:
        wallet = session.inject_or(BaseWallet)
        if not wallet:
            raise web.HTTPForbidden(reason="No wallet available")
        try:
            ledger = context.profile.inject_or(BaseLedger)
            attrib_def = await wallet.set_did_endpoint(
                did,
                endpoint,
                ledger,
                endpoint_type,
                write_ledger=write_ledger,
                endorser_did=endorser_did,
            )
        except WalletNotFoundError as err:
            raise web.HTTPNotFound(reason=err.roll_up) from err
        except LedgerConfigError as err:
            raise web.HTTPForbidden(reason=err.roll_up) from err
        except (LedgerError, WalletError) as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

    if not create_transaction_for_endorser:
        return web.json_response({})
    else:
        transaction_mgr = TransactionManager(context.profile)
        try:
            transaction = await transaction_mgr.create_record(
                messages_attach=attrib_def["signed_txn"], connection_id=connection_id
            )
        except StorageError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

        # if auto-request, send the request to the endorser
        if context.settings.get_value("endorser.auto_request"):
            try:
                transaction, transaction_request = await transaction_mgr.create_request(
                    transaction=transaction,
                    # TODO see if we need to parameterize these params
                    # expires_time=expires_time,
                    # endorser_write_txn=endorser_write_txn,
                )
            except (StorageError, TransactionManagerError) as err:
                raise web.HTTPBadRequest(reason=err.roll_up) from err

            await outbound_handler(transaction_request, connection_id=connection_id)

        return web.json_response({"txn": transaction.serialize()})


@docs(tags=["wallet"], summary="Query DID endpoint in wallet")
@querystring_schema(DIDQueryStringSchema())
@response_schema(DIDEndpointSchema, 200, description="")
async def wallet_get_did_endpoint(request: web.BaseRequest):
    """
    Request handler for getting the current DID endpoint from the wallet.

    Args:
        request: aiohttp request object

    Returns:
        The updated DID info

    """
    context: AdminRequestContext = request["context"]
    async with context.session() as session:
        wallet = session.inject_or(BaseWallet)
        if not wallet:
            raise web.HTTPForbidden(reason="No wallet available")
        did = request.query.get("did")
        if not did:
            raise web.HTTPBadRequest(reason="Request query must include DID")

        try:
            did_info = await wallet.get_local_did(did)
            endpoint = did_info.metadata.get("endpoint")
        except WalletNotFoundError as err:
            raise web.HTTPNotFound(reason=err.roll_up) from err
        except WalletError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response({"did": did, "endpoint": endpoint})


@docs(tags=["wallet"], summary="Rotate keypair for a DID not posted to the ledger")
@querystring_schema(DIDQueryStringSchema())
@response_schema(WalletModuleResponseSchema(), description="")
async def wallet_rotate_did_keypair(request: web.BaseRequest):
    """
    Request handler for rotating local DID keypair.

    Args:
        request: aiohttp request object

    Returns:
        An empty JSON response

    """
    context: AdminRequestContext = request["context"]
    did = request.query.get("did")
    if not did:
        raise web.HTTPBadRequest(reason="Request query must include DID")

    async with context.session() as session:
        wallet = session.inject_or(BaseWallet)
        if not wallet:
            raise web.HTTPForbidden(reason="No wallet available")
        try:
            did_info: DIDInfo = None
            did_info = await wallet.get_local_did(did)
            if did_info.metadata.get("posted", False):
                # call from ledger API instead to propagate through ledger NYM transaction
                raise web.HTTPBadRequest(reason=f"DID {did} is posted to the ledger")
            await wallet.rotate_did_keypair_start(did)  # do not take seed over the wire
            await wallet.rotate_did_keypair_apply(did)
        except WalletNotFoundError as err:
            raise web.HTTPNotFound(reason=err.roll_up) from err
        except WalletError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response({})


def register_events(event_bus: EventBus):
    """Subscribe to any events we need to support."""
    event_bus.subscribe(EVENT_LISTENER_PATTERN, on_register_nym_event)


async def on_register_nym_event(profile: Profile, event: Event):
    """Handle any events we need to support."""

    # after the nym record is written, promote to wallet public DID
    if is_author_role(profile) and profile.context.settings.get_value(
        "endorser.auto_promote_author_did"
    ):
        did = event.payload["did"]
        connection_id = event.payload.get("connection_id")
        try:
            info, attrib_def = await promote_wallet_public_did(
                profile, profile.context, profile.session, did, connection_id
            )
        except Exception as err:
            # log the error, but continue
            LOGGER.exception(
                "Error promoting to public DID: %s",
                err,
            )
            return

        transaction_mgr = TransactionManager(profile)
        try:
            transaction = await transaction_mgr.create_record(
                messages_attach=attrib_def["signed_txn"], connection_id=connection_id
            )
        except StorageError as err:
            # log the error, but continue
            LOGGER.exception(
                "Error accepting endorser invitation/configuring endorser connection: %s",
                err,
            )
            return

        # if auto-request, send the request to the endorser
        if profile.settings.get_value("endorser.auto_request"):
            try:
                transaction, transaction_request = await transaction_mgr.create_request(
                    transaction=transaction,
                    # TODO see if we need to parameterize these params
                    # expires_time=expires_time,
                    # endorser_write_txn=endorser_write_txn,
                )
            except (StorageError, TransactionManagerError) as err:
                # log the error, but continue
                LOGGER.exception(
                    "Error creating endorser transaction request: %s",
                    err,
                )

            # TODO not sure how to get outbound_handler in an event ...
            # await outbound_handler(transaction_request, connection_id=connection_id)
            responder = profile.inject_or(BaseResponder)
            if responder:
                await responder.send(
                    transaction_request,
                    connection_id=connection_id,
                )
            else:
                LOGGER.warning(
                    "Configuration has no BaseResponder: cannot update "
                    "ATTRIB record on DID: %s",
                    did,
                )


async def register(app: web.Application):
    """Register routes."""

    app.add_routes(
        [
            web.get("/wallet/did", wallet_did_list, allow_head=False),
            web.post("/wallet/did/create", wallet_create_did),
            web.get("/wallet/did/public", wallet_get_public_did, allow_head=False),
            web.post("/wallet/did/public", wallet_set_public_did),
            web.post("/wallet/set-did-endpoint", wallet_set_did_endpoint),
            web.get(
                "/wallet/get-did-endpoint", wallet_get_did_endpoint, allow_head=False
            ),
            web.patch("/wallet/did/local/rotate-keypair", wallet_rotate_did_keypair),
        ]
    )


def post_process_routes(app: web.Application):
    """Amend swagger API."""

    # Add top-level tags description
    if "tags" not in app._state["swagger_dict"]:
        app._state["swagger_dict"]["tags"] = []
    app._state["swagger_dict"]["tags"].append(
        {
            "name": "wallet",
            "description": "DID and tag policy management",
            "externalDocs": {
                "description": "Design",
                "url": (
                    "https://github.com/hyperledger/indy-sdk/tree/"
                    "master/docs/design/003-wallet-storage"
                ),
            },
        }
    )
