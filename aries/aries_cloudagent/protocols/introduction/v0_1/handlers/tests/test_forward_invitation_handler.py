from asynctest import TestCase as AsyncTestCase
from asynctest import mock as async_mock

from ......connections.models.conn_record import ConnRecord
from ......messaging.base_handler import HandlerException
from ......messaging.request_context import RequestContext
from ......messaging.responder import MockResponder
from ......protocols.connections.v1_0.messages.connection_invitation import (
    ConnectionInvitation,
)

from ...messages.forward_invitation import ForwardInvitation

from .. import forward_invitation_handler as test_module

TEST_DID = "55GkHamhTU1ZbTbV2ab9DE"
TEST_VERKEY = "3Dn1SJNPaCXcvvJvSbsFWP2xaCjMom3can8CQNhWrTRx"
TEST_ROUTE_VERKEY = "9WCgWKUaAJj3VWxxtzvvMQN3AoFxoBtBDo9ntwJnVVCC"
TEST_LABEL = "Label"
TEST_ENDPOINT = "http://localhost"
TEST_IMAGE_URL = "http://aries.ca/images/sample.png"


class TestForwardInvitationHandler(AsyncTestCase):
    async def setUp(self):
        self.context = RequestContext.test_context()

        self.context.connection_ready = True
        self.context.message = ForwardInvitation(
            invitation=ConnectionInvitation(
                label=TEST_LABEL,
                did=TEST_DID,
                recipient_keys=[TEST_VERKEY],
                endpoint=TEST_ENDPOINT,
                routing_keys=[TEST_ROUTE_VERKEY],
                image_url=TEST_IMAGE_URL,
            ),
            message="Hello World",
        )

    async def test_handle(self):
        handler = test_module.ForwardInvitationHandler()

        responder = MockResponder()
        with async_mock.patch.object(
            test_module, "ConnectionManager", autospec=True
        ) as mock_mgr:
            mock_mgr.return_value.receive_invitation = async_mock.CoroutineMock(
                return_value=ConnRecord(connection_id="dummy")
            )

            await handler.handle(self.context, responder)
            assert not (responder.messages)

    async def test_handle_x(self):
        handler = test_module.ForwardInvitationHandler()

        responder = MockResponder()
        with async_mock.patch.object(
            test_module, "ConnectionManager", autospec=True
        ) as mock_mgr:
            mock_mgr.return_value.receive_invitation = async_mock.CoroutineMock(
                side_effect=test_module.ConnectionManagerError("oops")
            )

            await handler.handle(self.context, responder)
            messages = responder.messages
            assert len(messages) == 1
            (result, _) = messages[0]
            assert type(result) == test_module.ProblemReport

    async def test_handle_not_ready(self):
        handler = test_module.ForwardInvitationHandler()
        self.context.connection_ready = False

        with self.assertRaises(HandlerException):
            await handler.handle(self.context, None)
