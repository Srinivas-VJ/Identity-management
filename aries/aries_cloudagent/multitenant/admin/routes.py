"""Multitenant admin routes."""

from aiohttp import web
from aiohttp_apispec import (
    docs,
    match_info_schema,
    querystring_schema,
    request_schema,
    response_schema,
)
from marshmallow import ValidationError, fields, validate, validates_schema

from ...admin.request_context import AdminRequestContext
from ...core.error import BaseError
from ...core.profile import ProfileManagerProvider
from ...messaging.models.base import BaseModelError
from ...messaging.models.openapi import OpenAPISchema
from ...messaging.valid import JSONWebToken, UUIDFour
from ...multitenant.base import BaseMultitenantManager
from ...storage.error import StorageError, StorageNotFoundError
from ...wallet.error import WalletSettingsError
from ...wallet.models.wallet_record import WalletRecord, WalletRecordSchema
from ..error import WalletKeyMissingError


def format_wallet_record(wallet_record: WalletRecord):
    """Serialize a WalletRecord object."""

    wallet_info = wallet_record.serialize()

    # Hide wallet wallet key
    if "wallet.key" in wallet_info["settings"]:
        del wallet_info["settings"]["wallet.key"]

    return wallet_info


class MultitenantModuleResponseSchema(OpenAPISchema):
    """Response schema for multitenant module."""


class WalletIdMatchInfoSchema(OpenAPISchema):
    """Path parameters and validators for request taking wallet id."""

    wallet_id = fields.Str(
        description="Subwallet identifier", required=True, example=UUIDFour.EXAMPLE
    )


class CreateWalletRequestSchema(OpenAPISchema):
    """Request schema for adding a new wallet which will be registered by the agent."""

    wallet_name = fields.Str(description="Wallet name", example="MyNewWallet")

    wallet_key = fields.Str(
        description="Master key used for key derivation.", example="MySecretKey123"
    )

    wallet_key_derivation = fields.Str(
        description="Key derivation",
        required=False,
        example="RAW",
        validate=validate.OneOf(["ARGON2I_MOD", "ARGON2I_INT", "RAW"]),
    )

    wallet_type = fields.Str(
        description="Type of the wallet to create",
        example="indy",
        default="in_memory",
        validate=validate.OneOf(
            [wallet_type for wallet_type in ProfileManagerProvider.MANAGER_TYPES]
        ),
    )

    wallet_dispatch_type = fields.Str(
        description="Webhook target dispatch type for this wallet. \
            default - Dispatch only to webhooks associated with this wallet. \
            base - Dispatch only to webhooks associated with the base wallet. \
            both - Dispatch to both webhook targets.",
        example="default",
        default="default",
        validate=validate.OneOf(["default", "both", "base"]),
    )

    wallet_webhook_urls = fields.List(
        fields.Str(
            description="Optional webhook URL to receive webhook messages",
            example="http://localhost:8022/webhooks",
        ),
        required=False,
        description="List of Webhook URLs associated with this subwallet",
    )

    label = fields.Str(
        description="Label for this wallet. This label is publicized\
            (self-attested) to other agents as part of forming a connection.",
        example="Alice",
    )

    image_url = fields.Str(
        description="Image url for this wallet. This image url is publicized\
            (self-attested) to other agents as part of forming a connection.",
        example="https://aries.ca/images/sample.png",
    )

    key_management_mode = fields.Str(
        description="Key management method to use for this wallet.",
        example=WalletRecord.MODE_MANAGED,
        default=WalletRecord.MODE_MANAGED,
        # MTODO: add unmanaged mode once implemented
        validate=validate.OneOf((WalletRecord.MODE_MANAGED,)),
    )

    @validates_schema
    def validate_fields(self, data, **kwargs):
        """
        Validate schema fields.

        Args:
            data: The data to validate

        Raises:
            ValidationError: If any of the fields do not validate

        """

        if data.get("wallet_type") == "indy":
            for field in ("wallet_key", "wallet_name"):
                if field not in data:
                    raise ValidationError("Missing required field", field)


class UpdateWalletRequestSchema(OpenAPISchema):
    """Request schema for updating a existing wallet."""

    wallet_dispatch_type = fields.Str(
        description="Webhook target dispatch type for this wallet. \
            default - Dispatch only to webhooks associated with this wallet. \
            base - Dispatch only to webhooks associated with the base wallet. \
            both - Dispatch to both webhook targets.",
        example="default",
        default="default",
        validate=validate.OneOf(["default", "both", "base"]),
    )
    wallet_webhook_urls = fields.List(
        fields.Str(
            description="Optional webhook URL to receive webhook messages",
            example="http://localhost:8022/webhooks",
        ),
        required=False,
        description="List of Webhook URLs associated with this subwallet",
    )
    label = fields.Str(
        description="Label for this wallet. This label is publicized\
            (self-attested) to other agents as part of forming a connection.",
        example="Alice",
    )
    image_url = fields.Str(
        description="Image url for this wallet. This image url is publicized\
            (self-attested) to other agents as part of forming a connection.",
        example="https://aries.ca/images/sample.png",
    )


