"""Admin routes for presentations."""

import json

from aiohttp import web
from aiohttp_apispec import (
    docs,
    match_info_schema,
    querystring_schema,
    request_schema,
    response_schema,
)
from marshmallow import fields, validate, validates_schema, ValidationError
from typing import Mapping, Sequence, Tuple

from ....admin.request_context import AdminRequestContext
from ....connections.models.conn_record import ConnRecord
from ....indy.holder import IndyHolder, IndyHolderError
from ....indy.models.cred_precis import IndyCredPrecisSchema
from ....indy.models.proof import IndyPresSpecSchema
from ....indy.models.proof_request import IndyProofRequestSchema
from ....indy.util import generate_pr_nonce
from ....ledger.error import LedgerError
from ....messaging.decorators.attach_decorator import AttachDecorator
from ....messaging.models.base import BaseModelError
from ....messaging.models.openapi import OpenAPISchema
from ....messaging.valid import (
    INDY_EXTRA_WQL,
    NUM_STR_NATURAL,
    NUM_STR_WHOLE,
    UUIDFour,
    UUID4,
)
from ....storage.error import StorageError, StorageNotFoundError
from ....storage.base import BaseStorage
from ....storage.vc_holder.base import VCHolder
from ....storage.vc_holder.vc_record import VCRecord
from ....utils.tracing import trace_event, get_timer, AdminAPIMessageTracingSchema
from ....vc.ld_proofs import BbsBlsSignature2020, Ed25519Signature2018
from ....wallet.error import WalletNotFoundError

from ..dif.pres_exch import InputDescriptors, ClaimFormat, SchemaInputDescriptor
from ..dif.pres_proposal_schema import DIFProofProposalSchema
from ..dif.pres_request_schema import (
    DIFProofRequestSchema,
    DIFPresSpecSchema,
)

from . import problem_report_for_record, report_problem
from .formats.handler import V20PresFormatHandlerError
from .manager import V20PresManager
from .message_types import (
    ATTACHMENT_FORMAT,
    PRES_20_PROPOSAL,
    PRES_20_REQUEST,
    SPEC_URI,
)
from .messages.pres_format import V20PresFormat
from .messages.pres_problem_report import ProblemReportReason
from .messages.pres_proposal import V20PresProposal
from .messages.pres_request import V20PresRequest
from .models.pres_exchange import V20PresExRecord, V20PresExRecordSchema


class V20PresentProofModuleResponseSchema(OpenAPISchema):
    """Response schema for Present Proof Module."""


class V20PresExRecordListQueryStringSchema(OpenAPISchema):
    """Parameters and validators for presentation exchange list query."""

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
        description="Role assigned in presentation exchange",
        required=False,
        validate=validate.OneOf(
            [
                getattr(V20PresExRecord, m)
                for m in vars(V20PresExRecord)
                if m.startswith("ROLE_")
            ]
        ),
    )
    state = fields.Str(
        description="Presentation exchange state",
        required=False,
        validate=validate.OneOf(
            [
                getattr(V20PresExRecord, m)
                for m in vars(V20PresExRecord)
                if m.startswith("STATE_")
            ]
        ),
    )


class V20PresExRecordListSchema(OpenAPISchema):
    """Result schema for a presentation exchange query."""

    results = fields.List(
        fields.Nested(V20PresExRecordSchema()),
        description="Presentation exchange records",
    )


class V20PresProposalByFormatSchema(OpenAPISchema):
    """Schema for presentation proposal per format."""

    indy = fields.Nested(
        IndyProofRequestSchema,
        required=False,
        description="Presentation proposal for indy",
    )
    dif = fields.Nested(
        DIFProofProposalSchema,
        required=False,
        description="Presentation proposal for DIF",
    )

    @validates_schema
    def validate_fields(self, data, **kwargs):
        """
        Validate schema fields: data must have at least one format.

        Args:
            data: The data to validate

        Raises:
            ValidationError: if data has no formats

        """
        if not any(f.api in data for f in V20PresFormat.Format):
            raise ValidationError(
                "V20PresProposalByFormatSchema requires indy, dif, or both"
            )


class V20PresProposalRequestSchema(AdminAPIMessageTracingSchema):
    """Request schema for sending a presentation proposal admin message."""

    connection_id = fields.UUID(
        description="Connection identifier", required=True, example=UUIDFour.EXAMPLE
    )
    comment = fields.Str(
        description="Human-readable comment", required=False, allow_none=True
    )
    presentation_proposal = fields.Nested(
        V20PresProposalByFormatSchema(),
        required=True,
    )
    auto_present = fields.Boolean(
        description=(
            "Whether to respond automatically to presentation requests, building "
            "and presenting requested proof"
        ),
        required=False,
        default=False,
    )
    trace = fields.Bool(
        description="Whether to trace event (default false)",
        required=False,
        example=False,
    )


