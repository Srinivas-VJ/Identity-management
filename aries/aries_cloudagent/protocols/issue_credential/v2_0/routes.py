"""Credential exchange admin routes."""

import logging

from json.decoder import JSONDecodeError
from typing import Mapping

from aiohttp import web
from aiohttp_apispec import (
    docs,
    match_info_schema,
    querystring_schema,
    request_schema,
    response_schema,
)
from marshmallow import fields, validate, validates_schema, ValidationError

from ...out_of_band.v1_0.models.oob_record import OobRecord
from ....wallet.util import default_did_from_verkey
from ....admin.request_context import AdminRequestContext
from ....connections.models.conn_record import ConnRecord
from ....core.profile import Profile
from ....indy.holder import IndyHolderError
from ....indy.issuer import IndyIssuerError
from ....ledger.error import LedgerError
from ....messaging.decorators.attach_decorator import AttachDecorator
from ....messaging.models.base import BaseModelError
from ....messaging.models.openapi import OpenAPISchema
from ....messaging.valid import (
    INDY_CRED_DEF_ID,
    INDY_DID,
    INDY_SCHEMA_ID,
    INDY_VERSION,
    UUIDFour,
    UUID4,
)
from ....storage.error import StorageError, StorageNotFoundError
from ....utils.tracing import trace_event, get_timer, AdminAPIMessageTracingSchema
from ....vc.ld_proofs.error import LinkedDataProofException

from . import problem_report_for_record, report_problem
from .manager import V20CredManager, V20CredManagerError
from .message_types import ATTACHMENT_FORMAT, CRED_20_PROPOSAL, SPEC_URI
from .messages.cred_format import V20CredFormat
from .messages.cred_problem_report import ProblemReportReason
from .messages.cred_proposal import V20CredProposal
from .messages.inner.cred_preview import V20CredPreview, V20CredPreviewSchema
from .models.cred_ex_record import V20CredExRecord, V20CredExRecordSchema
from .models.detail.ld_proof import V20CredExRecordLDProofSchema
from .models.detail.indy import V20CredExRecordIndySchema
from .formats.handler import V20CredFormatError
from .formats.ld_proof.models.cred_detail import LDProofVCDetailSchema

LOGGER = logging.getLogger(__name__)


class V20IssueCredentialModuleResponseSchema(OpenAPISchema):
    """Response schema for v2.0 Issue Credential Module."""


class V20CredExRecordListQueryStringSchema(OpenAPISchema):
    """Parameters and validators for credential exchange record list query."""

    connection_id = fields.UUID(
        description="Connection identifier",
        required=False,
        example=UUIDFour.EXAMPLE,  # typically but not necessarily a UUID4
    )
    thread_id = fields.UUID(
        description="Thread identifier",
        required=False,
        example=UUIDFour.EXAMPLE,  # typically but not necessarily a UUID4
    )
    role = fields.Str(
        description="Role assigned in credential exchange",
        required=False,
        validate=validate.OneOf(
            [
                getattr(V20CredExRecord, m)
                for m in vars(V20CredExRecord)
                if m.startswith("ROLE_")
            ]
        ),
    )
    state = fields.Str(
        description="Credential exchange state",
        required=False,
        validate=validate.OneOf(
            [
                getattr(V20CredExRecord, m)
                for m in vars(V20CredExRecord)
                if m.startswith("STATE_")
            ]
        ),
    )


class V20CredExRecordDetailSchema(OpenAPISchema):
    """Credential exchange record and any per-format details."""

    cred_ex_record = fields.Nested(
        V20CredExRecordSchema,
        required=False,
        description="Credential exchange record",
    )

    indy = fields.Nested(
        V20CredExRecordIndySchema,
        required=False,
    )
    ld_proof = fields.Nested(
        V20CredExRecordLDProofSchema,
        required=False,
    )


class V20CredExRecordListResultSchema(OpenAPISchema):
    """Result schema for credential exchange record list query."""

    results = fields.List(
        fields.Nested(V20CredExRecordDetailSchema),
        description="Credential exchange records and corresponding detail records",
    )


class V20CredStoreRequestSchema(OpenAPISchema):
    """Request schema for sending a credential store admin message."""

    credential_id = fields.Str(required=False)


class V20CredFilterIndySchema(OpenAPISchema):
    """Indy credential filtration criteria."""

    cred_def_id = fields.Str(
        description="Credential definition identifier",
        required=False,
        **INDY_CRED_DEF_ID,
    )
    schema_id = fields.Str(
        description="Schema identifier", required=False, **INDY_SCHEMA_ID
    )
    schema_issuer_did = fields.Str(
        description="Schema issuer DID", required=False, **INDY_DID
    )
    schema_name = fields.Str(
        description="Schema name", required=False, example="preferences"
    )
    schema_version = fields.Str(
        description="Schema version", required=False, **INDY_VERSION
    )
    issuer_did = fields.Str(
        description="Credential issuer DID", required=False, **INDY_DID
    )


