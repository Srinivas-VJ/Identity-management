"""Revoke message."""

from marshmallow import fields, validate
from .....messaging.agent_message import AgentMessage, AgentMessageSchema
from .....messaging.decorators.please_ack_decorator import (
    PleaseAckDecorator,
    PleaseAckDecoratorSchema,
)
from .....messaging.valid import UUIDFour
from ..message_types import PROTOCOL_PACKAGE, REVOKE

HANDLER_CLASS = f"{PROTOCOL_PACKAGE}.handlers.revoke_handler.RevokeHandler"


class Revoke(AgentMessage):
    """Class representing revoke message."""

    class Meta:
        """Revoke Meta."""

        handler_class = HANDLER_CLASS
        message_type = REVOKE
        schema_class = "RevokeSchema"

    def __init__(
        self,
        *,
        revocation_format: str,
        credential_id: str,
        please_ack: PleaseAckDecorator = None,
        comment: str = None,
        **kwargs,
    ):
        """Initialize revoke message."""
        super().__init__(**kwargs)
        self.revocation_format = revocation_format
        self.credential_id = credential_id
        self.comment = comment


class RevokeSchema(AgentMessageSchema):
    """Schema of Revoke message."""

    class Meta:
        """RevokeSchema Meta."""

        model_class = Revoke

    revocation_format = fields.Str(
        required=True,
        description=("The format of the credential revocation ID"),
        example="indy-anoncreds",
        validate=validate.OneOf(["indy-anoncreds"]),
    )
    credential_id = fields.Str(
        required=True,
        description=("Credential ID of the issued credential to be revoked"),
        example=UUIDFour.EXAMPLE,
    )
    please_ack = fields.Nested(
        PleaseAckDecoratorSchema,
        required=False,
        description="Whether or not the holder should acknowledge receipt",
        data_key="~please_ack",
    )
    comment = fields.Str(
        required=False,
        description="Human readable information about revocation notification",
    )
