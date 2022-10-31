"""Standard packed message format classes."""

import json
import logging
from typing import List, Sequence, Tuple, Union

from ..core.profile import ProfileSession

from ..protocols.routing.v1_0.messages.forward import Forward

from ..messaging.util import time_now
from ..utils.task_queue import TaskQueue
from ..wallet.base import BaseWallet
from ..wallet.error import WalletError
from ..wallet.util import b64_to_str

from .error import WireFormatParseError, WireFormatEncodeError, RecipientKeysError
from .inbound.receipt import MessageReceipt
from .wire_format import BaseWireFormat

LOGGER = logging.getLogger(__name__)


class PackWireFormat(BaseWireFormat):
    """Standard DIDComm message parser and serializer."""

    def __init__(self):
        """Initialize the pack wire format instance."""
        super().__init__()
        self.task_queue: TaskQueue = None

    async def parse_message(
        self,
        session: ProfileSession,
        message_body: Union[str, bytes],
    ) -> Tuple[dict, MessageReceipt]:
        """
        Deserialize an incoming message and further populate the request context.

        Args:
            session: The profile session for providing wallet access
            message_body: The body of the message

        Returns:
            A tuple of the parsed message and a message receipt instance

        Raises:
            WireFormatParseError: If the JSON parsing failed
            WireFormatParseError: If a wallet is required but can't be located

        """

        receipt = MessageReceipt()
        receipt.in_time = time_now()
        receipt.raw_message = message_body

        message_dict = None
        message_json = message_body

        if not message_json:
            raise WireFormatParseError("Message body is empty")

        try:
            message_dict = json.loads(message_json)
        except ValueError:
            raise WireFormatParseError("Message JSON parsing failed")
        if not isinstance(message_dict, dict):
            raise WireFormatParseError("Message JSON result is not an object")

        # packed messages are detected by the absence of @type
        if "@type" not in message_dict:

            try:
                unpack = self.unpack(session, message_body, receipt)
                message_json = await (
                    self.task_queue and self.task_queue.run(unpack) or unpack
                )
            except WireFormatParseError:
                LOGGER.debug("Message unpack failed, falling back to JSON")
            else:
                receipt.raw_message = message_json
                try:
                    message_dict = json.loads(message_json)
                except ValueError:
                    raise WireFormatParseError("Message JSON parsing failed")
                if not isinstance(message_dict, dict):
                    raise WireFormatParseError("Message JSON result is not an object")

        # parse thread ID
        thread_dec = message_dict.get("~thread")
        receipt.thread_id = (
            thread_dec and thread_dec.get("thid") or message_dict.get("@id")
        )
        receipt.parent_thread_id = thread_dec.get("pthid") if thread_dec else None

        # handle transport decorator
        transport_dec = message_dict.get("~transport")
        if transport_dec:
            receipt.direct_response_mode = transport_dec.get("return_route")

        LOGGER.debug(f"Expanded message: {message_dict}")

        return message_dict, receipt

    async def unpack(
        self,
        session: ProfileSession,
        message_body: Union[str, bytes],
        receipt: MessageReceipt,
    ):
        """Look up the wallet instance and perform the message unpack."""
        wallet = session.inject_or(BaseWallet)
        if not wallet:
            raise WireFormatParseError("Wallet not defined in profile session")

        try:
            unpacked = await wallet.unpack_message(message_body)
            (
                message_json,
                receipt.sender_verkey,
                receipt.recipient_verkey,
            ) = unpacked
            return message_json
        except WalletError as e:
            raise WireFormatParseError("Message unpack failed") from e

    async def encode_message(
        self,
        session: ProfileSession,
        message_json: Union[str, bytes],
        recipient_keys: Sequence[str],
        routing_keys: Sequence[str],
        sender_key: str,
    ) -> Union[str, bytes]:
        """
        Encode an outgoing message for transport.

        Args:
            session: The profile session for providing wallet access
            message_json: The message body to serialize
            recipient_keys: A sequence of recipient verkeys
            routing_keys: A sequence of routing verkeys
            sender_key: The verification key of the sending agent

        Returns:
            The encoded message

        Raises:
            MessageEncodeError: If the message could not be encoded

        """

        if sender_key and recipient_keys:
            pack = self.pack(
                session, message_json, recipient_keys, routing_keys, sender_key
            )
            message = await (self.task_queue and self.task_queue.run(pack) or pack)
        else:
            message = message_json
        return message

    async def pack(
        self,
        session: ProfileSession,
        message_json: Union[str, bytes],
        recipient_keys: Sequence[str],
        routing_keys: Sequence[str],
        sender_key: str,
    ):
        """Look up the wallet instance and perform the message pack."""
        if not sender_key or not recipient_keys:
            raise WireFormatEncodeError("Cannot pack message without associated keys")

        wallet = session.inject_or(BaseWallet)
        if not wallet:
            raise WireFormatEncodeError("No wallet instance")

        try:
            message = await wallet.pack_message(
                message_json, recipient_keys, sender_key
            )
        except WalletError as e:
            raise WireFormatEncodeError("Message pack failed") from e

        if routing_keys:
            recip_keys = recipient_keys
            for router_key in routing_keys:
                message = json.loads(message.decode("utf-8"))
                fwd_msg = Forward(to=recip_keys[0], msg=message)
                # Forwards are anon packed
                recip_keys = [router_key]
                try:
                    message = await wallet.pack_message(fwd_msg.to_json(), recip_keys)
                except WalletError as e:
                    raise WireFormatEncodeError("Forward message pack failed") from e
        return message

    def get_recipient_keys(self, message_body: Union[str, bytes]) -> List[str]:
        """
        Get all recipient keys from a wire message.

        Args:
            message_body: The body of the message

        Returns:
            List of recipient keys from the message body

        Raises:
            RecipientKeysError: If the recipient keys could not be extracted

        """

        try:
            message_dict = json.loads(message_body)
            protected = json.loads(b64_to_str(message_dict["protected"], urlsafe=True))
            recipients = protected["recipients"]

            recipient_keys = [recipient["header"]["kid"] for recipient in recipients]
        except Exception as e:
            raise RecipientKeysError(
                "Error trying to extract recipient keys from JWE", e
            )

        return recipient_keys
