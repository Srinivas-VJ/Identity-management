from asynctest import mock as async_mock, TestCase as AsyncTestCase

from ......core.in_memory import InMemoryProfile

from ...messages.inner.credential_preview import CredAttrSpec, CredentialPreview
from ...messages.credential_proposal import CredentialProposal

from .. import credential_exchange as test_module
from ..credential_exchange import V10CredentialExchange

TEST_DID = "LjgpST2rjsoxYegQDRm7EL"
SCHEMA_NAME = "bc-reg"
SCHEMA_TXN = 12
SCHEMA_ID = f"{TEST_DID}:2:{SCHEMA_NAME}:1.0"
SCHEMA = {
    "ver": "1.0",
    "id": SCHEMA_ID,
    "name": SCHEMA_NAME,
    "version": "1.0",
    "attrNames": ["legalName", "jurisdictionId", "incorporationDate"],
    "seqNo": SCHEMA_TXN,
}
CRED_DEF_ID = f"{TEST_DID}:3:CL:12:tag1"
CRED_PREVIEW = CredentialPreview(
    attributes=(
        CredAttrSpec.list_plain({"test": "123", "hello": "world"})
        + [CredAttrSpec(name="icon", value="cG90YXRv", mime_type="image/png")]
    )
)


class TestV10CredentialExchange(AsyncTestCase):
    """Test de/serialization."""

    async def test_serde(self):
        """Test de/serialization."""

        credential_proposal = CredentialProposal(
            comment="Hello World",
            credential_proposal=CRED_PREVIEW,
            schema_id=SCHEMA_ID,
            cred_def_id=CRED_DEF_ID,
        )
        for proposal_arg in [credential_proposal, credential_proposal.serialize()]:
            cx_rec = V10CredentialExchange(
                credential_exchange_id="dummy",
                connection_id="0000...",
                thread_id="dummy-thid",
                parent_thread_id="dummy-pthid",
                initiator=V10CredentialExchange.INITIATOR_EXTERNAL,
                role=V10CredentialExchange.ROLE_ISSUER,
                state=V10CredentialExchange.STATE_PROPOSAL_RECEIVED,
                credential_definition_id=CRED_DEF_ID,
                schema_id=SCHEMA_ID,
                credential_proposal_dict=proposal_arg,
                credential_request_metadata=None,
                credential_id="cred-id",
                revoc_reg_id=None,
                revocation_id=None,
                auto_offer=False,
                auto_issue=False,
                auto_remove=True,
                error_msg=None,
                trace=False,
            )
            assert type(cx_rec.credential_proposal_dict) == CredentialProposal
            ser = cx_rec.serialize()
            deser = V10CredentialExchange.deserialize(ser)
            assert type(deser.credential_proposal_dict) == CredentialProposal

    async def test_save_error_state(self):
        session = InMemoryProfile.test_session()
        record = V10CredentialExchange(state=None)
        assert record._last_state is None
        await record.save_error_state(session)  # cover short circuit

        record.state = V10CredentialExchange.STATE_PROPOSAL_RECEIVED
        await record.save(session)

        with async_mock.patch.object(
            record, "save", async_mock.CoroutineMock()
        ) as mock_save, async_mock.patch.object(
            test_module.LOGGER, "exception", async_mock.MagicMock()
        ) as mock_log_exc:
            mock_save.side_effect = test_module.StorageError()
            await record.save_error_state(session, reason="test")
            mock_log_exc.assert_called_once()