class CreateWalletResponseSchema(WalletRecordSchema):
    """Response schema for creating a wallet."""

    token = fields.Str(
        description="Authorization token to authenticate wallet requests",
        example=JSONWebToken.EXAMPLE,
    )


class RemoveWalletRequestSchema(OpenAPISchema):
    """Request schema for removing a wallet."""

    wallet_key = fields.Str(
        description="Master key used for key derivation. Only required for \
            unmanaged wallets.",
        example="MySecretKey123",
    )


class CreateWalletTokenRequestSchema(OpenAPISchema):
    """Request schema for creating a wallet token."""

    wallet_key = fields.Str(
        description="Master key used for key derivation. Only required for \
            unamanged wallets.",
        example="MySecretKey123",
    )


class CreateWalletTokenResponseSchema(OpenAPISchema):
    """Response schema for creating a wallet token."""

    token = fields.Str(
        description="Authorization token to authenticate wallet requests",
        example=JSONWebToken.EXAMPLE,
    )


class WalletListSchema(OpenAPISchema):
    """Result schema for wallet list."""

    results = fields.List(
        fields.Nested(WalletRecordSchema()),
        description="List of wallet records",
    )


class WalletListQueryStringSchema(OpenAPISchema):
    """Parameters and validators for wallet list request query string."""

    wallet_name = fields.Str(description="Wallet name", example="MyNewWallet")


@docs(tags=["multitenancy"], summary="Query subwallets")
@querystring_schema(WalletListQueryStringSchema())
@response_schema(WalletListSchema(), 200, description="")
async def wallets_list(request: web.BaseRequest):
    """
    Request handler for listing all internal subwallets.

    Args:
        request: aiohttp request object
    """

    context: AdminRequestContext = request["context"]
    profile = context.profile

    query = {}
    wallet_name = request.query.get("wallet_name")
    if wallet_name:
        query["wallet_name"] = wallet_name

    try:
        async with profile.session() as session:
            records = await WalletRecord.query(session, tag_filter=query)
        results = [format_wallet_record(record) for record in records]
        results.sort(key=lambda w: w["created_at"])
    except (StorageError, BaseModelError) as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response({"results": results})


@docs(tags=["multitenancy"], summary="Get a single subwallet")
@match_info_schema(WalletIdMatchInfoSchema())
@response_schema(WalletRecordSchema(), 200, description="")
async def wallet_get(request: web.BaseRequest):
    """
    Request handler for getting a single subwallet.

    Args:
        request: aiohttp request object

    Raises:
        HTTPNotFound: if wallet_id does not match any known wallets

    """

    context: AdminRequestContext = request["context"]
    profile = context.profile
    wallet_id = request.match_info["wallet_id"]

    try:
        async with profile.session() as session:
            wallet_record = await WalletRecord.retrieve_by_id(session, wallet_id)
        result = format_wallet_record(wallet_record)
    except StorageNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except BaseModelError as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response(result)


@docs(tags=["multitenancy"], summary="Create a subwallet")
@request_schema(CreateWalletRequestSchema)
@response_schema(CreateWalletResponseSchema(), 200, description="")
async def wallet_create(request: web.BaseRequest):
    """
    Request handler for adding a new subwallet for handling by the agent.

    Args:
        request: aiohttp request object
    """

    context: AdminRequestContext = request["context"]
    body = await request.json()

    key_management_mode = body.get("key_management_mode") or WalletRecord.MODE_MANAGED
    wallet_key = body.get("wallet_key")
    wallet_webhook_urls = body.get("wallet_webhook_urls") or []
    wallet_dispatch_type = body.get("wallet_dispatch_type") or "default"
    # If no webhooks specified, then dispatch only to base webhook targets
    if wallet_webhook_urls == []:
        wallet_dispatch_type = "base"

    settings = {
        "wallet.type": body.get("wallet_type") or "in_memory",
        "wallet.name": body.get("wallet_name"),
        "wallet.key": wallet_key,
        "wallet.webhook_urls": wallet_webhook_urls,
        "wallet.dispatch_type": wallet_dispatch_type,
    }

    label = body.get("label")
    image_url = body.get("image_url")
    key_derivation = body.get("wallet_key_derivation")
    if label:
        settings["default_label"] = label
    if image_url:
        settings["image_url"] = image_url
    if key_derivation:  # allow lower levels to handle default
        settings["wallet.key_derivation_method"] = key_derivation

    try:
        multitenant_mgr = context.profile.inject(BaseMultitenantManager)

        wallet_record = await multitenant_mgr.create_wallet(
            settings, key_management_mode
        )

        token = await multitenant_mgr.create_auth_token(wallet_record, wallet_key)
    except BaseError as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    result = {
        **format_wallet_record(wallet_record),
        "token": token,
    }
    return web.json_response(result)