class V20PresRequestByFormatSchema(OpenAPISchema):
    """Presentation request per format."""

    indy = fields.Nested(
        IndyProofRequestSchema,
        required=False,
        description="Presentation request for indy",
    )
    dif = fields.Nested(
        DIFProofRequestSchema,
        required=False,
        description="Presentation request for DIF",
    )

    @validates_schema
    def validate_fields(self, data, **kwargs):
        """
        Validate schema fields: data must have at least one format.

        Args:
            data: The data to validate

        Raises:
            ValidationError: if data has no formats

        """
        if not any(f.api in data for f in V20PresFormat.Format):
            raise ValidationError(
                "V20PresRequestByFormatSchema requires indy, dif, or both"
            )


class V20PresCreateRequestRequestSchema(AdminAPIMessageTracingSchema):
    """Request schema for creating a proof request free of any connection."""

    presentation_request = fields.Nested(V20PresRequestByFormatSchema(), required=True)
    comment = fields.Str(required=False, allow_none=True)
    auto_verify = fields.Bool(
        description="Verifier choice to auto-verify proof presentation",
        required=False,
        example=False,
    )
    trace = fields.Bool(
        description="Whether to trace event (default false)",
        required=False,
        example=False,
    )


class V20PresSendRequestRequestSchema(V20PresCreateRequestRequestSchema):
    """Request schema for sending a proof request on a connection."""

    connection_id = fields.UUID(
        description="Connection identifier", required=True, example=UUIDFour.EXAMPLE
    )


class V20PresentationSendRequestToProposalSchema(AdminAPIMessageTracingSchema):
    """Request schema for sending a proof request bound to a proposal."""

    auto_verify = fields.Bool(
        description="Verifier choice to auto-verify proof presentation",
        required=False,
        example=False,
    )
    trace = fields.Bool(
        description="Whether to trace event (default false)",
        required=False,
        example=False,
    )


class V20PresSpecByFormatRequestSchema(AdminAPIMessageTracingSchema):
    """Presentation specification schema by format, for send-presentation request."""

    indy = fields.Nested(
        IndyPresSpecSchema,
        required=False,
        description="Presentation specification for indy",
    )
    dif = fields.Nested(
        DIFPresSpecSchema,
        required=False,
        description=(
            "Optional Presentation specification for DIF, "
            "overrides the PresentionExchange record's PresRequest"
        ),
    )

    @validates_schema
    def validate_fields(self, data, **kwargs):
        """
        Validate schema fields: specify exactly one format.

        Args:
            data: The data to validate

        Raises:
            ValidationError: if data does not have exactly one format.

        """
        if len(data.keys() & {f.api for f in V20PresFormat.Format}) != 1:
            raise ValidationError(
                "V20PresSpecByFormatRequestSchema must specify one presentation format"
            )


class V20CredentialsFetchQueryStringSchema(OpenAPISchema):
    """Parameters and validators for credentials fetch request query string."""

    referent = fields.Str(
        description="Proof request referents of interest, comma-separated",
        required=False,
        example="1_name_uuid,2_score_uuid",
    )
    start = fields.Str(
        description="Start index",
        required=False,
        strict=True,
        **NUM_STR_WHOLE,
    )
    count = fields.Str(
        description="Maximum number to retrieve",
        required=False,
        **NUM_STR_NATURAL,
    )
    extra_query = fields.Str(
        description="(JSON) object mapping referents to extra WQL queries",
        required=False,
        **INDY_EXTRA_WQL,
    )


class V20PresProblemReportRequestSchema(OpenAPISchema):
    """Request schema for sending problem report."""

    description = fields.Str(required=True)


class V20PresExIdMatchInfoSchema(OpenAPISchema):
    """Path parameters for request taking presentation exchange id."""

    pres_ex_id = fields.Str(
        description="Presentation exchange identifier", required=True, **UUID4
    )


async def _add_nonce(indy_proof_request: Mapping) -> Mapping:
    """Add nonce to indy proof request if need be."""

    if not indy_proof_request.get("nonce"):
        indy_proof_request["nonce"] = await generate_pr_nonce()
    return indy_proof_request


def _formats_attach(by_format: Mapping, msg_type: str, spec: str) -> Mapping:
    """Break out formats and proposals/requests/presentations for v2.0 messages."""
    attach = []
    for (fmt_api, item_by_fmt) in by_format.items():
        if fmt_api == V20PresFormat.Format.INDY.api:
            attach.append(
                AttachDecorator.data_base64(mapping=item_by_fmt, ident=fmt_api)
            )
        elif fmt_api == V20PresFormat.Format.DIF.api:
            attach.append(AttachDecorator.data_json(mapping=item_by_fmt, ident=fmt_api))
    return {
        "formats": [
            V20PresFormat(
                attach_id=fmt_api,
                format_=ATTACHMENT_FORMAT[msg_type][fmt_api],
            )
            for fmt_api in by_format
        ],
        f"{spec}_attach": attach,
    }