class V20CredFilterSchema(OpenAPISchema):
    """Credential filtration criteria."""

    indy = fields.Nested(
        V20CredFilterIndySchema,
        required=False,
        description="Credential filter for indy",
    )
    ld_proof = fields.Nested(
        LDProofVCDetailSchema,
        required=False,
        description="Credential filter for linked data proof",
    )

    @validates_schema
    def validate_fields(self, data, **kwargs):
        """
        Validate schema fields.

        Data must have indy, ld_proof, or both.

        Args:
            data: The data to validate

        Raises:
            ValidationError: if data has neither indy nor ld_proof

        """
        if not any(f.api in data for f in V20CredFormat.Format):
            raise ValidationError(
                "V20CredFilterSchema requires indy, ld_proof, or both"
            )


class V20IssueCredSchemaCore(AdminAPIMessageTracingSchema):
    """Filter, auto-remove, comment, trace."""

    filter_ = fields.Nested(
        V20CredFilterSchema,
        required=True,
        data_key="filter",
        description="Credential specification criteria by format",
    )
    auto_remove = fields.Bool(
        description=(
            "Whether to remove the credential exchange record on completion "
            "(overrides --preserve-exchange-records configuration setting)"
        ),
        required=False,
    )
    comment = fields.Str(
        description="Human-readable comment", required=False, allow_none=True
    )

    credential_preview = fields.Nested(V20CredPreviewSchema, required=False)

    @validates_schema
    def validate(self, data, **kwargs):
        """Make sure preview is present when indy format is present."""

        if data.get("filter", {}).get("indy") and not data.get("credential_preview"):
            raise ValidationError(
                "Credential preview is required if indy filter is present"
            )


class V20CredFilterLDProofSchema(OpenAPISchema):
    """Credential filtration criteria."""

    ld_proof = fields.Nested(
        LDProofVCDetailSchema,
        required=True,
        description="Credential filter for linked data proof",
    )


class V20CredRequestFreeSchema(AdminAPIMessageTracingSchema):
    """Filter, auto-remove, comment, trace."""

    connection_id = fields.UUID(
        description="Connection identifier",
        required=True,
        example=UUIDFour.EXAMPLE,  # typically but not necessarily a UUID4
    )
    # Request can only start with LD Proof
    filter_ = fields.Nested(
        V20CredFilterLDProofSchema,
        required=True,
        data_key="filter",
        description="Credential specification criteria by format",
    )
    auto_remove = fields.Bool(
        description=(
            "Whether to remove the credential exchange record on completion "
            "(overrides --preserve-exchange-records configuration setting)"
        ),
        required=False,
    )
    comment = fields.Str(
        description="Human-readable comment", required=False, allow_none=True
    )
    trace = fields.Bool(
        description="Whether to trace event (default false)",
        required=False,
        example=False,
    )
    holder_did = fields.Str(
        description="Holder DID to substitute for the credentialSubject.id",
        required=False,
        allow_none=True,
        example="did:key:ahsdkjahsdkjhaskjdhakjshdkajhsdkjahs",
    )


class V20CredExFreeSchema(V20IssueCredSchemaCore):
    """Request schema for sending credential admin message."""

    connection_id = fields.UUID(
        description="Connection identifier",
        required=True,
        example=UUIDFour.EXAMPLE,  # typically but not necessarily a UUID4
    )


class V20CredBoundOfferRequestSchema(OpenAPISchema):
    """Request schema for sending bound credential offer admin message."""

    filter_ = fields.Nested(
        V20CredFilterSchema,
        required=False,
        data_key="filter",
        description="Credential specification criteria by format",
    )
    counter_preview = fields.Nested(
        V20CredPreviewSchema,
        required=False,
        description="Optional content for counter-proposal",
    )

    @validates_schema
    def validate_fields(self, data, **kwargs):
        """Validate schema fields: need both filter and counter_preview or neither."""
        if (
            "filter_" in data
            and ("indy" in data["filter_"] or "ld_proof" in data["filter_"])
        ) ^ ("counter_preview" in data):
            raise ValidationError(
                f"V20CredBoundOfferRequestSchema\n{data}\nrequires "
                "both indy/ld_proof filter and counter_preview or neither"
            )


class V20CredOfferRequestSchema(V20IssueCredSchemaCore):
    """Request schema for sending credential offer admin message."""

    connection_id = fields.UUID(
        description="Connection identifier",
        required=True,
        example=UUIDFour.EXAMPLE,  # typically but not necessarily a UUID4
    )
    auto_issue = fields.Bool(
        description=(
            "Whether to respond automatically to credential requests, creating "
            "and issuing requested credentials"
        ),
        required=False,
    )


class V20CredOfferConnFreeRequestSchema(V20IssueCredSchemaCore):
    """Request schema for creating credential offer free from connection."""

    auto_issue = fields.Bool(
        description=(
            "Whether to respond automatically to credential requests, creating "
            "and issuing requested credentials"
        ),
        required=False,
    )


class V20CredRequestRequestSchema(OpenAPISchema):
    """Request schema for sending credential request message."""

    holder_did = fields.Str(
        description="Holder DID to substitute for the credentialSubject.id",
        required=False,
        allow_none=True,
        example="did:key:ahsdkjahsdkjhaskjdhakjshdkajhsdkjahs",
    )


class V20CredIssueRequestSchema(OpenAPISchema):
    """Request schema for sending credential issue admin message."""

    comment = fields.Str(
        description="Human-readable comment", required=False, allow_none=True
    )


class V20CredIssueProblemReportRequestSchema(OpenAPISchema):
    """Request schema for sending problem report."""

    description = fields.Str(required=True)


