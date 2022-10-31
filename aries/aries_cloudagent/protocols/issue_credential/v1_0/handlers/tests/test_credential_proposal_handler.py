from asynctest import mock as async_mock, TestCase as AsyncTestCase

from ......messaging.request_context import RequestContext
from ......messaging.responder import MockResponder
from ......transport.inbound.receipt import MessageReceipt

from ...messages.credential_proposal import CredentialProposal

from .. import credential_proposal_handler as test_module


class TestCredentialProposalHandler(AsyncTestCase):
    async def test_called(self):
        request_context = RequestContext.test_context()
        request_context.message_receipt = MessageReceipt()
        request_context.connection_record = async_mock.MagicMock()

        with async_mock.patch.object(
            test_module, "CredentialManager", autospec=True
        ) as mock_cred_mgr:
            mock_cred_mgr.return_value.receive_proposal = async_mock.CoroutineMock(
                return_value=async_mock.MagicMock()
            )
            mock_cred_mgr.return_value.receive_proposal.return_value.auto_offer = False
            request_context.message = CredentialProposal()
            request_context.connection_ready = True
            handler = test_module.CredentialProposalHandler()
            responder = MockResponder()
            await handler.handle(request_context, responder)

        mock_cred_mgr.assert_called_once_with(request_context.profile)
        mock_cred_mgr.return_value.receive_proposal.assert_called_once_with(
            request_context.message, request_context.connection_record.connection_id
        )
        assert not responder.messages

    async def test_called_auto_offer(self):
        request_context = RequestContext.test_context()
        request_context.message_receipt = MessageReceipt()
        request_context.connection_record = async_mock.MagicMock()

        with async_mock.patch.object(
            test_module, "CredentialManager", autospec=True
        ) as mock_cred_mgr:
            mock_cred_mgr.return_value.receive_proposal = async_mock.CoroutineMock(
                return_value=async_mock.MagicMock()
            )
            mock_cred_mgr.return_value.receive_proposal.return_value.auto_offer = True
            mock_cred_mgr.return_value.create_offer = async_mock.CoroutineMock(
                return_value=(None, "credential_offer_message")
            )
            request_context.message = CredentialProposal()
            request_context.connection_ready = True
            handler = test_module.CredentialProposalHandler()
            responder = MockResponder()
            await handler.handle(request_context, responder)

        mock_cred_mgr.assert_called_once_with(request_context.profile)
        mock_cred_mgr.return_value.receive_proposal.assert_called_once_with(
            request_context.message, request_context.connection_record.connection_id
        )
        messages = responder.messages
        assert len(messages) == 1
        (result, target) = messages[0]
        assert result == "credential_offer_message"
        assert target == {}

    async def test_called_auto_offer_x(self):
        request_context = RequestContext.test_context()
        request_context.message_receipt = MessageReceipt()
        request_context.connection_record = async_mock.MagicMock()

        with async_mock.patch.object(
            test_module, "CredentialManager", autospec=True
        ) as mock_cred_mgr:
            mock_cred_mgr.return_value.receive_proposal = async_mock.CoroutineMock(
                return_value=async_mock.MagicMock(
                    save_error_state=async_mock.CoroutineMock()
                )
            )
            mock_cred_mgr.return_value.receive_proposal.return_value.auto_offer = True
            mock_cred_mgr.return_value.create_offer = async_mock.CoroutineMock(
                side_effect=test_module.IndyIssuerError()
            )

            request_context.message = CredentialProposal()
            request_context.connection_ready = True
            handler = test_module.CredentialProposalHandler()
            responder = MockResponder()

            with async_mock.patch.object(
                responder, "send_reply", async_mock.CoroutineMock()
            ) as mock_send_reply, async_mock.patch.object(
                handler._logger, "exception", async_mock.MagicMock()
            ) as mock_log_exc:
                await handler.handle(request_context, responder)
                mock_log_exc.assert_called_once()

    async def test_called_not_ready(self):
        request_context = RequestContext.test_context()
        request_context.message_receipt = MessageReceipt()
        request_context.connection_record = async_mock.MagicMock()

        with async_mock.patch.object(
            test_module, "CredentialManager", autospec=True
        ) as mock_cred_mgr:
            mock_cred_mgr.return_value.receive_proposal = async_mock.CoroutineMock()
            request_context.message = CredentialProposal()
            request_context.connection_ready = False
            handler = test_module.CredentialProposalHandler()
            responder = MockResponder()
            with self.assertRaises(test_module.HandlerException) as err:
                await handler.handle(request_context, responder)
            assert (
                err.exception.message
                == "Connection used for credential proposal not ready"
            )

        assert not responder.messages

    async def test_called_no_connection(self):
        request_context = RequestContext.test_context()
        request_context.message_receipt = MessageReceipt()

        request_context.message = CredentialProposal()
        handler = test_module.CredentialProposalHandler()
        responder = MockResponder()
        with self.assertRaises(test_module.HandlerException) as err:
            await handler.handle(request_context, responder)
        assert (
            err.exception.message
            == "Connectionless not supported for credential proposal"
        )

        assert not responder.messages