@docs(tags=["present-proof v2.0"], summary="Fetch all present-proof exchange records")
@querystring_schema(V20PresExRecordListQueryStringSchema)
@response_schema(V20PresExRecordListSchema(), 200, description="")
async def present_proof_list(request: web.BaseRequest):
    """
    Request handler for searching presentation exchange records.

    Args:
        request: aiohttp request object

    Returns:
        The presentation exchange list response

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
            records = await V20PresExRecord.query(
                session=session,
                tag_filter=tag_filter,
                post_filter_positive=post_filter,
            )
        results = [record.serialize() for record in records]
    except (StorageError, BaseModelError) as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    return web.json_response({"results": results})


@docs(
    tags=["present-proof v2.0"],
    summary="Fetch a single presentation exchange record",
)
@match_info_schema(V20PresExIdMatchInfoSchema())
@response_schema(V20PresExRecordSchema(), 200, description="")
async def present_proof_retrieve(request: web.BaseRequest):
    """
    Request handler for fetching a single presentation exchange record.

    Args:
        request: aiohttp request object

    Returns:
        The presentation exchange record response

    """
    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    pres_ex_id = request.match_info["pres_ex_id"]
    pres_ex_record = None
    try:
        async with profile.session() as session:
            pres_ex_record = await V20PresExRecord.retrieve_by_id(session, pres_ex_id)
        result = pres_ex_record.serialize()
    except StorageNotFoundError as err:
        # no such pres ex record: not protocol error, user fat-fingered id
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except (BaseModelError, StorageError) as err:
        # present but broken or hopeless: protocol error
        if pres_ex_record:
            async with profile.session() as session:
                await pres_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ABANDONED.value,
            web.HTTPBadRequest,
            pres_ex_record,
            outbound_handler,
        )

    return web.json_response(result)


@docs(
    tags=["present-proof v2.0"],
    summary="Fetch credentials from wallet for presentation request",
)
@match_info_schema(V20PresExIdMatchInfoSchema())
@querystring_schema(V20CredentialsFetchQueryStringSchema())
@response_schema(IndyCredPrecisSchema(many=True), 200, description="")
async def present_proof_credentials_list(request: web.BaseRequest):
    """
    Request handler for searching applicable credential records.

    Args:
        request: aiohttp request object

    Returns:
        The credential list response

    """
    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    pres_ex_id = request.match_info["pres_ex_id"]
    referents = request.query.get("referent")
    pres_referents = (r.strip() for r in referents.split(",")) if referents else ()

    try:
        async with profile.session() as session:
            pres_ex_record = await V20PresExRecord.retrieve_by_id(session, pres_ex_id)
    except StorageNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err

    start = request.query.get("start")
    count = request.query.get("count")

    # url encoded json extra_query
    encoded_extra_query = request.query.get("extra_query") or "{}"
    extra_query = json.loads(encoded_extra_query)

    # defaults
    start = int(start) if isinstance(start, str) else 0
    count = int(count) if isinstance(count, str) else 10

    indy_holder = profile.inject(IndyHolder)
    indy_credentials = []
    # INDY
    try:
        indy_pres_request = pres_ex_record.by_format["pres_request"].get(
            V20PresFormat.Format.INDY.api
        )
        if indy_pres_request:
            indy_credentials = (
                await indy_holder.get_credentials_for_presentation_request_by_referent(
                    indy_pres_request,
                    pres_referents,
                    start,
                    count,
                    extra_query,
                )
            )
    except IndyHolderError as err:
        if pres_ex_record:
            async with profile.session() as session:
                await pres_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ABANDONED.value,
            web.HTTPBadRequest,
            pres_ex_record,
            outbound_handler,
        )

    dif_holder = profile.inject(VCHolder)
    dif_credentials = []
    dif_cred_value_list = []
    # DIF
    try:
        dif_pres_request = pres_ex_record.by_format["pres_request"].get(
            V20PresFormat.Format.DIF.api
        )
        if dif_pres_request:
            input_descriptors_list = dif_pres_request.get(
                "presentation_definition", {}
            ).get("input_descriptors")
            claim_fmt = dif_pres_request.get("presentation_definition", {}).get(
                "format"
            )
            if claim_fmt and len(claim_fmt.keys()) > 0:
                claim_fmt = ClaimFormat.deserialize(claim_fmt)
            input_descriptors = []
            for input_desc_dict in input_descriptors_list:
                input_descriptors.append(InputDescriptors.deserialize(input_desc_dict))
            record_ids = set()
            for input_descriptor in input_descriptors:
                proof_type = None
                limit_disclosure = input_descriptor.constraint.limit_disclosure and (
                    input_descriptor.constraint.limit_disclosure == "required"
                )
                uri_list = []
                one_of_uri_groups = []
                if input_descriptor.schemas:
                    if input_descriptor.schemas.oneof_filter:
                        one_of_uri_groups = await retrieve_uri_list_from_schema_filter(
                            input_descriptor.schemas.uri_groups
                        )
                    else:
                        schema_uris = input_descriptor.schemas.uri_groups[0]
                        for schema_uri in schema_uris:
                            if schema_uri.required is None:
                                required = True
                            else:
                                required = schema_uri.required
                            if required:
                                uri_list.append(schema_uri.uri)
                if len(uri_list) == 0:
                    uri_list = None
                if len(one_of_uri_groups) == 0:
                    one_of_uri_groups = None
                if limit_disclosure:
                    proof_type = [BbsBlsSignature2020.signature_type]
                if claim_fmt:
                    if claim_fmt.ldp_vp:
                        if "proof_type" in claim_fmt.ldp_vp:
                            proof_types = claim_fmt.ldp_vp.get("proof_type")
                            if limit_disclosure and (
                                BbsBlsSignature2020.signature_type not in proof_types
                            ):
                                raise web.HTTPBadRequest(
                                    reason=(
                                        "Verifier submitted presentation request with "
                                        "limit_disclosure [selective disclosure] "
                                        "option but verifier does not support "
                                        "BbsBlsSignature2020 format"
                                    )
                                )
                            elif (
                                len(proof_types) == 1
                                and (
                                    BbsBlsSignature2020.signature_type
                                    not in proof_types
                                )
                                and (
                                    Ed25519Signature2018.signature_type
                                    not in proof_types
                                )
                            ):
                                raise web.HTTPBadRequest(
                                    reason=(
                                        "Only BbsBlsSignature2020 and/or "
                                        "Ed25519Signature2018 signature types "
                                        "are supported"
                                    )
                                )
                            elif (
                                len(proof_types) >= 2
                                and (
                                    BbsBlsSignature2020.signature_type
                                    not in proof_types
                                )
                                and (
                                    Ed25519Signature2018.signature_type
                                    not in proof_types
                                )
                            ):
                                raise web.HTTPBadRequest(
                                    reason=(
                                        "Only BbsBlsSignature2020 and "
                                        "Ed25519Signature2018 signature types "
                                        "are supported"
                                    )
                                )
                            else:
                                for proof_format in proof_types:
                                    if (
                                        proof_format
                                        == Ed25519Signature2018.signature_type
                                    ):
                                        proof_type = [
                                            Ed25519Signature2018.signature_type
                                        ]
                                        break
                                    elif (
                                        proof_format
                                        == BbsBlsSignature2020.signature_type
                                    ):
                                        proof_type = [
                                            BbsBlsSignature2020.signature_type
                                        ]
                                        break
                    else:
                        raise web.HTTPBadRequest(
                            reason=(
                                "Currently, only ldp_vp with "
                                "BbsBlsSignature2020 and Ed25519Signature2018"
                                " signature types are supported"
                            )
                        )
                if one_of_uri_groups:
                    records = []
                    cred_group_record_ids = set()
                    for uri_group in one_of_uri_groups:
                        search = dif_holder.search_credentials(
                            proof_types=proof_type, pd_uri_list=uri_group
                        )
                        cred_group = await search.fetch(count)
                        (
                            cred_group_vcrecord_list,
                            cred_group_vcrecord_ids_set,
                        ) = await process_vcrecords_return_list(
                            cred_group, cred_group_record_ids
                        )
                        cred_group_record_ids = cred_group_vcrecord_ids_set
                        records = records + cred_group_vcrecord_list
                else:
                    search = dif_holder.search_credentials(
                        proof_types=proof_type,
                        pd_uri_list=uri_list,
                    )
                    records = await search.fetch(count)
                # Avoiding addition of duplicate records
                vcrecord_list, vcrecord_ids_set = await process_vcrecords_return_list(
                    records, record_ids
                )
                record_ids = vcrecord_ids_set
                dif_credentials = dif_credentials + vcrecord_list
            for dif_credential in dif_credentials:
                cred_value = dif_credential.cred_value
                cred_value["record_id"] = dif_credential.record_id
                dif_cred_value_list.append(cred_value)
    except (
        StorageNotFoundError,
        V20PresFormatHandlerError,
    ) as err:
        if pres_ex_record:
            async with profile.session() as session:
                await pres_ex_record.save_error_state(session, reason=err.roll_up)
        await report_problem(
            err,
            ProblemReportReason.ABANDONED.value,
            web.HTTPBadRequest,
            pres_ex_record,
            outbound_handler,
        )
    credentials = list(indy_credentials) + dif_cred_value_list
    return web.json_response(credentials)


async def process_vcrecords_return_list(
    vc_records: Sequence[VCRecord], record_ids: set
) -> Tuple[Sequence[VCRecord], set]:
    """Return list of non-duplicate VCRecords."""
    to_add = []
    for vc_record in vc_records:
        if vc_record.record_id not in record_ids:
            to_add.append(vc_record)
            record_ids.add(vc_record.record_id)
    return (to_add, record_ids)


async def retrieve_uri_list_from_schema_filter(
    schema_uri_groups: Sequence[Sequence[SchemaInputDescriptor]],
) -> Sequence[str]:
    """Retrieve list of schema uri from uri_group."""
    group_schema_uri_list = []
    for schema_group in schema_uri_groups:
        uri_list = []
        for schema in schema_group:
            uri_list.append(schema.uri)
        if len(uri_list) > 0:
            group_schema_uri_list.append(uri_list)
    return group_schema_uri_list


@docs(tags=["present-proof v2.0"], summary="Sends a presentation proposal")
@request_schema(V20PresProposalRequestSchema())
@response_schema(V20PresExRecordSchema(), 200, description="")
async def present_proof_send_proposal(request: web.BaseRequest):
    """
    Request handler for sending a presentation proposal.

    Args:
        request: aiohttp request object

    Returns:
        The presentation exchange details

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json()

    comment = body.get("comment")
    connection_id = body.get("connection_id")

    pres_proposal = body.get("presentation_proposal")
    conn_record = None
    try:
        async with profile.session() as session:
            conn_record = await ConnRecord.retrieve_by_id(session, connection_id)
        pres_proposal_message = V20PresProposal(
            comment=comment,
            **_formats_attach(pres_proposal, PRES_20_PROPOSAL, "proposals"),
        )
    except (BaseModelError, StorageError) as err:
        # other party does not care about our false protocol start
        raise web.HTTPBadRequest(reason=err.roll_up)

    if not conn_record.is_ready:
        raise web.HTTPForbidden(reason=f"Connection {connection_id} not ready")

    trace_msg = body.get("trace")
    pres_proposal_message.assign_trace_decorator(
        context.settings,
        trace_msg,
    )
    auto_present = body.get(
        "auto_present", context.settings.get("debug.auto_respond_presentation_request")
    )

    pres_manager = V20PresManager(profile)
    pres_ex_record = None
    try:
        pres_ex_record = await pres_manager.create_exchange_for_proposal(
            connection_id=connection_id,
            pres_proposal_message=pres_proposal_message,
            auto_present=auto_present,
        )
        result = pres_ex_record.serialize()
    except (BaseModelError, StorageError) as err:
        if pres_ex_record:
            async with profile.session() as session:
                await pres_ex_record.save_error_state(session, reason=err.roll_up)
        # other party does not care about our false protocol start
        raise web.HTTPBadRequest(reason=err.roll_up)

    await outbound_handler(pres_proposal_message, connection_id=connection_id)

    trace_event(
        context.settings,
        pres_proposal_message,
        outcome="presentation_exchange_propose.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["present-proof v2.0"],
    summary="Creates a presentation request not bound to any proposal or connection",
)
@request_schema(V20PresCreateRequestRequestSchema())
@response_schema(V20PresExRecordSchema(), 200, description="")
async def present_proof_create_request(request: web.BaseRequest):
    """
    Request handler for creating a free presentation request.

    The presentation request will not be bound to any proposal
    or existing connection.

    Args:
        request: aiohttp request object

    Returns:
        The presentation exchange details

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json()

    comment = body.get("comment")
    pres_request_spec = body.get("presentation_request")
    if pres_request_spec and V20PresFormat.Format.INDY.api in pres_request_spec:
        await _add_nonce(pres_request_spec[V20PresFormat.Format.INDY.api])

    pres_request_message = V20PresRequest(
        comment=comment,
        will_confirm=True,
        **_formats_attach(pres_request_spec, PRES_20_REQUEST, "request_presentations"),
    )
    auto_verify = body.get(
        "auto_verify", context.settings.get("debug.auto_verify_presentation")
    )
    trace_msg = body.get("trace")
    pres_request_message.assign_trace_decorator(
        context.settings,
        trace_msg,
    )

    pres_manager = V20PresManager(profile)
    pres_ex_record = None
    try:
        pres_ex_record = await pres_manager.create_exchange_for_request(
            connection_id=None,
            pres_request_message=pres_request_message,
            auto_verify=auto_verify,
        )
        result = pres_ex_record.serialize()
    except (BaseModelError, StorageError) as err:
        if pres_ex_record:
            async with profile.session() as session:
                await pres_ex_record.save_error_state(session, reason=err.roll_up)
        # other party does not care about our false protocol start
        raise web.HTTPBadRequest(reason=err.roll_up)

    await outbound_handler(pres_request_message, connection_id=None)

    trace_event(
        context.settings,
        pres_request_message,
        outcome="presentation_exchange_create_request.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["present-proof v2.0"],
    summary="Sends a free presentation request not bound to any proposal",
)
@request_schema(V20PresSendRequestRequestSchema())
@response_schema(V20PresExRecordSchema(), 200, description="")
async def present_proof_send_free_request(request: web.BaseRequest):
    """
    Request handler for sending a presentation request free from any proposal.

    Args:
        request: aiohttp request object

    Returns:
        The presentation exchange details

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json()

    connection_id = body.get("connection_id")
    try:
        async with profile.session() as session:
            conn_record = await ConnRecord.retrieve_by_id(session, connection_id)
    except StorageNotFoundError as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    if not conn_record.is_ready:
        raise web.HTTPForbidden(reason=f"Connection {connection_id} not ready")

    comment = body.get("comment")
    pres_request_spec = body.get("presentation_request")
    if pres_request_spec and V20PresFormat.Format.INDY.api in pres_request_spec:
        await _add_nonce(pres_request_spec[V20PresFormat.Format.INDY.api])
    pres_request_message = V20PresRequest(
        comment=comment,
        will_confirm=True,
        **_formats_attach(pres_request_spec, PRES_20_REQUEST, "request_presentations"),
    )
    auto_verify = body.get(
        "auto_verify", context.settings.get("debug.auto_verify_presentation")
    )
    trace_msg = body.get("trace")
    pres_request_message.assign_trace_decorator(
        context.settings,
        trace_msg,
    )

    pres_manager = V20PresManager(profile)
    pres_ex_record = None
    try:
        pres_ex_record = await pres_manager.create_exchange_for_request(
            connection_id=connection_id,
            pres_request_message=pres_request_message,
            auto_verify=auto_verify,
        )
        result = pres_ex_record.serialize()
    except (BaseModelError, StorageError) as err:
        if pres_ex_record:
            async with profile.session() as session:
                await pres_ex_record.save_error_state(session, reason=err.roll_up)
        # other party does not care about our false protocol start
        raise web.HTTPBadRequest(reason=err.roll_up)

    await outbound_handler(pres_request_message, connection_id=connection_id)

    trace_event(
        context.settings,
        pres_request_message,
        outcome="presentation_exchange_send_request.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["present-proof v2.0"],
    summary="Sends a presentation request in reference to a proposal",
)
@match_info_schema(V20PresExIdMatchInfoSchema())
@request_schema(V20PresentationSendRequestToProposalSchema())
@response_schema(V20PresExRecordSchema(), 200, description="")
async def present_proof_send_bound_request(request: web.BaseRequest):
    """
    Request handler for sending a presentation request bound to a proposal.

    Args:
        request: aiohttp request object

    Returns:
        The presentation exchange details

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    body = await request.json()

    pres_ex_id = request.match_info["pres_ex_id"]
    pres_ex_record = None
    try:
        async with profile.session() as session:
            pres_ex_record = await V20PresExRecord.retrieve_by_id(session, pres_ex_id)
    except StorageNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err

    if pres_ex_record.state != (V20PresExRecord.STATE_PROPOSAL_RECEIVED):
        raise web.HTTPBadRequest(
            reason=(
                f"Presentation exchange {pres_ex_id} "
                f"in {pres_ex_record.state} state "
                f"(must be {V20PresExRecord.STATE_PROPOSAL_RECEIVED})"
            )
        )
    connection_id = pres_ex_record.connection_id

    try:
        async with profile.session() as session:
            conn_record = await ConnRecord.retrieve_by_id(session, connection_id)
    except StorageError as err:
        raise web.HTTPBadRequest(reason=err.roll_up) from err

    if not conn_record.is_ready:
        raise web.HTTPForbidden(reason=f"Connection {connection_id} not ready")

    pres_ex_record.auto_verify = body.get(
        "auto_verify", context.settings.get("debug.auto_verify_presentation")
    )
    pres_manager = V20PresManager(profile)
    try:
        (
            pres_ex_record,
            pres_request_message,
        ) = await pres_manager.create_bound_request(pres_ex_record)
        result = pres_ex_record.serialize()
    except (BaseModelError, LedgerError, StorageError) as err:
        if pres_ex_record:
            async with profile.session() as session:
                await pres_ex_record.save_error_state(session, reason=err.roll_up)
        # other party cares that we cannot continue protocol
        await report_problem(
            err,
            ProblemReportReason.ABANDONED.value,
            web.HTTPBadRequest,
            pres_ex_record,
            outbound_handler,
        )

    trace_msg = body.get("trace")
    pres_request_message.assign_trace_decorator(
        context.settings,
        trace_msg,
    )
    await outbound_handler(pres_request_message, connection_id=connection_id)

    trace_event(
        context.settings,
        pres_request_message,
        outcome="presentation_exchange_send_request.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(tags=["present-proof v2.0"], summary="Sends a proof presentation")
@match_info_schema(V20PresExIdMatchInfoSchema())
@request_schema(V20PresSpecByFormatRequestSchema())
@response_schema(V20PresExRecordSchema(), description="")
async def present_proof_send_presentation(request: web.BaseRequest):
    """
    Request handler for sending a presentation.

    Args:
        request: aiohttp request object

    Returns:
        The presentation exchange details

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]
    pres_ex_id = request.match_info["pres_ex_id"]
    body = await request.json()
    if "dif" in body:
        fmt = V20PresFormat.Format.get("dif").api
    elif "indy" in body:
        fmt = V20PresFormat.Format.get("indy").api
    else:
        raise web.HTTPBadRequest(
            reason=(
                "No presentation format specification provided, "
                "either dif or indy must be included. "
                "In case of DIF, if no additional specification "
                'needs to be provided then include "dif": {}'
            )
        )
    comment = body.get("comment")
    pres_ex_record = None
    try:
        async with profile.session() as session:
            pres_ex_record = await V20PresExRecord.retrieve_by_id(session, pres_ex_id)
    except StorageNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err

    if pres_ex_record.state != (V20PresExRecord.STATE_REQUEST_RECEIVED):
        raise web.HTTPBadRequest(
            reason=(
                f"Presentation exchange {pres_ex_id} "
                f"in {pres_ex_record.state} state "
                f"(must be {V20PresExRecord.STATE_REQUEST_RECEIVED})"
            )
        )

    # Fetch connection if exchange has record
    conn_record = None
    if pres_ex_record.connection_id:
        try:
            async with profile.session() as session:

                conn_record = await ConnRecord.retrieve_by_id(
                    session, pres_ex_record.connection_id
                )
        except StorageNotFoundError as err:
            raise web.HTTPBadRequest(reason=err.roll_up) from err

    if conn_record and not conn_record.is_ready:
        raise web.HTTPForbidden(
            reason=f"Connection {pres_ex_record.connection_id} not ready"
        )

    pres_manager = V20PresManager(profile)
    try:
        request_data = {fmt: body.get(fmt)}
        pres_ex_record, pres_message = await pres_manager.create_pres(
            pres_ex_record,
            request_data=request_data,
            comment=comment,
        )
        result = pres_ex_record.serialize()
    except (
        BaseModelError,
        IndyHolderError,
        LedgerError,
        V20PresFormatHandlerError,
        StorageError,
        WalletNotFoundError,
    ) as err:
        async with profile.session() as session:
            await pres_ex_record.save_error_state(session, reason=err.roll_up)
        # other party cares that we cannot continue protocol
        await report_problem(
            err,
            ProblemReportReason.ABANDONED.value,
            web.HTTPBadRequest,
            pres_ex_record,
            outbound_handler,
        )
    trace_msg = body.get("trace")
    pres_message.assign_trace_decorator(
        context.settings,
        trace_msg,
    )
    await outbound_handler(pres_message, connection_id=pres_ex_record.connection_id)

    trace_event(
        context.settings,
        pres_message,
        outcome="presentation_exchange_send_request.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(tags=["present-proof v2.0"], summary="Verify a received presentation")
@match_info_schema(V20PresExIdMatchInfoSchema())
@response_schema(V20PresExRecordSchema(), description="")
async def present_proof_verify_presentation(request: web.BaseRequest):
    """
    Request handler for verifying a presentation request.

    Args:
        request: aiohttp request object

    Returns:
        The presentation exchange details

    """
    r_time = get_timer()

    context: AdminRequestContext = request["context"]
    profile = context.profile
    outbound_handler = request["outbound_message_router"]

    pres_ex_id = request.match_info["pres_ex_id"]

    pres_ex_record = None
    try:
        async with profile.session() as session:
            pres_ex_record = await V20PresExRecord.retrieve_by_id(session, pres_ex_id)
    except StorageNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err

    if pres_ex_record.state != (V20PresExRecord.STATE_PRESENTATION_RECEIVED):
        raise web.HTTPBadRequest(
            reason=(
                f"Presentation exchange {pres_ex_id} "
                f"in {pres_ex_record.state} state "
                f"(must be {V20PresExRecord.STATE_PRESENTATION_RECEIVED})"
            )
        )

    pres_manager = V20PresManager(profile)
    try:
        pres_ex_record = await pres_manager.verify_pres(pres_ex_record)
        result = pres_ex_record.serialize()
    except (BaseModelError, LedgerError, StorageError) as err:
        if pres_ex_record:
            async with profile.session() as session:
                await pres_ex_record.save_error_state(session, reason=err.roll_up)
        # other party cares that we cannot continue protocol
        await report_problem(
            err,
            ProblemReportReason.ABANDONED.value,
            web.HTTPBadRequest,
            pres_ex_record,
            outbound_handler,
        )

    trace_event(
        context.settings,
        pres_ex_record,
        outcome="presentation_exchange_verify.END",
        perf_counter=r_time,
    )

    return web.json_response(result)


@docs(
    tags=["present-proof v2.0"],
    summary="Send a problem report for presentation exchange",
)
@match_info_schema(V20PresExIdMatchInfoSchema())
@request_schema(V20PresProblemReportRequestSchema())
@response_schema(V20PresentProofModuleResponseSchema(), 200, description="")
async def present_proof_problem_report(request: web.BaseRequest):
    """
    Request handler for sending problem report.

    Args:
        request: aiohttp request object

    """
    context: AdminRequestContext = request["context"]
    outbound_handler = request["outbound_message_router"]

    pres_ex_id = request.match_info["pres_ex_id"]
    body = await request.json()
    description = body["description"]

    try:
        async with context.profile.session() as session:
            pres_ex_record = await V20PresExRecord.retrieve_by_id(session, pres_ex_id)
            await pres_ex_record.save_error_state(
                session,
                reason=f"created problem report: {description}",
            )
        report = problem_report_for_record(pres_ex_record, description)
    except StorageNotFoundError as err:  # other party does not care about meta-problems
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except StorageError as err:
        raise web.HTTPBadRequest(reason=err.roll_up)

    await outbound_handler(report, connection_id=pres_ex_record.connection_id)

    return web.json_response({})


@docs(
    tags=["present-proof v2.0"],
    summary="Remove an existing presentation exchange record",
)
@match_info_schema(V20PresExIdMatchInfoSchema())
@response_schema(V20PresentProofModuleResponseSchema(), description="")
async def present_proof_remove(request: web.BaseRequest):
    """
    Request handler for removing a presentation exchange record.

    Args:
        request: aiohttp request object

    """
    context: AdminRequestContext = request["context"]

    pres_ex_id = request.match_info["pres_ex_id"]
    pres_ex_record = None
    try:
        async with context.profile.session() as session:
            try:
                pres_ex_record = await V20PresExRecord.retrieve_by_id(
                    session, pres_ex_id
                )
                await pres_ex_record.delete_record(session)
            except (BaseModelError, ValidationError):
                storage = session.inject(BaseStorage)
                storage_record = await storage.get_record(
                    record_type=V20PresExRecord.RECORD_TYPE, record_id=pres_ex_id
                )
                await storage.delete_record(storage_record)
    except StorageNotFoundError as err:
        raise web.HTTPNotFound(reason=err.roll_up) from err
    except StorageError as err:
        raise web.HTTPBadRequest(reason=err.roll_up)

    return web.json_response({})


async def register(app: web.Application):
    """Register routes."""

    app.add_routes(
        [
            web.get(
                "/present-proof-2.0/records",
                present_proof_list,
                allow_head=False,
            ),
            web.get(
                "/present-proof-2.0/records/{pres_ex_id}",
                present_proof_retrieve,
                allow_head=False,
            ),
            web.get(
                "/present-proof-2.0/records/{pres_ex_id}/credentials",
                present_proof_credentials_list,
                allow_head=False,
            ),
            web.post(
                "/present-proof-2.0/send-proposal",
                present_proof_send_proposal,
            ),
            web.post(
                "/present-proof-2.0/create-request",
                present_proof_create_request,
            ),
            web.post(
                "/present-proof-2.0/send-request",
                present_proof_send_free_request,
            ),
            web.post(
                "/present-proof-2.0/records/{pres_ex_id}/send-request",
                present_proof_send_bound_request,
            ),
            web.post(
                "/present-proof-2.0/records/{pres_ex_id}/send-presentation",
                present_proof_send_presentation,
            ),
            web.post(
                "/present-proof-2.0/records/{pres_ex_id}/verify-presentation",
                present_proof_verify_presentation,
            ),
            web.post(
                "/present-proof-2.0/records/{pres_ex_id}/problem-report",
                present_proof_problem_report,
            ),
            web.delete(
                "/present-proof-2.0/records/{pres_ex_id}",
                present_proof_remove,
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
            "name": "present-proof v2.0",
            "description": "Proof presentation v2.0",
            "externalDocs": {"description": "Specification", "url": SPEC_URI},
        }
    )