class V20CredIdMatchInfoSchema(OpenAPISchema):
    """Path parameters and validators for request taking credential id."""

    credential_id = fields.Str(
        description="Credential identifier", required=True, example=UUIDFour.EXAMPLE
    )


class V20CredExIdMatchInfoSchema(OpenAPISchema):
    """Path parameters and validators for request taking credential exchange id."""

    cred_ex_id = fields.Str(
        description="Credential exchange identifier", required=True, **UUID4
    )


def _formats_filters(filt_spec: Mapping) -> Mapping:
    """Break out formats and filters for v2.0 cred proposal messages."""

    return (
        {
            "formats": [
                V20CredFormat(
                    attach_id=fmt_api,
                    format_=ATTACHMENT_FORMAT[CRED_20_PROPOSAL][fmt_api],
                )
                for fmt_api in filt_spec
            ],
            "filters_attach": [
                AttachDecorator.data_base64(filt_by_fmt, ident=fmt_api)
                for (fmt_api, filt_by_fmt) in filt_spec.items()
            ],
        }
        if filt_spec
        else {}
    )


async def _get_attached_credentials(
    profile: Profile, cred_ex_record: V20CredExRecord
) -> Mapping:
    """Fetch the detail records attached to a credential exchange."""
    result = {}

    for fmt in V20CredFormat.Format:
        detail_record = await fmt.handler(profile).get_detail_record(
            cred_ex_record.cred_ex_id
        )
        if detail_record:
            result[fmt.api] = detail_record

    return result


def _format_result_with_details(
    cred_ex_record: V20CredExRecord, details: Mapping
) -> Mapping:
    """Get credential exchange result with detail records."""
    result = {"cred_ex_record": cred_ex_record.serialize()}
    for fmt in V20CredFormat.Format:
        ident = fmt.api
        detail_record = details.get(ident)
        result[ident] = detail_record.serialize() if detail_record else None
    return result


@docs(
    tags=["issue-credential v2.0"],
    summary="Fetch all credential exchange records",
)
@querystring_schema(V20CredExRecordListQueryStringSchema)
@response_schema(V20CredExRecordListResultSchema(), 200, description="")
async def credential_exchange_list(request: web.BaseRequest):
    """
    Request handler for searching credential exchange records.

    Args:
        request: aiohttp request object

    Returns:
        The connection list response

    """
    context: AdminRequestContext = request["context"]
    profile = context.profile
    tag_filter = {}
    if "thread_id" in request.query and request.query["thread_id"] != "":
        tag_filter["thread_id"] = request.query["thread_id"]
    post_filter = {
        k: request.query[k]
        for k in ("connection_id", "role", "state")
        if request.query.get(k, "") != ""
    }

    try:
        async with profile.session() as session:
            cred_ex_records = await V20CredExRecord.query(
                session=session,
                tag_filter=tag_filter,
                post_filter_positive=post_filter,
            )

        results = []
        for cxr in cred_ex_records:
            details = await _get_attached_credentials(profile, cxr)
            result = _format_result_with_details(cxr, details)
            results.append(result)

    except (StorageError, BaseModelError) as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response({"results": results})


@docs(
    tags=["issue-credential v2.0"],
    summary="Fetch a single credential exchange record",
)
@match_info_schema(V20CredExIdMatchInfoSchema())
@response_schema(V20CredExRecordDetailSchema(), 200, description="")
async def credential_exchange_retrieve(request: web.BaseRequest):
    """
    Request handler for fetching single credential exchange record.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    cred_ex_id = request.match_info["cred_ex_id"]
    cred_ex_record = None
    try:
        async with profile.session() as session:
            cred_ex_record = await V20CredExRecord.retrieve_by_id(session, cred_ex_id)

        details = await _get_attached_credentials(profile, cred_ex_record)
        result = _format_result_with_details(cred_ex_record, details)
    except StorageNotFoundError as err:
        # no such cred ex record: not protocol error, user fat-fingered id
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except (BaseModelError, StorageError) as err:
        # present but broken or hopeless: protocol error
        await report_problem(
            err,
            ProblemReportReason.ISSUANCE_ABANDONED.value,
            web.HTTPBadRequest,
            cred_ex_record,
            outbound_handler,
        )

    return web.json_response(result)


@docs(
    tags=["issue-credential v2.0"],
    summary="Create credential from attribute values",
)
@request_schema(V20IssueCredSchemaCore())
@response_schema(V20CredExRecordSchema(), 200, description="")
async def credential_exchange_create(request: web.BaseRequest):
    """
    Request handler for creating a credential from attr values.

    The internal credential record will be created without the credential
    being sent to any connection. This can be used in conjunction with
    the `oob` protocols to bind messages to an out of band message.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]

    body = await request.json()

    comment = body.get("comment")
    preview_spec = body.get("credential_preview")
    filt_spec = body.get("filter")
    auto_remove = body.get("auto_remove")
    if not filt_spec:
        raise web.HTTPBadRequest(reason="Missing filter")
    trace_msg = body.get("trace")

    try:
        # Not all formats use credential preview
        cred_preview = (
            V20CredPreview.deserialize(preview_spec) if preview_spec else None
        )
        cred_proposal = V20CredProposal(
            comment=comment,
            credential_preview=cred_preview,
            **_formats_filters(filt_spec),
        )
        cred_proposal.assign_trace_decorator(
            context.settings,
            trace_msg,
        )

        trace_event(
            context.settings,
            cred_proposal,
            outcome="credential_exchange_create.START",
        )

        cred_manager = V20CredManager(context.profile)
        (cred_ex_record, cred_offer_message) = await cred_manager.prepare_send(
            connection_id=None,
            cred_proposal=cred_proposal,
            auto_remove=auto_remove,
        )
    except (StorageError, BaseModelError) as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    trace_event(
        context.settings,
        cred_offer_message,
        outcome="credential_exchange_create.END",
        perf_counter=r_time,
    )

    return web.json_response(cred_ex_record.serialize())