@docs(tags=["multitenancy"], summary="Update a subwallet")
@match_info_schema(WalletIdMatchInfoSchema())
@request_schema(UpdateWalletRequestSchema)
@response_schema(WalletRecordSchema(), 200, description="")
async def wallet_update(request: web.BaseRequest):
    """
    Request handler for updating a existing subwallet for handling by the agent.

    Args:
        request: aiohttp request object
    """

    context: AdminRequestContext = request["context"]
    wallet_id = request.match_info["wallet_id"]

    body = await request.json()
    wallet_webhook_urls = body.get("wallet_webhook_urls")
    wallet_dispatch_type = body.get("wallet_dispatch_type")
    label = body.get("label")
    image_url = body.get("image_url")

    if all(
        v is None for v in (wallet_webhook_urls, wallet_dispatch_type, label, image_url)
    ):
        raise web.HTTPBadRequest(reason="At least one parameter is required.")

    # adjust wallet_dispatch_type according to wallet_webhook_urls
    if wallet_webhook_urls and wallet_dispatch_type is None:
        wallet_dispatch_type = "default"
    if wallet_webhook_urls == []:
        wallet_dispatch_type = "base"

    # only parameters that are not none are updated
    settings = {}
    if wallet_webhook_urls is not None:
        settings["wallet.webhook_urls"] = wallet_webhook_urls
    if wallet_dispatch_type is not None:
        settings["wallet.dispatch_type"] = wallet_dispatch_type
    if label is not None:
        settings["default_label"] = label
    if image_url is not None:
        settings["image_url"] = image_url

    try:
        multitenant_mgr = context.profile.inject(BaseMultitenantManager)
        wallet_record = await multitenant_mgr.update_wallet(wallet_id, settings)

        result = format_wallet_record(wallet_record)
    except StorageNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except WalletSettingsError as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response(result)


@docs(tags=["multitenancy"], summary="Get auth token for a subwallet")
@request_schema(CreateWalletTokenRequestSchema)
@response_schema(CreateWalletTokenResponseSchema(), 200, description="")
async def wallet_create_token(request: web.BaseRequest):
    """
    Request handler for creating an authorization token for a specific subwallet.

    Args:
        request: aiohttp request object
    """

    context: AdminRequestContext = request["context"]
    wallet_id = request.match_info["wallet_id"]
    wallet_key = None

    if request.has_body:
        body = await request.json()
        wallet_key = body.get("wallet_key")

    profile = context.profile
    try:
        multitenant_mgr = profile.inject(BaseMultitenantManager)
        async with profile.session() as session:
            wallet_record = await WalletRecord.retrieve_by_id(session, wallet_id)

        if (not wallet_record.requires_external_key) and wallet_key:
            raise web.HTTPBadRequest(
                reason=f"Wallet {wallet_id} doesn't require"
                " the wallet key to be provided"
            )

        token = await multitenant_mgr.create_auth_token(wallet_record, wallet_key)
    except StorageNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except WalletKeyMissingError as err:
        raise web.HTTPUnauthorized(reason=err.roll_up) from err

    return web.json_response({"token": token})


@docs(
    tags=["multitenancy"],
    summary="Remove a subwallet",
)
@match_info_schema(WalletIdMatchInfoSchema())
@request_schema(RemoveWalletRequestSchema)
@response_schema(MultitenantModuleResponseSchema(), 200, description="")
async def wallet_remove(request: web.BaseRequest):
    """
    Request handler to remove a subwallet from agent and storage.

    Args:
        request: aiohttp request object.

    """

    context: AdminRequestContext = request["context"]
    wallet_id = request.match_info["wallet_id"]
    wallet_key = None

    if request.has_body:
        body = await request.json()
        wallet_key = body.get("wallet_key")

    profile = context.profile
    try:
        multitenant_mgr = profile.inject(BaseMultitenantManager)
        async with profile.session() as session:
            wallet_record = await WalletRecord.retrieve_by_id(session, wallet_id)

        if (not wallet_record.requires_external_key) and wallet_key:
            raise web.HTTPBadRequest(
                reason=f"Wallet {wallet_id} doesn't require"
                " the wallet key to be provided"
            )

        await multitenant_mgr.remove_wallet(wallet_id, wallet_key)
    except StorageNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except WalletKeyMissingError as err:
        raise web.HTTPUnauthorized(reason=err.roll_up) from err

    return web.json_response({})


# MTODO: add wallet import route
# MTODO: add wallet export route
# MTODO: add rotate wallet key route


async def register(app: web.Application):
    """Register routes."""

    app.add_routes(
        [
            web.get("/multitenancy/wallets", wallets_list, allow_head=False),
            web.post("/multitenancy/wallet", wallet_create),
            web.get("/multitenancy/wallet/{wallet_id}", wallet_get, allow_head=False),
            web.put("/multitenancy/wallet/{wallet_id}", wallet_update),
            web.post("/multitenancy/wallet/{wallet_id}/token", wallet_create_token),
            web.post("/multitenancy/wallet/{wallet_id}/remove", wallet_remove),
        ]
    )


def post_process_routes(app: web.Application):
    """Amend swagger API."""

    # Add top-level tags description
    if "tags" not in app._state["swagger_dict"]:
        app._state["swagger_dict"]["tags"] = []
    app._state["swagger_dict"]["tags"].append(
        {"name": "multitenancy", "description": "Multitenant wallet management"}
    )