@docs(
    tags=["issue-credential v2.0"],
    summary="Send holder a credential, automating entire flow",
)
@request_schema(V20CredExFreeSchema())
@response_schema(V20CredExRecordSchema(), 200, description="")
async def credential_exchange_send(request: web.BaseRequest):
    """
    Request handler for sending credential from issuer to holder from attr values.

    If both issuer and holder are configured for automatic responses, the operation
    ultimately results in credential issue; otherwise, the result waits on the first
    response not automated; the credential exchange record retains state regardless.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json()

    comment = body.get("comment")
    connection_id = body.get("connection_id")
    filt_spec = body.get("filter")
    if not filt_spec:
        raise web.HTTPBadRequest(reason="Missing filter")
    preview_spec = body.get("credential_preview")
    auto_remove = body.get("auto_remove")
    trace_msg = body.get("trace")

    conn_record = None
    cred_ex_record = None
    try:
        # Not all formats use credential preview
        cred_preview = (
            V20CredPreview.deserialize(preview_spec) if preview_spec else None
        )
        async with profile.session() as session:
            conn_record = await ConnRecord.retrieve_by_id(session, connection_id)
        if not conn_record.is_ready:
            raise web.HTTPForbidden(reason=f"Connection {connection_id} not ready")

        # TODO: why do we create a proposal and then use that to create an offer.
        # Seems easier to just pass the proposal data to the format specific handler
        cred_proposal = V20CredProposal(
            comment=comment,
            credential_preview=cred_preview,
            **_formats_filters(filt_spec),
        )
        cred_proposal.assign_trace_decorator(
            context.settings,
            trace_msg,
        )

        trace_event(
            context.settings,
            cred_proposal,
            outcome="credential_exchange_send.START",
        )

        cred_manager = V20CredManager(profile)
        (cred_ex_record, cred_offer_message) = await cred_manager.prepare_send(
            connection_id,
            cred_proposal=cred_proposal,
            auto_remove=auto_remove,
        )
        result = cred_ex_record.serialize()

    except (
        BaseModelError,
        LedgerError,
        StorageError,
        V20CredManagerError,
        V20CredFormatError,
    ) as err:
        LOGGER.exception("Error preparing credential offer")
        if cred_ex_record:
            async with profile.session() as session:
                await cred_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ISSUANCE_ABANDONED.value,
            web.HTTPBadRequest,
            cred_ex_record or conn_record,
            outbound_handler,
        )

    await outbound_handler(
        cred_offer_message,
        connection_id=cred_ex_record.connection_id,
    )

    trace_event(
        context.settings,
        cred_offer_message,
        outcome="credential_exchange_send.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["issue-credential v2.0"],
    summary="Send issuer a credential proposal",
)
@request_schema(V20CredExFreeSchema())
@response_schema(V20CredExRecordSchema(), 200, description="")
async def credential_exchange_send_proposal(request: web.BaseRequest):
    """
    Request handler for sending credential proposal.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json()

    connection_id = body.get("connection_id")
    comment = body.get("comment")
    preview_spec = body.get("credential_preview")
    filt_spec = body.get("filter")
    if not filt_spec:
        raise web.HTTPBadRequest(reason="Missing filter")
    auto_remove = body.get("auto_remove")
    trace_msg = body.get("trace")

    conn_record = None
    cred_ex_record = None
    try:
        cred_preview = (
            V20CredPreview.deserialize(preview_spec) if preview_spec else None
        )
        async with profile.session() as session:
            conn_record = await ConnRecord.retrieve_by_id(session, connection_id)
        if not conn_record.is_ready:
            raise web.HTTPForbidden(reason=f"Connection {connection_id} not ready")

        cred_manager = V20CredManager(profile)
        cred_ex_record = await cred_manager.create_proposal(
            connection_id=connection_id,
            auto_remove=auto_remove,
            comment=comment,
            cred_preview=cred_preview,
            trace=trace_msg,
            fmt2filter={
                V20CredFormat.Format.get(fmt_api): filt_by_fmt
                for (fmt_api, filt_by_fmt) in filt_spec.items()
            },
        )

        cred_proposal_message = cred_ex_record.cred_proposal
        result = cred_ex_record.serialize()

    except (BaseModelError, StorageError) as err:
        LOGGER.exception("Error preparing credential proposal")
        if cred_ex_record:
            async with profile.session() as session:
                await cred_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ISSUANCE_ABANDONED.value,
            web.HTTPBadRequest,
            cred_ex_record or conn_record,
            outbound_handler,
        )

    await outbound_handler(cred_proposal_message, connection_id=connection_id)

    trace_event(
        context.settings,
        cred_proposal_message,
        outcome="credential_exchange_send_proposal.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


async def _create_free_offer(
    profile: Profile,
    filt_spec: Mapping = None,
    connection_id: str = None,
    auto_issue: bool = False,
    auto_remove: bool = False,
    preview_spec: dict = None,
    comment: str = None,
    trace_msg: bool = None,
):
    """Create a credential offer and related exchange record."""

    cred_preview = V20CredPreview.deserialize(preview_spec) if preview_spec else None
    cred_proposal = V20CredProposal(
        comment=comment,
        credential_preview=cred_preview,
        **_formats_filters(filt_spec),
    )
    cred_proposal.assign_trace_decorator(
        profile.settings,
        trace_msg,
    )

    cred_ex_record = V20CredExRecord(
        connection_id=connection_id,
        initiator=V20CredExRecord.INITIATOR_SELF,
        role=V20CredExRecord.ROLE_ISSUER,
        cred_proposal=cred_proposal.serialize(),
        auto_issue=auto_issue,
        auto_remove=auto_remove,
        trace=trace_msg,
    )

    cred_manager = V20CredManager(profile)
    (cred_ex_record, cred_offer_message) = await cred_manager.create_offer(
        cred_ex_record,
        comment=comment,
    )

    return (cred_ex_record, cred_offer_message)


@docs(
    tags=["issue-credential v2.0"],
    summary="Create a credential offer, independent of any proposal or connection",
)
@request_schema(V20CredOfferConnFreeRequestSchema())
@response_schema(V20CredExRecordSchema(), 200, description="")
async def credential_exchange_create_free_offer(request: web.BaseRequest):
    """
    Request handler for creating free credential offer.

    Unlike with `send-offer`, this credential exchange is not tied to a specific
    connection. It must be dispatched out-of-band by the controller.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile

    body = await request.json()

    auto_issue = body.get(
        "auto_issue", context.settings.get("debug.auto_respond_credential_request")
    )
    auto_remove = body.get("auto_remove")
    comment = body.get("comment")
    preview_spec = body.get("credential_preview")
    filt_spec = body.get("filter")
    if not filt_spec:
        raise web.HTTPBadRequest(reason="Missing filter")
    trace_msg = body.get("trace")
    cred_ex_record = None
    try:
        (cred_ex_record, cred_offer_message) = await _create_free_offer(
            profile=profile,
            filt_spec=filt_spec,
            auto_issue=auto_issue,
            auto_remove=auto_remove,
            preview_spec=preview_spec,
            comment=comment,
            trace_msg=trace_msg,
        )
        result = cred_ex_record.serialize()
    except (
        BaseModelError,
        LedgerError,
        V20CredFormatError,
        V20CredManagerError,
    ) as err:
        LOGGER.exception("Error creating free credential offer")
        if cred_ex_record:
            async with profile.session() as session:
                await cred_ex_record.save_error_state(session, reason=err.roll_up)
        raise web.HTTPBadRequest(reason=err.roll_up)
    trace_event(
        context.settings,
        cred_offer_message,
        outcome="credential_exchange_create_free_offer.END",
        perf_counter=r_time,
    )
    return web.json_response(result)


@docs(
    tags=["issue-credential v2.0"],
    summary="Send holder a credential offer, independent of any proposal",
)
@request_schema(V20CredOfferRequestSchema())
@response_schema(V20CredExRecordSchema(), 200, description="")
async def credential_exchange_send_free_offer(request: web.BaseRequest):
    """
    Request handler for sending free credential offer.

    An issuer initiates a such a credential offer, free from any
    holder-initiated corresponding credential proposal with preview.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json()

    connection_id = body.get("connection_id")
    filt_spec = body.get("filter")
    if not filt_spec:
        raise web.HTTPBadRequest(reason="Missing filter")
    auto_issue = body.get(
        "auto_issue", context.settings.get("debug.auto_respond_credential_request")
    )
    auto_remove = body.get("auto_remove")
    comment = body.get("comment")
    preview_spec = body.get("credential_preview")
    trace_msg = body.get("trace")

    cred_ex_record = None
    conn_record = None
    try:
        async with profile.session() as session:
            conn_record = await ConnRecord.retrieve_by_id(session, connection_id)
        if not conn_record.is_ready:
            raise web.HTTPForbidden(reason=f"Connection {connection_id} not ready")

        cred_ex_record, cred_offer_message = await _create_free_offer(
            profile=profile,
            filt_spec=filt_spec,
            connection_id=connection_id,
            auto_issue=auto_issue,
            auto_remove=auto_remove,
            preview_spec=preview_spec,
            comment=comment,
            trace_msg=trace_msg,
        )
        result = cred_ex_record.serialize()

    except (
        BaseModelError,
        IndyIssuerError,
        LedgerError,
        StorageNotFoundError,
        V20CredFormatError,
        V20CredManagerError,
    ) as err:
        LOGGER.exception("Error preparing free credential offer")
        if cred_ex_record:
            async with profile.session() as session:
                await cred_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ISSUANCE_ABANDONED.value,
            web.HTTPBadRequest,
            cred_ex_record or conn_record,
            outbound_handler,
        )

    await outbound_handler(cred_offer_message, connection_id=connection_id)

    trace_event(
        context.settings,
        cred_offer_message,
        outcome="credential_exchange_send_free_offer.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["issue-credential v2.0"],
    summary="Send holder a credential offer in reference to a proposal with preview",
)
@match_info_schema(V20CredExIdMatchInfoSchema())
@request_schema(V20CredBoundOfferRequestSchema())
@response_schema(V20CredExRecordSchema(), 200, description="")
async def credential_exchange_send_bound_offer(request: web.BaseRequest):
    """
    Request handler for sending bound credential offer.

    A holder initiates this sequence with a credential proposal; this message
    responds with an offer bound to the proposal.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json() if request.body_exists else {}
    filt_spec = body.get("filter")
    preview_spec = body.get("counter_preview")

    cred_ex_id = request.match_info["cred_ex_id"]
    cred_ex_record = None
    conn_record = None
    try:
        async with profile.session() as session:
            try:
                cred_ex_record = await V20CredExRecord.retrieve_by_id(
                    session,
                    cred_ex_id,
                )
            except StorageNotFoundError as err:
                raise web.HTTPNotFound(reason=err.roll_up) from err

            connection_id = cred_ex_record.connection_id
            if cred_ex_record.state != (
                V20CredExRecord.STATE_PROPOSAL_RECEIVED
            ):  # check state here: manager call creates free offers too
                raise V20CredManagerError(
                    f"Credential exchange record {cred_ex_record.cred_ex_id} "
                    f"in {cred_ex_record.state} state "
                    f"(must be {V20CredExRecord.STATE_PROPOSAL_RECEIVED})"
                )

            conn_record = await ConnRecord.retrieve_by_id(session, connection_id)
            if not conn_record.is_ready:
                raise web.HTTPForbidden(reason=f"Connection {connection_id} not ready")

        cred_manager = V20CredManager(profile)
        (cred_ex_record, cred_offer_message) = await cred_manager.create_offer(
            cred_ex_record,
            counter_proposal=V20CredProposal(
                comment=None,
                credential_preview=V20CredPreview.deserialize(preview_spec),
                **_formats_filters(filt_spec),
            )
            if preview_spec
            else None,
            comment=None,
        )

        result = cred_ex_record.serialize()

    except (
        BaseModelError,
        IndyIssuerError,
        LedgerError,
        StorageError,
        V20CredFormatError,
        V20CredManagerError,
    ) as err:
        LOGGER.exception("Error preparing bound credential offer")
        if cred_ex_record:
            async with profile.session() as session:
                await cred_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ISSUANCE_ABANDONED.value,
            web.HTTPBadRequest,
            cred_ex_record,
            outbound_handler,
        )
    except LinkedDataProofException as err:
        raise web.HTTPBadRequest(reason=err) from err

    await outbound_handler(cred_offer_message, connection_id=connection_id)

    trace_event(
        context.settings,
        cred_offer_message,
        outcome="credential_exchange_send_bound_offer.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["issue-credential v2.0"],
    summary=(
        "Send issuer a credential request not bound to an existing thread."
        " Indy credentials cannot start at a request"
    ),
)
@request_schema(V20CredRequestFreeSchema())
@response_schema(V20CredExRecordSchema(), 200, description="")
async def credential_exchange_send_free_request(request: web.BaseRequest):
    """
    Request handler for sending free credential request.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json()

    connection_id = body.get("connection_id")
    comment = body.get("comment")
    filt_spec = body.get("filter")
    if not filt_spec:
        raise web.HTTPBadRequest(reason="Missing filter")
    auto_remove = body.get("auto_remove")
    trace_msg = body.get("trace")
    holder_did = body.get("holder_did")

    conn_record = None
    cred_ex_record = None
    try:
        try:
            async with profile.session() as session:
                conn_record = await ConnRecord.retrieve_by_id(session, connection_id)
            if not conn_record.is_ready:
                raise web.HTTPForbidden(reason=f"Connection {connection_id} not ready")
        except StorageNotFoundError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

        cred_manager = V20CredManager(profile)

        cred_proposal = V20CredProposal(
            comment=comment,
            **_formats_filters(filt_spec),
        )

        cred_ex_record = V20CredExRecord(
            connection_id=connection_id,
            auto_remove=auto_remove,
            cred_proposal=cred_proposal.serialize(),
            initiator=V20CredExRecord.INITIATOR_SELF,
            role=V20CredExRecord.ROLE_HOLDER,
            trace=trace_msg,
        )

        cred_ex_record, cred_request_message = await cred_manager.create_request(
            cred_ex_record=cred_ex_record,
            holder_did=holder_did,
            comment=comment,
        )

        result = cred_ex_record.serialize()

    except (
        BaseModelError,
        IndyHolderError,
        LedgerError,
        StorageError,
        V20CredManagerError,
    ) as err:
        LOGGER.exception("Error preparing free credential request")
        if cred_ex_record:
            async with profile.session() as session:
                await cred_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ISSUANCE_ABANDONED.value,
            web.HTTPBadRequest,
            cred_ex_record,
            outbound_handler,
        )

    await outbound_handler(cred_request_message, connection_id=connection_id)

    trace_event(
        context.settings,
        cred_request_message,
        outcome="credential_exchange_send_free_request.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["issue-credential v2.0"],
    summary="Send issuer a credential request",
)
@match_info_schema(V20CredExIdMatchInfoSchema())
@request_schema(V20CredRequestRequestSchema())
@response_schema(V20CredExRecordSchema(), 200, description="")
async def credential_exchange_send_bound_request(request: web.BaseRequest):
    """
    Request handler for sending credential request.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    try:
        body = await request.json() or {}
        holder_did = body.get("holder_did")
    except JSONDecodeError:
        holder_did = None

    cred_ex_id = request.match_info["cred_ex_id"]

    cred_ex_record = None
    conn_record = None
    try:
        async with profile.session() as session:
            try:
                cred_ex_record = await V20CredExRecord.retrieve_by_id(
                    session,
                    cred_ex_id,
                )
            except StorageNotFoundError as err:
                raise web.HTTPNotFound(reason=err.roll_up) from err

            conn_record = None
            if cred_ex_record.connection_id:
                try:
                    conn_record = await ConnRecord.retrieve_by_id(
                        session, cred_ex_record.connection_id
                    )
                except StorageNotFoundError as err:
                    raise web.HTTPBadRequest(reason=err.roll_up) from err

        if conn_record and not conn_record.is_ready:
            raise web.HTTPForbidden(
                reason=f"Connection {cred_ex_record.connection_id} not ready"
            )

        if conn_record or holder_did:
            holder_did = holder_did or conn_record.my_did
        else:
            # Need to get the holder DID from the out of band record
            async with profile.session() as session:
                oob_record = await OobRecord.retrieve_by_tag_filter(
                    session,
                    {"invi_msg_id": cred_ex_record.cred_offer._thread.pthid},
                )
                # Transform recipient key into did
                holder_did = default_did_from_verkey(oob_record.our_recipient_key)

        cred_manager = V20CredManager(profile)
        cred_ex_record, cred_request_message = await cred_manager.create_request(
            cred_ex_record,
            holder_did,
        )

        result = cred_ex_record.serialize()

    except (
        BaseModelError,
        IndyHolderError,
        LedgerError,
        StorageError,
        V20CredFormatError,
        V20CredManagerError,
    ) as err:
        LOGGER.exception("Error preparing bound credential request")
        if cred_ex_record:
            async with profile.session() as session:
                await cred_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ISSUANCE_ABANDONED.value,
            web.HTTPBadRequest,
            cred_ex_record,
            outbound_handler,
        )

    await outbound_handler(
        cred_request_message, connection_id=cred_ex_record.connection_id
    )

    trace_event(
        context.settings,
        cred_request_message,
        outcome="credential_exchange_send_bound_request.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["issue-credential v2.0"],
    summary="Send holder a credential",
)
@match_info_schema(V20CredExIdMatchInfoSchema())
@request_schema(V20CredIssueRequestSchema())
@response_schema(V20CredExRecordDetailSchema(), 200, description="")
async def credential_exchange_issue(request: web.BaseRequest):
    """
    Request handler for sending credential.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json()
    comment = body.get("comment")

    cred_ex_id = request.match_info["cred_ex_id"]

    cred_ex_record = None
    conn_record = None
    try:
        async with profile.session() as session:
            try:
                cred_ex_record = await V20CredExRecord.retrieve_by_id(
                    session,
                    cred_ex_id,
                )
            except StorageNotFoundError as err:
                raise web.HTTPNotFound(reason=err.roll_up) from err

            conn_record = None
            if cred_ex_record.connection_id:
                conn_record = await ConnRecord.retrieve_by_id(
                    session, cred_ex_record.connection_id
                )
        if conn_record and not conn_record.is_ready:
            raise web.HTTPForbidden(
                reason=f"Connection {cred_ex_record.connection_id} not ready"
            )

        cred_manager = V20CredManager(profile)
        (cred_ex_record, cred_issue_message) = await cred_manager.issue_credential(
            cred_ex_record,
            comment=comment,
        )

        details = await _get_attached_credentials(profile, cred_ex_record)
        result = _format_result_with_details(cred_ex_record, details)

    except (
        BaseModelError,
        IndyIssuerError,
        LedgerError,
        StorageError,
        V20CredFormatError,
        V20CredManagerError,
    ) as err:
        LOGGER.exception("Error preparing issued credential")
        if cred_ex_record:
            async with profile.session() as session:
                await cred_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ISSUANCE_ABANDONED.value,
            web.HTTPBadRequest,
            cred_ex_record,
            outbound_handler,
        )

    await outbound_handler(
        cred_issue_message, connection_id=cred_ex_record.connection_id
    )

    trace_event(
        context.settings,
        cred_issue_message,
        outcome="credential_exchange_issue.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["issue-credential v2.0"],
    summary="Store a received credential",
)
@match_info_schema(V20CredExIdMatchInfoSchema())
@request_schema(V20CredStoreRequestSchema())
@response_schema(V20CredExRecordDetailSchema(), 200, description="")
async def credential_exchange_store(request: web.BaseRequest):
    """
    Request handler for storing credential.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    try:
        body = await request.json() or {}
        cred_id = body.get("credential_id")
    except JSONDecodeError:
        cred_id = None

    cred_ex_id = request.match_info["cred_ex_id"]
    cred_ex_record = None
    conn_record = None
    try:
        async with profile.session() as session:
            try:
                cred_ex_record = await V20CredExRecord.retrieve_by_id(
                    session,
                    cred_ex_id,
                )
            except StorageNotFoundError as err:
                raise web.HTTPNotFound(reason=err.roll_up) from err

            conn_record = None
            if cred_ex_record.connection_id:
                conn_record = await ConnRecord.retrieve_by_id(
                    session, cred_ex_record.connection_id
                )
            if conn_record and not conn_record.is_ready:
                raise web.HTTPForbidden(
                    reason=f"Connection {cred_ex_record.connection_id} not ready"
                )

        cred_manager = V20CredManager(profile)
        cred_ex_record = await cred_manager.store_credential(cred_ex_record, cred_id)

    except (
        IndyHolderError,
        StorageError,
        V20CredManagerError,
    ) as err:  # treat failure to store as mangled on receipt hence protocol error
        LOGGER.exception("Error storing issued credential")
        if cred_ex_record:
            async with profile.session() as session:
                await cred_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ISSUANCE_ABANDONED.value,
            web.HTTPBadRequest,
            cred_ex_record,
            outbound_handler,
        )

    try:
        # fetch these early, before potential removal
        details = await _get_attached_credentials(profile, cred_ex_record)

        # the record may be auto-removed here
        (
            cred_ex_record,
            cred_ack_message,
        ) = await cred_manager.send_cred_ack(cred_ex_record)

        result = _format_result_with_details(cred_ex_record, details)

    except (
        BaseModelError,
        StorageError,
        V20CredFormatError,
        V20CredManagerError,
    ) as err:
        # protocol finished OK: do not send problem report nor set record state error
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    trace_event(
        context.settings,
        cred_ack_message,
        outcome="credential_exchange_store.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["issue-credential v2.0"],
    summary="Remove an existing credential exchange record",
)
@match_info_schema(V20CredExIdMatchInfoSchema())
@response_schema(V20IssueCredentialModuleResponseSchema(), 200, description="")
async def credential_exchange_remove(request: web.BaseRequest):
    """
    Request handler for removing a credential exchange record.

    Args:
        request: aiohttp request object

    """
    context: AdminRequestContext = request["context"]

    cred_ex_id = request.match_info["cred_ex_id"]
    try:
        cred_manager = V20CredManager(context.profile)
        await cred_manager.delete_cred_ex_record(cred_ex_id)
    except StorageNotFoundError as err:  # not a protocol error
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except StorageError as err:  # not a protocol error
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response({})


@docs(
    tags=["issue-credential v2.0"],
    summary="Send a problem report for credential exchange",
)
@match_info_schema(V20CredExIdMatchInfoSchema())
@request_schema(V20CredIssueProblemReportRequestSchema())
@response_schema(V20IssueCredentialModuleResponseSchema(), 200, description="")
async def credential_exchange_problem_report(request: web.BaseRequest):
    """
    Request handler for sending problem report.

    Args:
        request: aiohttp request object

    """
    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    cred_ex_id = request.match_info["cred_ex_id"]
    body = await request.json()
    description = body["description"]

    try:
        async with profile.session() as session:
            cred_ex_record = await V20CredExRecord.retrieve_by_id(session, cred_ex_id)
            report = problem_report_for_record(cred_ex_record, description)
            await cred_ex_record.save_error_state(
                session,
                reason=f"created problem report: {description}",
            )
    except StorageNotFoundError as err:  # other party does not care about meta-problems
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except StorageError as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    await outbound_handler(report, connection_id=cred_ex_record.connection_id)

    return web.json_response({})


async def register(app: web.Application):
    """Register routes."""

    app.add_routes(
        [
            web.get(
                "/issue-credential-2.0/records",
                credential_exchange_list,
                allow_head=False,
            ),
            web.post(
                "/issue-credential-2.0/create-offer",
                credential_exchange_create_free_offer,
            ),
            web.get(
                "/issue-credential-2.0/records/{cred_ex_id}",
                credential_exchange_retrieve,
                allow_head=False,
            ),
            web.post("/issue-credential-2.0/create", credential_exchange_create),
            web.post("/issue-credential-2.0/send", credential_exchange_send),
            web.post(
                "/issue-credential-2.0/send-proposal", credential_exchange_send_proposal
            ),
            web.post(
                "/issue-credential-2.0/send-offer", credential_exchange_send_free_offer
            ),
            web.post(
                "/issue-credential-2.0/send-request",
                credential_exchange_send_free_request,
            ),
            web.post(
                "/issue-credential-2.0/records/{cred_ex_id}/send-offer",
                credential_exchange_send_bound_offer,
            ),
            web.post(
                "/issue-credential-2.0/records/{cred_ex_id}/send-request",
                credential_exchange_send_bound_request,
            ),
            web.post(
                "/issue-credential-2.0/records/{cred_ex_id}/issue",
                credential_exchange_issue,
            ),
            web.post(
                "/issue-credential-2.0/records/{cred_ex_id}/store",
                credential_exchange_store,
            ),
            web.post(
                "/issue-credential-2.0/records/{cred_ex_id}/problem-report",
                credential_exchange_problem_report,
            ),
            web.delete(
                "/issue-credential-2.0/records/{cred_ex_id}",
                credential_exchange_remove,
            ),
        ]
    )


def post_process_routes(app: web.Application):
    """Amend swagger API."""

    # Add top-level tags description
    if "tags" not in app._state["swagger_dict"]:
        app._state["swagger_dict"]["tags"] = []
    app._state["swagger_dict"]["tags"].append(
        {
            "name": "issue-credential v2.0",
            "description": "Credential issue v2.0",
            "externalDocs": {"description": "Specification", "url": SPEC_URI},
        }
    )
