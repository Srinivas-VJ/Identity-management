import asyncio
import json
import tempfile
import pytest

from os import path

from asynctest import mock as async_mock, TestCase as AsyncTestCase

from ...config.injection_context import InjectionContext
from ...cache.in_memory import InMemoryCache
from ...indy.issuer import IndyIssuer, IndyIssuerError
from ...indy.sdk.profile import IndySdkProfile
from ...storage.record import StorageRecord
from ...wallet.base import BaseWallet
from ...wallet.did_info import DIDInfo
from ...wallet.did_posture import DIDPosture
from ...wallet.error import WalletNotFoundError
from ...wallet.indy import IndySdkWallet
from ...wallet.key_type import ED25519
from ...wallet.did_method import SOV

from ..endpoint_type import EndpointType
from ..indy import (
    BadLedgerRequestError,
    ClosedPoolError,
    ErrorCode,
    IndyErrorHandler,
    IndyError,
    IndySdkLedger,
    IndySdkLedgerPool,
    IndySdkLedgerPoolProvider,
    GENESIS_TRANSACTION_FILE,
    LedgerConfigError,
    LedgerError,
    LedgerTransactionError,
    Role,
    TAA_ACCEPTED_RECORD_TYPE,
)


GENESIS_TRANSACTION_PATH = path.join(
    tempfile.gettempdir(), f"name_{GENESIS_TRANSACTION_FILE}"
)


@pytest.mark.indy
class TestIndySdkLedgerPoolProvider(AsyncTestCase):
    async def test_provide(self):
        provider = IndySdkLedgerPoolProvider()
        mock_injector = async_mock.MagicMock(
            inject=async_mock.MagicMock(return_value=None)
        )
        provider.provide(
            settings={
                "ledger.read_only": True,
                "ledger.genesis_transactions": "genesis-txns",
            },
            injector=mock_injector,
        )


@pytest.mark.indy
class TestIndySdkLedger(AsyncTestCase):
    async def setUp(self):
        self.test_did = "55GkHamhTU1ZbTbV2ab9DE"
        self.test_did_info = DIDInfo(
            did=self.test_did,
            verkey="3Dn1SJNPaCXcvvJvSbsFWP2xaCjMom3can8CQNhWrTRx",
            metadata={"test": "test"},
            method=SOV,
            key_type=ED25519,
        )
        self.test_verkey = "3Dn1SJNPaCXcvvJvSbsFWP2xaCjMom3can8CQNhWrTRx"
        context = InjectionContext()
        context.injector.bind_instance(IndySdkLedgerPool, IndySdkLedgerPool("name"))
        with async_mock.patch.object(IndySdkProfile, "_make_finalizer"):
            self.profile = IndySdkProfile(
                async_mock.CoroutineMock(),
                context,
            )
        self.session = await self.profile.session()

    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.list_pools")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("builtins.open")
    async def test_init(
        self, mock_open, mock_open_ledger, mock_list_pools, mock_create_config
    ):
        mock_open.return_value = async_mock.MagicMock()
        mock_list_pools.return_value = []

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", genesis_transactions="genesis_transactions"),
            self.profile,
        )

        assert ledger.pool_name == "name"
        assert not ledger.read_only
        assert ledger.backend
        assert ledger.profile is self.profile

        await ledger.__aenter__()

        mock_open.assert_called_once_with(GENESIS_TRANSACTION_PATH, "w")
        mock_open.return_value.__enter__.return_value.write.assert_called_once_with(
            "genesis_transactions"
        )
        mock_create_config.assert_called_once_with(
            "name", json.dumps({"genesis_txn": GENESIS_TRANSACTION_PATH})
        )
        assert ledger.did_to_nym(ledger.nym_to_did(self.test_did)) == self.test_did

    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.list_pools")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("builtins.open")
    async def test_init_not_checked(
        self, mock_open, mock_open_ledger, mock_list_pools, mock_create_config
    ):
        mock_open.return_value = async_mock.MagicMock()
        mock_list_pools.return_value = []

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name"), self.profile)

        assert ledger.pool_name == "name"
        assert ledger.backend
        assert ledger.profile is self.profile

        with self.assertRaises(LedgerError):
            await ledger.__aenter__()

        mock_list_pools.return_value = [{"pool": ledger.pool_name}]
        await ledger.__aenter__()

    @async_mock.patch("indy.pool.list_pools")
    @async_mock.patch("builtins.open")
    async def test_init_do_not_recreate(self, mock_open, mock_list_pools):
        mock_open.return_value = async_mock.MagicMock()
        mock_list_pools.return_value = [{"pool": "name"}, {"pool": "another"}]

        pool = IndySdkLedgerPool("name")
        assert pool.name == "name"

        with self.assertRaises(LedgerConfigError):
            await pool.create_pool_config("genesis_transactions", recreate=False)

        mock_open.assert_called_once_with(GENESIS_TRANSACTION_PATH, "w")

    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.delete_pool_ledger_config")
    @async_mock.patch("indy.pool.list_pools")
    @async_mock.patch("builtins.open")
    async def test_init_recreate(
        self, mock_open, mock_list_pools, mock_delete_config, mock_create_config
    ):
        mock_open.return_value = async_mock.MagicMock()
        mock_list_pools.return_value = [{"pool": "name"}, {"pool": "another"}]
        mock_delete_config.return_value = None

        pool = IndySdkLedgerPool("name")
        assert pool.name == "name"

        await pool.create_pool_config("genesis_transactions", recreate=True)

        mock_open.assert_called_once_with(GENESIS_TRANSACTION_PATH, "w")
        mock_delete_config.assert_called_once_with("name")
        mock_create_config.assert_called_once_with(
            "name", json.dumps({"genesis_txn": GENESIS_TRANSACTION_PATH})
        )

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    async def test_aenter_aexit(
        self, mock_close_pool, mock_open_ledger, mock_set_proto
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger as led:
                mock_set_proto.assert_called_once_with(2)
                mock_open_ledger.assert_called_once_with("name", "{}")
                assert led == ledger
                mock_close_pool.assert_not_called()
                assert led.pool_handle == mock_open_ledger.return_value

        mock_close_pool.assert_called_once()
        assert ledger.pool_handle is None

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    async def test_aenter_aexit_nested_keepalive(
        self, mock_close_pool, mock_open_ledger, mock_set_proto
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, keepalive=1), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger as led0:
                mock_set_proto.assert_called_once_with(2)
                mock_open_ledger.assert_called_once_with("name", "{}")
                assert led0 == ledger
                mock_close_pool.assert_not_called()
                assert led0.pool_handle == mock_open_ledger.return_value

            async with ledger as led1:
                assert ledger.pool.ref_count == 1

        mock_close_pool.assert_not_called()  # it's a future
        assert ledger.pool_handle

        await asyncio.sleep(1.01)
        mock_close_pool.assert_called_once()
        assert ledger.pool_handle is None

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    async def test_aenter_aexit_close_x(
        self, mock_close_pool, mock_open_ledger, mock_set_proto
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_close_pool.side_effect = IndyError(ErrorCode.PoolLedgerTimeout)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            with self.assertRaises(LedgerError):
                async with ledger as led:
                    assert led.pool_handle == mock_open_ledger.return_value

        assert ledger.pool_handle == mock_open_ledger.return_value
        assert ledger.pool.ref_count == 1

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    async def test_submit_pool_closed(
        self, mock_close_pool, mock_open_ledger, mock_create_config, mock_set_proto
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            with self.assertRaises(ClosedPoolError) as context:
                await ledger._submit("{}")
        assert "sign and submit request to closed pool" in str(context.exception)

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch("indy.ledger.sign_and_submit_request")
    @async_mock.patch("indy.ledger.multi_sign_request")
    async def test_submit_signed(
        self,
        mock_indy_multi_sign,
        mock_sign_submit,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):
        mock_indy_multi_sign.return_value = json.dumps({"endorsed": "content"})
        mock_sign_submit.return_value = '{"op": "REPLY"}'

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            async with ledger:
                mock_wallet_get_public_did.return_value = None

                with self.assertRaises(BadLedgerRequestError):
                    await ledger._submit("{}", True)

                mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
                mock_did = mock_wallet_get_public_did.return_value
                mock_did.did = self.test_did

                await ledger._submit(
                    request_json="{}",
                    sign=True,
                    taa_accept=False,
                )

                result_json = await ledger._submit(  # multi-sign for later endorsement
                    request_json="{}",
                    sign=True,
                    taa_accept=False,
                    write_ledger=False,
                )
                assert json.loads(result_json) == {"endorsed": "content"}

                await ledger.txn_submit(  # cover txn_submit()
                    request_json="{}",
                    sign=True,
                    taa_accept=False,
                )

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch("indy.ledger.sign_and_submit_request")
    @async_mock.patch("indy.ledger.append_txn_author_agreement_acceptance_to_request")
    async def test_submit_signed_taa_accept(
        self,
        mock_append_taa,
        mock_sign_submit,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):

        mock_append_taa.return_value = "{}"
        mock_sign_submit.return_value = '{"op": "REPLY"}'

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            ledger = IndySdkLedger(
                IndySdkLedgerPool("name", checked=True), self.profile
            )
            ledger.get_latest_txn_author_acceptance = async_mock.CoroutineMock(
                return_value={
                    "text": "sample",
                    "version": "0.0",
                    "digest": "digest",
                    "mechanism": "dummy",
                    "time": "now",
                }
            )

            async with ledger:
                mock_did = mock_wallet_get_public_did.return_value
                mock_did.did = self.test_did

                await ledger._submit(
                    request_json="{}",
                    sign=None,
                    taa_accept=True,
                    sign_did=self.test_did_info,
                )
                mock_append_taa.assert_called_once_with(
                    "{}", "sample", "0.0", "digest", "dummy", "now"
                )

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch("indy.ledger.submit_request")
    async def test_submit_unsigned(
        self,
        mock_submit,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):

        mock_did = async_mock.MagicMock()

        future = asyncio.Future()
        future.set_result(mock_did)

        mock_submit.return_value = '{"op": "REPLY"}'

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = future
            async with ledger:
                await ledger._submit("{}", False)
                mock_submit.assert_called_once_with(ledger.pool_handle, "{}")

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch("indy.ledger.submit_request")
    async def test_submit_unsigned_ledger_transaction_error(
        self,
        mock_submit,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):

        mock_did = async_mock.MagicMock()

        future = asyncio.Future()
        future.set_result(mock_did)

        mock_submit.return_value = '{"op": "NO-SUCH-OP"}'

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = future
            ledger = IndySdkLedger(
                IndySdkLedgerPool("name", checked=True), self.profile
            )
            async with ledger:
                with self.assertRaises(LedgerTransactionError):
                    await ledger._submit("{}", False)
                mock_submit.assert_called_once_with(ledger.pool_handle, "{}")

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch("indy.ledger.submit_request")
    async def test_submit_rejected(
        self,
        mock_submit,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):

        mock_did = async_mock.MagicMock()

        future = asyncio.Future()
        future.set_result(mock_did)

        mock_submit.return_value = '{"op": "REQNACK", "reason": "a reason"}'

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = future
            async with ledger:
                with self.assertRaises(LedgerTransactionError) as context:
                    await ledger._submit("{}", False)
                assert "Ledger rejected transaction request" in str(context.exception)

        mock_submit.return_value = '{"op": "REJECT", "reason": "another reason"}'

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = future
            async with ledger:
                with self.assertRaises(LedgerTransactionError) as context:
                    await ledger._submit("{}", False)
                assert "Ledger rejected transaction request" in str(context.exception)

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch("indy.ledger.multi_sign_request")
    async def test_txn_endorse(
        self,
        mock_indy_multi_sign,
        mock_indy_close,
        mock_indy_open,
        mock_create_config,
        mock_set_proto,
    ):
        mock_indy_multi_sign.return_value = json.dumps({"endorsed": "content"})
        mock_indy_open.return_value = 1

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = None
            with self.assertRaises(ClosedPoolError):
                await ledger.txn_endorse(request_json=json.dumps({"...": "..."}))

            async with ledger:
                with self.assertRaises(BadLedgerRequestError):
                    await ledger.txn_endorse(request_json=json.dumps({"...": "..."}))

                mock_wallet_get_public_did.return_value = self.test_did_info

                endorsed_json = await ledger.txn_endorse(
                    request_json=json.dumps({"...": "..."})
                )
                assert json.loads(endorsed_json) == {"endorsed": "content"}

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.fetch_schema_by_id")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_schema_by_seq_no"
    )
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_schema_request")
    @async_mock.patch("indy.ledger.append_request_endorser")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_send_schema(
        self,
        mock_is_ledger_read_only,
        mock_append_request_endorser,
        mock_build_schema_req,
        mock_add_record,
        mock_fetch_schema_by_seq_no,
        mock_fetch_schema_by_id,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_is_ledger_read_only.return_value = False

        issuer = async_mock.MagicMock(IndyIssuer)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        issuer.create_schema.return_value = ("schema_issuer_did:name:1.0", "{}")
        mock_fetch_schema_by_id.return_value = None
        mock_fetch_schema_by_seq_no.return_value = None

        mock_submit.return_value = (
            r'{"op":"REPLY","result":{"txnMetadata":{"seqNo": 1}}}'
        )
        future = asyncio.Future()
        future.set_result(async_mock.MagicMock(add_record=async_mock.CoroutineMock()))
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            ledger, "get_indy_storage", async_mock.MagicMock()
        ) as mock_get_storage:
            mock_get_storage.return_value = future
            async with ledger:
                mock_wallet_get_public_did.return_value = None

                with self.assertRaises(BadLedgerRequestError):
                    schema_id, schema_def = await ledger.create_and_send_schema(
                        issuer, "schema_name", "schema_version", [1, 2, 3]
                    )

                mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
                mock_did = mock_wallet_get_public_did.return_value
                mock_did.did = self.test_did

                schema_id, schema_def = await ledger.create_and_send_schema(
                    issuer, "schema_name", "schema_version", [1, 2, 3]
                )
                issuer.create_schema.assert_called_once_with(
                    mock_did.did, "schema_name", "schema_version", [1, 2, 3]
                )

                mock_build_schema_req.assert_called_once_with(
                    mock_did.did, issuer.create_schema.return_value[1]
                )

                mock_submit.assert_called_once_with(
                    mock_build_schema_req.return_value,
                    sign=True,
                    sign_did=mock_wallet_get_public_did.return_value,
                    taa_accept=None,
                    write_ledger=True,
                )

                assert schema_id == issuer.create_schema.return_value[0]

                schema_id, signed_txn = await ledger.create_and_send_schema(
                    issuer=issuer,
                    schema_name="schema_name",
                    schema_version="schema_version",
                    attribute_names=[1, 2, 3],
                    write_ledger=False,
                    endorser_did=self.test_did,
                )
                assert schema_id == issuer.create_schema.return_value[0]
                assert "signed_txn" in signed_txn

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.check_existing_schema"
    )
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_schema_request")
    async def test_send_schema_already_exists(
        self,
        mock_build_schema_req,
        mock_add_record,
        mock_check_existing,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):
        # mock_did = async_mock.CoroutineMock()

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.create_schema.return_value = ("1", "{}")
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        mock_add_record = async_mock.CoroutineMock()
        future = asyncio.Future()
        future.set_result(
            async_mock.MagicMock(
                return_value=async_mock.MagicMock(add_record=mock_add_record)
            )
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            ledger, "get_indy_storage", async_mock.MagicMock()
        ) as mock_get_storage:
            mock_get_storage.return_value = future
            mock_wallet_get_public_did.return_value = self.test_did_info
            fetch_schema_id = (
                f"{mock_wallet_get_public_did.return_value.did}:2:"
                "schema_name:schema_version"
            )
            mock_check_existing.return_value = (fetch_schema_id, {})

            async with ledger:
                schema_id, schema_def = await ledger.create_and_send_schema(
                    issuer, "schema_name", "schema_version", [1, 2, 3]
                )
                assert schema_id == fetch_schema_id
                assert schema_def == {}

            mock_add_record.assert_not_called()

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.check_existing_schema"
    )
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_schema_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_send_schema_ledger_transaction_error_already_exists(
        self,
        mock_is_ledger_read_only,
        mock_build_schema_req,
        mock_add_record,
        mock_check_existing,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_is_ledger_read_only.return_value = False

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.create_schema.return_value = ("1", "{}")
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        ledger._submit = async_mock.CoroutineMock(
            side_effect=LedgerTransactionError("UnauthorizedClientRequest")
        )
        future = asyncio.Future()
        future.set_result(async_mock.MagicMock(add_record=async_mock.CoroutineMock()))
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            ledger, "get_indy_storage", async_mock.MagicMock()
        ) as mock_get_storage:
            mock_get_storage.return_value = future
            mock_wallet_get_public_did.return_value = self.test_did_info
            fetch_schema_id = (
                f"{mock_wallet_get_public_did.return_value.did}:2:"
                "schema_name:schema_version"
            )
            mock_check_existing.side_effect = [None, (fetch_schema_id, "{}")]
            async with ledger:
                schema_id, schema_def = await ledger.create_and_send_schema(
                    issuer, "schema_name", "schema_version", [1, 2, 3]
                )
                assert schema_id == fetch_schema_id

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.check_existing_schema"
    )
    async def test_send_schema_ledger_read_only(
        self,
        mock_check_existing,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.create_schema.return_value = ("1", "{}")
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            fetch_schema_id = (
                f"{mock_wallet_get_public_did.return_value.did}:2:"
                "schema_name:schema_version"
            )
            mock_check_existing.side_effect = [None, fetch_schema_id]
            async with ledger:
                with self.assertRaises(LedgerError) as context:
                    await ledger.create_and_send_schema(
                        issuer, "schema_name", "schema_version", [1, 2, 3]
                    )
                assert "read only" in str(context.exception)

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.check_existing_schema"
    )
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_send_schema_issuer_error(
        self,
        mock_is_ledger_read_only,
        mock_check_existing,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_is_ledger_read_only.return_value = False

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.create_schema = async_mock.CoroutineMock(
            side_effect=IndyIssuerError("dummy error")
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            fetch_schema_id = (
                f"{mock_wallet_get_public_did.return_value.did}:2:"
                "schema_name:schema_version"
            )
            mock_check_existing.side_effect = [None, fetch_schema_id]
            async with ledger:
                with self.assertRaises(LedgerError) as context:
                    await ledger.create_and_send_schema(
                        issuer, "schema_name", "schema_version", [1, 2, 3]
                    )
                assert "dummy error" in str(context.exception)

    @async_mock.patch("indy.pool.set_protocol_version")
    @async_mock.patch("indy.pool.create_pool_ledger_config")
    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.check_existing_schema"
    )
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_schema_request")
    async def test_send_schema_ledger_transaction_error(
        self,
        mock_build_schema_req,
        mock_add_record,
        mock_check_existing,
        mock_close_pool,
        mock_open_ledger,
        mock_create_config,
        mock_set_proto,
    ):

        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.create_schema.return_value = ("1", "{}")
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        ledger._submit = async_mock.CoroutineMock(
            side_effect=LedgerTransactionError("Some other error message")
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            fetch_schema_id = (
                f"{mock_wallet_get_public_did.return_value.did}:2:"
                "schema_name:schema_version"
            )
            mock_check_existing.side_effect = [None, fetch_schema_id]
            async with ledger:
                with self.assertRaises(LedgerTransactionError):
                    await ledger.create_and_send_schema(
                        issuer, "schema_name", "schema_version", [1, 2, 3]
                    )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.fetch_schema_by_id")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_schema_by_seq_no"
    )
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_schema_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_send_schema_no_seq_no(
        self,
        mock_is_ledger_read_only,
        mock_build_schema_req,
        mock_add_record,
        mock_fetch_schema_by_seq_no,
        mock_fetch_schema_by_id,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        issuer = async_mock.MagicMock(IndyIssuer)
        mock_is_ledger_read_only.return_value = False
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        issuer.create_schema.return_value = ("schema_issuer_did:name:1.0", "{}")
        mock_fetch_schema_by_id.return_value = None
        mock_fetch_schema_by_seq_no.return_value = None

        mock_submit.return_value = (
            r'{"op":"REPLY","result":{"txnMetadata":{"no": "seqNo"}}}'
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            async with ledger:
                mock_wallet.get_public_did = async_mock.CoroutineMock()
                mock_did = mock_wallet_get_public_did.return_value
                mock_did.did = self.test_did

                with self.assertRaises(LedgerError) as context:
                    await ledger.create_and_send_schema(
                        issuer, "schema_name", "schema_version", [1, 2, 3]
                    )
                assert "schema sequence number" in str(context.exception)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.fetch_schema_by_id")
    async def test_check_existing_schema(
        self,
        mock_fetch_schema_by_id,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)

        mock_fetch_schema_by_id.return_value = {"attrNames": ["a", "b", "c"]}
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            mock_did = mock_wallet_get_public_did.return_value
            mock_did.did = self.test_did
            async with ledger:
                schema_id, schema_def = await ledger.check_existing_schema(
                    public_did=self.test_did,
                    schema_name="test",
                    schema_version="1.0",
                    attribute_names=["c", "b", "a"],
                )
                assert schema_id == f"{self.test_did}:2:test:1.0"

                with self.assertRaises(LedgerTransactionError):
                    await ledger.check_existing_schema(
                        public_did=self.test_did,
                        schema_name="test",
                        schema_version="1.0",
                        attribute_names=["a", "b", "c", "d"],
                    )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_schema_request")
    @async_mock.patch("indy.ledger.parse_get_schema_response")
    async def test_get_schema(
        self,
        mock_parse_get_schema_resp,
        mock_build_get_schema_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)

        mock_parse_get_schema_resp.return_value = (None, '{"attrNames": ["a", "b"]}')

        mock_submit.return_value = '{"result":{"seqNo":1}}'

        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            mock_did = mock_wallet_get_public_did.return_value
            mock_did.did = self.test_did
            ledger = IndySdkLedger(
                IndySdkLedgerPool("name", checked=True, cache=InMemoryCache()),
                self.profile,
            )
            async with ledger:
                response = await ledger.get_schema("schema_id")
                mock_wallet_get_public_did.assert_called_once_with()
                mock_build_get_schema_req.assert_called_once_with(
                    mock_did.did, "schema_id"
                )
                mock_submit.assert_called_once_with(
                    mock_build_get_schema_req.return_value, sign_did=mock_did
                )
                mock_parse_get_schema_resp.assert_called_once_with(
                    mock_submit.return_value
                )

                assert response == json.loads(
                    mock_parse_get_schema_resp.return_value[1]
                )

                response == await ledger.get_schema("schema_id")  # cover get-from-cache
                assert response == json.loads(
                    mock_parse_get_schema_resp.return_value[1]
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_schema_request")
    async def test_get_schema_not_found(
        self,
        mock_build_get_schema_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)

        mock_submit.return_value = json.dumps({"result": {"seqNo": None}})

        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            mock_did = mock_wallet_get_public_did.return_value
            mock_did.did = self.test_did
            ledger = IndySdkLedger(
                IndySdkLedgerPool("name", checked=True, cache=InMemoryCache()),
                self.profile,
            )

            async with ledger:
                response = await ledger.get_schema("schema_id")
                mock_wallet_get_public_did.assert_called_once_with()
                mock_build_get_schema_req.assert_called_once_with(
                    mock_did.did, "schema_id"
                )
                mock_submit.assert_called_once_with(
                    mock_build_get_schema_req.return_value, sign_did=mock_did
                )

                assert response is None

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_txn_request")
    @async_mock.patch("indy.ledger.build_get_schema_request")
    @async_mock.patch("indy.ledger.parse_get_schema_response")
    async def test_get_schema_by_seq_no(
        self,
        mock_parse_get_schema_resp,
        mock_build_get_schema_req,
        mock_build_get_txn_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)

        mock_parse_get_schema_resp.return_value = (None, '{"attrNames": ["a", "b"]}')

        submissions = [
            json.dumps(
                {
                    "result": {
                        "data": {
                            "txn": {
                                "type": "101",
                                "metadata": {"from": self.test_did},
                                "data": {
                                    "data": {"name": "preferences", "version": "1.0"}
                                },
                            }
                        }
                    }
                }
            ),
            json.dumps({"result": {"seqNo": 999}}),
        ]  # need to subscript these in assertions later
        mock_submit.side_effect = [
            sub for sub in submissions
        ]  # becomes list iterator, unsubscriptable, in mock object
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            mock_did = mock_wallet_get_public_did.return_value
            mock_did.did = self.test_did
            ledger = IndySdkLedger(
                IndySdkLedgerPool("name", checked=True), self.profile
            )
            async with ledger:
                response = await ledger.get_schema("999")
                mock_wallet_get_public_did.assert_called_once_with()
                mock_build_get_txn_req.assert_called_once_with(None, None, seq_no=999)
                mock_build_get_schema_req.assert_called_once_with(
                    mock_did.did, f"{self.test_did}:2:preferences:1.0"
                )
                mock_submit.assert_has_calls(
                    [
                        async_mock.call(mock_build_get_txn_req.return_value),
                        async_mock.call(
                            mock_build_get_schema_req.return_value, sign_did=mock_did
                        ),
                    ]
                )
                mock_parse_get_schema_resp.assert_called_once_with(submissions[1])

                assert response == json.loads(
                    mock_parse_get_schema_resp.return_value[1]
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_txn_request")
    @async_mock.patch("indy.ledger.build_get_schema_request")
    @async_mock.patch("indy.ledger.parse_get_schema_response")
    async def test_get_schema_by_wrong_seq_no(
        self,
        mock_parse_get_schema_resp,
        mock_build_get_schema_req,
        mock_build_get_txn_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)

        mock_parse_get_schema_resp.return_value = (None, '{"attrNames": ["a", "b"]}')

        submissions = [
            json.dumps(
                {
                    "result": {
                        "data": {
                            "txn": {
                                "type": "102",
                            }
                        }
                    }
                }
            ),  # not a schema
            json.dumps({"result": {"seqNo": 999}}),
        ]  # need to subscript these in assertions later
        mock_submit.side_effect = [
            sub for sub in submissions
        ]  # becomes list iterator, unsubscriptable, in mock object
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            mock_did = mock_wallet_get_public_did.return_value
            mock_did.did = self.test_did
            async with ledger:
                with self.assertRaises(LedgerTransactionError):
                    await ledger.get_schema("999")

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.find_all_records")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_cred_def_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_send_credential_definition(
        self,
        mock_is_ledger_read_only,
        mock_build_cred_def,
        mock_add_record,
        mock_find_all_records,
        mock_submit,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_find_all_records.return_value = []
        mock_is_ledger_read_only.return_value = False

        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.side_effect = [None, cred_def]

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.make_credential_definition_id.return_value = cred_def_id
        issuer.create_and_store_credential_definition.return_value = (
            cred_def_id,
            cred_def_json,
        )
        issuer.credential_definition_in_wallet.return_value = False

        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        future = asyncio.Future()
        future.set_result(async_mock.MagicMock(add_record=async_mock.CoroutineMock()))
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            ledger, "get_indy_storage", async_mock.MagicMock()
        ) as mock_get_storage:
            mock_get_storage.return_value = future
            async with ledger:
                mock_wallet_get_public_did.return_value = None
                with self.assertRaises(BadLedgerRequestError):
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )
                mock_wallet_get_public_did.return_value = DIDInfo(
                    did=self.test_did,
                    verkey=self.test_verkey,
                    metadata=None,
                    method=SOV,
                    key_type=ED25519,
                )
                mock_did = mock_wallet_get_public_did.return_value
                (
                    result_id,
                    result_def,
                    novel,
                ) = await ledger.create_and_send_credential_definition(
                    issuer, schema_id, None, tag
                )
                assert result_id == cred_def_id
                assert novel
                mock_get_schema.assert_called_once_with(schema_id)
                mock_build_cred_def.assert_called_once_with(mock_did.did, cred_def_json)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.find_all_records")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_cred_def_request")
    @async_mock.patch("indy.ledger.append_request_endorser")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_send_credential_definition_endorse_only(
        self,
        mock_is_ledger_read_only,
        mock_append_request_endorser,
        mock_build_cred_def,
        mock_add_record,
        mock_find_all_records,
        mock_submit,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_find_all_records.return_value = []
        mock_is_ledger_read_only.return_value = False

        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.side_effect = [None, cred_def]

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.make_credential_definition_id.return_value = cred_def_id
        issuer.create_and_store_credential_definition.return_value = (
            cred_def_id,
            cred_def_json,
        )
        issuer.credential_definition_in_wallet.return_value = False
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = DIDInfo(
                self.test_did,
                self.test_verkey,
                None,
                SOV,
                ED25519,
            )
            async with ledger:
                (
                    result_id,
                    signed_txn,
                    novel,
                ) = await ledger.create_and_send_credential_definition(
                    issuer=issuer,
                    schema_id=schema_id,
                    signature_type=None,
                    tag=tag,
                    support_revocation=False,
                    write_ledger=False,
                    endorser_did=self.test_did,
                )
                assert "signed_txn" in signed_txn

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.find_all_records")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_cred_def_request")
    async def test_send_credential_definition_exists_in_ledger_and_wallet(
        self,
        mock_build_cred_def,
        mock_add_record,
        mock_find_all_records,
        mock_submit,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_find_all_records.return_value = []

        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.return_value = {"mock": "cred-def"}

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.make_credential_definition_id.return_value = cred_def_id
        issuer.create_and_store_credential_definition.return_value = (
            cred_def_id,
            cred_def_json,
        )
        issuer.credential_definition_in_wallet.return_value = True
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        future = asyncio.Future()
        future.set_result(async_mock.MagicMock(add_record=async_mock.CoroutineMock()))
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            ledger, "get_indy_storage", async_mock.MagicMock()
        ) as mock_get_storage:
            mock_get_storage.return_value = future
            mock_wallet_get_public_did.return_value = DIDInfo(
                did=self.test_did,
                verkey=self.test_verkey,
                metadata=None,
                method=SOV,
                key_type=ED25519,
            )

            async with ledger:
                mock_did = mock_wallet_get_public_did.return_value

                (
                    result_id,
                    result_def,
                    novel,
                ) = await ledger.create_and_send_credential_definition(
                    issuer, schema_id, None, tag
                )
                assert result_id == cred_def_id
                assert not novel

                mock_wallet_get_public_did.assert_called_once_with()
                mock_get_schema.assert_called_once_with(schema_id)

                mock_build_cred_def.assert_not_called()
                mock_get_storage.assert_not_called()

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    async def test_send_credential_definition_no_such_schema(
        self,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_get_schema.return_value = {}

        issuer = async_mock.MagicMock(IndyIssuer)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            async with ledger:
                with self.assertRaises(LedgerError):
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.find_all_records")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_cred_def_request")
    async def test_send_credential_definition_offer_exception(
        self,
        mock_build_cred_def,
        mock_add_record,
        mock_find_all_records,
        mock_submit,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_find_all_records.return_value = []

        mock_get_schema.return_value = {"seqNo": 999}

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.credential_definition_in_wallet.side_effect = IndyIssuerError(
            "common IO error"
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            async with ledger:
                with self.assertRaises(LedgerError):
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    async def test_send_credential_definition_cred_def_in_wallet_not_ledger(
        self,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.return_value = {}

        issuer = async_mock.MagicMock(IndyIssuer)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            async with ledger:
                with self.assertRaises(LedgerError):
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    async def test_send_credential_definition_cred_def_not_on_ledger_wallet_check_x(
        self,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.return_value = {}

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.credential_definition_in_wallet = async_mock.CoroutineMock(
            side_effect=IndyIssuerError("dummy error")
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            async with ledger:
                with self.assertRaises(LedgerError) as context:
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )
                assert "dummy error" in str(context.exception)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    async def test_send_credential_definition_cred_def_not_on_ledger_nor_wallet_send_x(
        self,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.return_value = {}

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.credential_definition_in_wallet = async_mock.CoroutineMock(
            return_value=False
        )
        issuer.create_and_store_credential_definition = async_mock.CoroutineMock(
            side_effect=IndyIssuerError("dummy error")
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            async with ledger:
                with self.assertRaises(LedgerError) as context:
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )
                assert "dummy error" in str(context.exception)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    async def test_send_credential_definition_read_only(
        self,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.return_value = {}

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.credential_definition_in_wallet = async_mock.CoroutineMock(
            return_value=False
        )
        issuer.create_and_store_credential_definition = async_mock.CoroutineMock(
            return_value=("cred-def-id", "cred-def-json")
        )
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            async with ledger:
                with self.assertRaises(LedgerError) as context:
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )
                assert "read only" in str(context.exception)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    async def test_send_credential_definition_cred_def_on_ledger_not_in_wallet(
        self,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.return_value = cred_def

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.credential_definition_in_wallet = async_mock.CoroutineMock(
            return_value=False
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            async with ledger:
                with self.assertRaises(LedgerError):
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.find_all_records")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_cred_def_request")
    async def test_send_credential_definition_on_ledger_in_wallet(
        self,
        mock_build_cred_def,
        mock_add_record,
        mock_find_all_records,
        mock_submit,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_find_all_records.return_value = []

        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.return_value = cred_def

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.make_credential_definition_id.return_value = cred_def_id
        issuer.create_and_store_credential_definition.return_value = (
            cred_def_id,
            cred_def_json,
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            async with ledger:
                mock_wallet_get_public_did.return_value = None
                with self.assertRaises(BadLedgerRequestError):
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )

                mock_wallet_get_public_did.return_value = DIDInfo(
                    did=self.test_did,
                    verkey=self.test_verkey,
                    metadata=None,
                    method=SOV,
                    key_type=ED25519,
                )
                mock_did = mock_wallet_get_public_did.return_value

                (
                    result_id,
                    result_def,
                    novel,
                ) = await ledger.create_and_send_credential_definition(
                    issuer, schema_id, None, tag
                )
            assert result_id == cred_def_id

            mock_get_schema.assert_called_once_with(schema_id)

            mock_build_cred_def.assert_not_called()

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch(
        "aries_cloudagent.ledger.indy.IndySdkLedger.fetch_credential_definition"
    )
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.find_all_records")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("indy.ledger.build_cred_def_request")
    async def test_send_credential_definition_create_cred_def_exception(
        self,
        mock_build_cred_def,
        mock_add_record,
        mock_find_all_records,
        mock_submit,
        mock_fetch_cred_def,
        mock_close,
        mock_open,
        mock_get_schema,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_find_all_records.return_value = []

        mock_get_schema.return_value = {"seqNo": 999}
        cred_def_id = f"{self.test_did}:3:CL:999:default"
        cred_def_value = {
            "primary": {"n": "...", "s": "...", "r": "...", "revocation": None}
        }
        cred_def = {
            "ver": "1.0",
            "id": cred_def_id,
            "schemaId": "999",
            "type": "CL",
            "tag": "default",
            "value": cred_def_value,
        }
        cred_def_json = json.dumps(cred_def)

        mock_fetch_cred_def.return_value = None

        issuer = async_mock.MagicMock(IndyIssuer)
        issuer.create_and_store_credential_definition.side_effect = IndyIssuerError(
            "invalid structure"
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        schema_id = "schema_issuer_did:name:1.0"
        tag = "default"
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = DIDInfo(
                did=self.test_did,
                verkey=self.test_verkey,
                metadata=None,
                method=SOV,
                key_type=ED25519,
            )
            async with ledger:
                with self.assertRaises(LedgerError):
                    await ledger.create_and_send_credential_definition(
                        issuer, schema_id, None, tag
                    )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_cred_def_request")
    @async_mock.patch("indy.ledger.parse_get_cred_def_response")
    async def test_get_credential_definition(
        self,
        mock_parse_get_cred_def_resp,
        mock_build_get_cred_def_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_parse_get_cred_def_resp.return_value = (
            None,
            json.dumps({"result": {"seqNo": 1}}),
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = async_mock.CoroutineMock()
            mock_did = mock_wallet_get_public_did.return_value
            ledger = IndySdkLedger(
                IndySdkLedgerPool("name", checked=True, cache=InMemoryCache()),
                self.profile,
            )

            async with ledger:
                response = await ledger.get_credential_definition("cred_def_id")
                mock_wallet_get_public_did.assert_called_once_with()
                mock_build_get_cred_def_req.assert_called_once_with(
                    mock_did.did, "cred_def_id"
                )
                mock_submit.assert_called_once_with(
                    mock_build_get_cred_def_req.return_value, sign_did=mock_did
                )
                mock_parse_get_cred_def_resp.assert_called_once_with(
                    mock_submit.return_value
                )
                assert response == json.loads(
                    mock_parse_get_cred_def_resp.return_value[1]
                )
                response == await ledger.get_credential_definition(  # cover get-from-cache
                    "cred_def_id"
                )
                assert response == json.loads(
                    mock_parse_get_cred_def_resp.return_value[1]
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_cred_def_request")
    @async_mock.patch("indy.ledger.parse_get_cred_def_response")
    async def test_get_credential_definition_ledger_not_found(
        self,
        mock_parse_get_cred_def_resp,
        mock_build_get_cred_def_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)

        mock_parse_get_cred_def_resp.side_effect = IndyError(
            error_code=ErrorCode.LedgerNotFound, error_details={"message": "not today"}
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            ledger = IndySdkLedger(
                IndySdkLedgerPool("name", checked=True), self.profile
            )
            async with ledger:
                response = await ledger.get_credential_definition("cred_def_id")
                mock_did = mock_wallet_get_public_did.return_value
                mock_wallet_get_public_did.assert_called_once_with()
                mock_build_get_cred_def_req.assert_called_once_with(
                    mock_did.did, "cred_def_id"
                )
                mock_submit.assert_called_once_with(
                    mock_build_get_cred_def_req.return_value, sign_did=mock_did
                )
                mock_parse_get_cred_def_resp.assert_called_once_with(
                    mock_submit.return_value
                )

                assert response is None

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_cred_def_request")
    @async_mock.patch("indy.ledger.parse_get_cred_def_response")
    async def test_fetch_credential_definition_ledger_x(
        self,
        mock_parse_get_cred_def_resp,
        mock_build_get_cred_def_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()

        mock_parse_get_cred_def_resp.side_effect = IndyError(
            error_code=ErrorCode.CommonInvalidParam1,
            error_details={"message": "not today"},
        )

        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                with self.assertRaises(LedgerError) as context:
                    await ledger.fetch_credential_definition("cred_def_id")
                assert "not today" in str(context.exception)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_key_for_did(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_submit.return_value = json.dumps(
            {"result": {"data": json.dumps({"verkey": self.test_verkey})}}
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.get_key_for_did(self.test_did)

                assert mock_build_get_nym_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                )
                assert mock_submit.called_once_with(
                    mock_build_get_nym_req.return_value,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                assert response == self.test_verkey

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_endpoint_for_did(
        self, mock_submit, mock_build_get_attrib_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        endpoint = "http://aries.ca"
        mock_submit.return_value = json.dumps(
            {"result": {"data": json.dumps({"endpoint": {"endpoint": endpoint}})}}
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.get_endpoint_for_did(self.test_did)

                assert mock_build_get_attrib_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                    "endpoint",
                    None,
                    None,
                )
                assert mock_submit.called_once_with(
                    mock_build_get_attrib_req.return_value,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                assert response == endpoint

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_endpoint_of_type_profile_for_did(
        self, mock_submit, mock_build_get_attrib_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        endpoint = "http://company.com/masterdata"
        endpoint_type = EndpointType.PROFILE
        mock_submit.return_value = json.dumps(
            {
                "result": {
                    "data": json.dumps(
                        {"endpoint": {EndpointType.PROFILE.indy: endpoint}}
                    )
                }
            }
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.get_endpoint_for_did(
                    self.test_did,
                    endpoint_type,
                )

                assert mock_build_get_attrib_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                    "endpoint",
                    None,
                    None,
                )
                assert mock_submit.called_once_with(
                    mock_build_get_attrib_req.return_value,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                assert response == endpoint

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_all_endpoints_for_did(
        self, mock_submit, mock_build_get_attrib_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        profile_endpoint = "http://company.com/masterdata"
        default_endpoint = "http://agent.company.com"
        data_json = json.dumps(
            {"endpoint": {"endpoint": default_endpoint, "profile": profile_endpoint}}
        )
        mock_submit.return_value = json.dumps({"result": {"data": data_json}})
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.get_all_endpoints_for_did(self.test_did)

                assert mock_build_get_attrib_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                    "endpoint",
                    None,
                    None,
                )
                assert mock_submit.called_once_with(
                    mock_build_get_attrib_req.return_value,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                assert response == json.loads(data_json).get("endpoint")

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_all_endpoints_for_did_none(
        self, mock_submit, mock_build_get_attrib_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        profile_endpoint = "http://company.com/masterdata"
        default_endpoint = "http://agent.company.com"
        mock_submit.return_value = json.dumps({"result": {"data": None}})
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.get_all_endpoints_for_did(self.test_did)

                assert mock_build_get_attrib_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                    "endpoint",
                    None,
                    None,
                )
                assert mock_submit.called_once_with(
                    mock_build_get_attrib_req.return_value,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                assert response is None

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_endpoint_for_did_address_none(
        self, mock_submit, mock_build_get_attrib_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_submit.return_value = json.dumps(
            {"result": {"data": json.dumps({"endpoint": None})}}
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.get_endpoint_for_did(self.test_did)

                assert mock_build_get_attrib_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                    "endpoint",
                    None,
                    None,
                )
                assert mock_submit.called_once_with(
                    mock_build_get_attrib_req.return_value,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                assert response is None

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_endpoint_for_did_no_endpoint(
        self, mock_submit, mock_build_get_attrib_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_submit.return_value = json.dumps({"result": {"data": None}})
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.get_endpoint_for_did(self.test_did)

                assert mock_build_get_attrib_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                    "endpoint",
                    None,
                    None,
                )
                assert mock_submit.called_once_with(
                    mock_build_get_attrib_req.return_value,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                assert response is None

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("indy.ledger.build_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_update_endpoint_for_did(
        self,
        mock_is_ledger_read_only,
        mock_submit,
        mock_build_attrib_req,
        mock_build_get_attrib_req,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        endpoint = ["http://old.aries.ca", "http://new.aries.ca"]
        mock_is_ledger_read_only.return_value = False
        mock_submit.side_effect = [
            json.dumps(
                {
                    "result": {
                        "data": json.dumps({"endpoint": {"endpoint": endpoint[i]}})
                    }
                }
            )
            for i in range(len(endpoint))
        ]
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.update_endpoint_for_did(
                    self.test_did, endpoint[1]
                )

                assert mock_build_get_attrib_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                    "endpoint",
                    None,
                    None,
                )
                mock_submit.assert_has_calls(
                    [
                        async_mock.call(
                            mock_build_get_attrib_req.return_value,
                            sign_did=mock_wallet_get_public_did.return_value,
                        ),
                        async_mock.call(mock_build_attrib_req.return_value, True, True),
                    ]
                )
                assert response

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @pytest.mark.asyncio
    async def test_construct_attr_json_with_routing_keys(self, mock_close, mock_open):
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        async with ledger:
            attr_json = await ledger._construct_attr_json(
                "https://url",
                EndpointType.ENDPOINT,
                routing_keys=["3YJCx3TqotDWFGv7JMR5erEvrmgu5y4FDqjR7sKWxgXn"],
            )
        assert attr_json == json.dumps(
            {
                "endpoint": {
                    "endpoint": "https://url",
                    "routingKeys": ["3YJCx3TqotDWFGv7JMR5erEvrmgu5y4FDqjR7sKWxgXn"],
                }
            }
        )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @pytest.mark.asyncio
    async def test_construct_attr_json_with_routing_keys_all_exist_endpoints(
        self, mock_close, mock_open
    ):
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        async with ledger:
            attr_json = await ledger._construct_attr_json(
                "https://url",
                EndpointType.ENDPOINT,
                all_exist_endpoints={"profile": "https://endpoint/profile"},
                routing_keys=["3YJCx3TqotDWFGv7JMR5erEvrmgu5y4FDqjR7sKWxgXn"],
            )
        assert attr_json == json.dumps(
            {
                "endpoint": {
                    "profile": "https://endpoint/profile",
                    "endpoint": "https://url",
                    "routingKeys": ["3YJCx3TqotDWFGv7JMR5erEvrmgu5y4FDqjR7sKWxgXn"],
                }
            }
        )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("indy.ledger.build_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    @pytest.mark.asyncio
    async def test_update_endpoint_for_did_calls_attr_json(
        self,
        mock_is_ledger_read_only,
        mock_submit,
        mock_build_attrib_req,
        mock_build_get_attrib_req,
        mock_close,
        mock_open,
    ):
        routing_keys = ["3YJCx3TqotDWFGv7JMR5erEvrmgu5y4FDqjR7sKWxgXn"]
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        mock_is_ledger_read_only.return_value = False
        async with ledger:
            with async_mock.patch.object(
                IndySdkWallet, "get_public_did"
            ) as mock_wallet_get_public_did, async_mock.patch.object(
                ledger,
                "_construct_attr_json",
                async_mock.CoroutineMock(
                    return_value=json.dumps(
                        {
                            "endpoint": {
                                "endpoint": {
                                    "endpoint": "https://url",
                                    "routingKeys": [],
                                }
                            }
                        }
                    )
                ),
            ) as mock_construct_attr_json, async_mock.patch.object(
                ledger,
                "get_all_endpoints_for_did",
                async_mock.CoroutineMock(return_value={}),
            ), async_mock.patch.object(
                ledger, "did_to_nym"
            ):
                mock_wallet_get_public_did.return_value = self.test_did_info
                await ledger.update_endpoint_for_did(
                    mock_wallet_get_public_did,
                    "https://url",
                    EndpointType.ENDPOINT,
                    routing_keys=routing_keys,
                )
                mock_construct_attr_json.assert_called_once_with(
                    "https://url", EndpointType.ENDPOINT, {}, routing_keys
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("indy.ledger.build_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_update_endpoint_for_did_no_prior_endpoints(
        self,
        mock_is_ledger_read_only,
        mock_submit,
        mock_build_attrib_req,
        mock_build_get_attrib_req,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        endpoint = "http://new.aries.ca"
        mock_is_ledger_read_only.return_value = False
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                with async_mock.patch.object(
                    ledger, "get_all_endpoints_for_did", async_mock.CoroutineMock()
                ) as mock_get_all:
                    mock_get_all.return_value = None
                    response = await ledger.update_endpoint_for_did(
                        self.test_did, endpoint
                    )

                    assert mock_build_get_attrib_req.called_once_with(
                        self.test_did,
                        ledger.did_to_nym(self.test_did),
                        "endpoint",
                        None,
                        None,
                    )
                    mock_submit.assert_has_calls(
                        [
                            async_mock.call(
                                mock_build_attrib_req.return_value, True, True
                            ),
                        ]
                    )
                    assert response

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("indy.ledger.build_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_update_endpoint_of_type_profile_for_did(
        self,
        mock_is_ledger_read_only,
        mock_submit,
        mock_build_attrib_req,
        mock_build_get_attrib_req,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        endpoint = ["http://company.com/oldProfile", "http://company.com/newProfile"]
        endpoint_type = EndpointType.PROFILE
        mock_is_ledger_read_only.return_value = False
        mock_submit.side_effect = [
            json.dumps(
                {
                    "result": {
                        "data": json.dumps(
                            {"endpoint": {endpoint_type.indy: endpoint[i]}}
                        )
                    }
                }
            )
            for i in range(len(endpoint))
        ]
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        # ledger = async_mock.patch.object(
        #    ledger,
        #    "is_ledger_read_only",
        #    async_mock.CoroutineMock(return_value=False),
        # )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.update_endpoint_for_did(
                    self.test_did, endpoint[1], endpoint_type
                )

                assert mock_build_get_attrib_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                    "endpoint",
                    None,
                    None,
                )
                mock_submit.assert_has_calls(
                    [
                        async_mock.call(
                            mock_build_get_attrib_req.return_value,
                            sign_did=mock_wallet_get_public_did.return_value,
                        ),
                        async_mock.call(mock_build_attrib_req.return_value, True, True),
                    ]
                )
                assert response

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_update_endpoint_for_did_duplicate(
        self, mock_submit, mock_build_get_attrib_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        endpoint = "http://aries.ca"
        mock_submit.return_value = json.dumps(
            {"result": {"data": json.dumps({"endpoint": {"endpoint": endpoint}})}}
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.update_endpoint_for_did(self.test_did, endpoint)

                assert mock_build_get_attrib_req.called_once_with(
                    self.test_did,
                    ledger.did_to_nym(self.test_did),
                    "endpoint",
                    None,
                    None,
                )
                assert mock_submit.called_once_with(
                    mock_build_get_attrib_req.return_value,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                assert not response

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_attrib_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_update_endpoint_for_did_read_only(
        self, mock_submit, mock_build_get_attrib_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        endpoint = "http://aries.ca"
        mock_submit.return_value = json.dumps(
            {"result": {"data": json.dumps({"endpoint": {"endpoint": endpoint}})}}
        )
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                with self.assertRaises(LedgerError) as context:
                    await ledger.update_endpoint_for_did(
                        self.test_did, "distinct endpoint"
                    )
                assert "read only" in str(context.exception)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_register_nym(
        self,
        mock_is_ledger_read_only,
        mock_submit,
        mock_build_nym_req,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_is_ledger_read_only.return_value = False
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            IndySdkWallet, "get_local_did"
        ) as mock_wallet_get_local_did, async_mock.patch.object(
            IndySdkWallet, "replace_local_did_metadata"
        ) as mock_wallet_replace_local_did_metadata:
            ledger = IndySdkLedger(
                IndySdkLedgerPool("name", checked=True), self.profile
            )
            mock_wallet_get_public_did.return_value = self.test_did_info
            mock_wallet_get_local_did.return_value = self.test_did_info
            mock_wallet_replace_local_did_metadata.return_value = (
                async_mock.CoroutineMock()
            )
            async with ledger:
                await ledger.register_nym(
                    self.test_did,
                    self.test_verkey,
                    "alias",
                    None,
                )
                assert mock_build_nym_req.called_once_with(
                    self.test_did,
                    self.test_did,
                    self.test_verkey,
                    "alias",
                    None,
                )
                assert mock_submit.called_once_with(
                    mock_build_nym_req.return_value,
                    True,
                    True,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                mock_wallet_replace_local_did_metadata.assert_called_once_with(
                    self.test_did_info.did,
                    {
                        "test": "test",
                        **DIDPosture.POSTED.metadata,
                    },
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    async def test_register_nym_read_only(self, mock_close, mock_open):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                with self.assertRaises(LedgerError) as context:
                    await ledger.register_nym(
                        self.test_did,
                        self.test_verkey,
                        "alias",
                        None,
                    )
                assert "read only" in str(context.exception)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_register_nym_no_public_did(
        self,
        mock_is_ledger_read_only,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock(
            type="indy",
            get_local_did=async_mock.CoroutineMock(),
            replace_local_did_metadata=async_mock.CoroutineMock(),
        )
        mock_is_ledger_read_only.return_value = False
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = None
            async with ledger:
                with self.assertRaises(WalletNotFoundError):
                    await ledger.register_nym(
                        self.test_did,
                        self.test_verkey,
                        "alias",
                        None,
                    )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_register_nym_ledger_x(
        self,
        mock_is_ledger_read_only,
        mock_submit,
        mock_build_nym_req,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        mock_build_nym_req.side_effect = IndyError(
            error_code=ErrorCode.CommonInvalidParam1,
            error_details={"message": "not today"},
        )
        mock_is_ledger_read_only.return_value = False
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                with self.assertRaises(LedgerError):
                    await ledger.register_nym(
                        self.test_did,
                        self.test_verkey,
                        "alias",
                        None,
                    )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.is_ledger_read_only")
    async def test_register_nym_steward_register_others_did(
        self,
        mock_is_ledger_read_only,
        mock_submit,
        mock_build_nym_req,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_is_ledger_read_only.return_value = False
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            IndySdkWallet, "get_local_did"
        ) as mock_wallet_get_local_did, async_mock.patch.object(
            IndySdkWallet, "replace_local_did_metadata"
        ) as mock_wallet_replace_local_did_metadata:
            mock_wallet_get_public_did.return_value = self.test_did_info
            mock_wallet_get_local_did.side_effect = WalletNotFoundError()
            mock_wallet_replace_local_did_metadata.return_value = (
                async_mock.CoroutineMock()
            )
            async with ledger:
                await ledger.register_nym(
                    self.test_did,
                    self.test_verkey,
                    "alias",
                    None,
                )
                assert mock_build_nym_req.called_once_with(
                    self.test_did,
                    self.test_did,
                    self.test_verkey,
                    "alias",
                    None,
                )
                assert mock_submit.called_once_with(
                    mock_build_nym_req.return_value,
                    True,
                    True,
                    sign_did=mock_wallet_get_public_did.return_value,
                )
                mock_wallet_replace_local_did_metadata.assert_not_called()

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_nym_role(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_submit.return_value = json.dumps(
            {
                "result": {
                    "dest": "GjZWsBLgZCR18aL468JAT7w9CZRiBnpxUPPgyQxh4voa",
                    "txnTime": 1597858571,
                    "reqId": 1597858571783588400,
                    "state_proof": {
                        "root_hash": "7K26MUQt8E2X1vsRJUmc2298VtY8YC5BSDfT5CRJeUDi",
                        "proof_nodes": "+QHo...",
                        "multi_signature": {
                            "participants": ["Node4", "Node3", "Node2"],
                            "value": {
                                "state_root_hash": "7K2...",
                                "pool_state_root_hash": "GT8...",
                                "ledger_id": 1,
                                "txn_root_hash": "Hnr...",
                                "timestamp": 1597858571,
                            },
                            "signature": "QuX...",
                        },
                    },
                    "data": json.dumps(
                        {
                            "dest": "GjZWsBLgZCR18aL468JAT7w9CZRiBnpxUPPgyQxh4voa",
                            "identifier": "V4SGRU86Z58d6TV7PBUe6f",
                            "role": 101,
                            "seqNo": 11,
                            "txnTime": 1597858571,
                            "verkey": "GjZWsBLgZCR18aL468JAT7w9CZRiBnpxUPPgyQxh4voa",
                        }
                    ),
                    "seqNo": 11,
                    "identifier": "GjZWsBLgZCR18aL468JAT7w9CZRiBnpxUPPgyQxh4voa",
                    "type": "105",
                },
                "op": "REPLY",
            }
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                assert await ledger.get_nym_role(self.test_did) == Role.ENDORSER
                assert mock_build_get_nym_req.called_once_with(
                    self.test_did,
                    self.test_did,
                )
                assert mock_submit.called_once_with(mock_build_get_nym_req.return_value)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    async def test_get_nym_role_indy_x(
        self, mock_build_get_nym_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_build_get_nym_req.side_effect = IndyError(
            error_code=ErrorCode.CommonInvalidParam1,
            error_details={"message": "not today"},
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                with self.assertRaises(LedgerError) as context:
                    await ledger.get_nym_role(self.test_did)
                assert "not today" in context.exception.message

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_nym_role_did_not_public_x(
        self, mock_submit, mock_build_get_nym_req, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_submit.return_value = json.dumps(
            {
                "result": {
                    "dest": "GjZWsBLgZCR18aL468JAT7w9CZRiBnpxUPPgyQxh4voa",
                    "txnTime": 1597858571,
                    "reqId": 1597858571783588400,
                    "state_proof": {
                        "root_hash": "7K26MUQt8E2X1vsRJUmc2298VtY8YC5BSDfT5CRJeUDi",
                        "proof_nodes": "+QHo...",
                        "multi_signature": {
                            "participants": ["Node4", "Node3", "Node2"],
                            "value": {
                                "state_root_hash": "7K2...",
                                "pool_state_root_hash": "GT8...",
                                "ledger_id": 1,
                                "txn_root_hash": "Hnr...",
                                "timestamp": 1597858571,
                            },
                            "signature": "QuX...",
                        },
                    },
                    "data": json.dumps(None),
                    "seqNo": 11,
                    "identifier": "GjZWsBLgZCR18aL468JAT7w9CZRiBnpxUPPgyQxh4voa",
                    "type": "105",
                },
                "op": "REPLY",
            }
        )
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                with self.assertRaises(BadLedgerRequestError):
                    await ledger.get_nym_role(self.test_did)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("indy.ledger.build_get_txn_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.register_nym")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_rotate_public_did_keypair(
        self,
        mock_submit,
        mock_register_nym,
        mock_build_get_txn_request,
        mock_build_get_nym_request,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_submit.side_effect = [
            json.dumps({"result": {"data": json.dumps({"seqNo": 1234})}}),
            json.dumps(
                {
                    "result": {
                        "data": {"txn": {"data": {"role": "101", "alias": "Billy"}}}
                    }
                }
            ),
        ]
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            IndySdkWallet, "rotate_did_keypair_start", autospec=True
        ) as mock_wallet_rotate_did_keypair_start, async_mock.patch.object(
            IndySdkWallet, "rotate_did_keypair_apply", autospec=True
        ) as mock_wallet_rotate_did_keypair_apply:
            mock_wallet_get_public_did.return_value = self.test_did_info
            mock_wallet_rotate_did_keypair_start.return_value = self.test_verkey
            mock_wallet_rotate_did_keypair_apply.return_value = None
            async with ledger:
                await ledger.rotate_public_did_keypair()

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_rotate_public_did_keypair_no_nym(
        self, mock_submit, mock_build_get_nym_request, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_submit.return_value = json.dumps({"result": {"data": json.dumps(None)}})
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)

        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            IndySdkWallet, "rotate_did_keypair_start", autospec=True
        ) as mock_wallet_rotate_did_keypair_start, async_mock.patch.object(
            IndySdkWallet, "rotate_did_keypair_apply", autospec=True
        ) as mock_wallet_rotate_did_keypair_apply:
            mock_wallet_get_public_did.return_value = self.test_did_info
            mock_wallet_rotate_did_keypair_start.return_value = self.test_verkey
            mock_wallet_rotate_did_keypair_apply.return_value = None
            async with ledger:
                with self.assertRaises(BadLedgerRequestError):
                    await ledger.rotate_public_did_keypair()

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_nym_request")
    @async_mock.patch("indy.ledger.build_get_txn_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.register_nym")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_rotate_public_did_keypair_corrupt_nym_txn(
        self,
        mock_submit,
        mock_register_nym,
        mock_build_get_txn_request,
        mock_build_get_nym_request,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_submit.side_effect = [
            json.dumps({"result": {"data": json.dumps({"seqNo": 1234})}}),
            json.dumps({"result": {"data": None}}),
        ]
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            IndySdkWallet, "rotate_did_keypair_start", autospec=True
        ) as mock_wallet_rotate_did_keypair_start, async_mock.patch.object(
            IndySdkWallet, "rotate_did_keypair_apply", autospec=True
        ) as mock_wallet_rotate_did_keypair_apply:
            mock_wallet_get_public_did.return_value = self.test_did_info
            mock_wallet_rotate_did_keypair_start.return_value = self.test_verkey
            mock_wallet_rotate_did_keypair_apply.return_value = None
            async with ledger:
                with self.assertRaises(BadLedgerRequestError):
                    await ledger.rotate_public_did_keypair()

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_revoc_reg_def_request")
    @async_mock.patch("indy.ledger.parse_get_revoc_reg_def_response")
    async def test_get_revoc_reg_def(
        self,
        mock_indy_parse_get_rrdef_resp,
        mock_indy_build_get_rrdef_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_parse_get_rrdef_resp.return_value = (
            "rr-id",
            json.dumps({"...": "..."}),
        )
        mock_submit.return_value = json.dumps({"result": {"txnTime": 1234567890}})

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                result = await ledger.get_revoc_reg_def("rr-id")
                assert result == {"...": "...", "txnTime": 1234567890}

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_revoc_reg_def_request")
    async def test_get_revoc_reg_def_indy_x(
        self, mock_indy_build_get_rrdef_req, mock_submit, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        mock_indy_build_get_rrdef_req.side_effect = IndyError(
            error_code=ErrorCode.CommonInvalidParam1,
            error_details={"message": "not today"},
        )
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                with self.assertRaises(IndyError) as context:
                    await ledger.get_revoc_reg_def("rr-id")
                assert "not today" in context.exception.message

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_revoc_reg_request")
    @async_mock.patch("indy.ledger.parse_get_revoc_reg_response")
    async def test_get_revoc_reg_entry(
        self,
        mock_indy_parse_get_rr_resp,
        mock_indy_build_get_rr_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_parse_get_rr_resp.return_value = (
            "rr-id",
            '{"hello": "world"}',
            1234567890,
        )

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                (result, _) = await ledger.get_revoc_reg_entry("rr-id", 1234567890)
                assert result == {"hello": "world"}

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_revoc_reg_request")
    @async_mock.patch("indy.ledger.parse_get_revoc_reg_response")
    async def test_get_revoc_reg_entry_x(
        self,
        mock_indy_parse_get_rr_resp,
        mock_indy_build_get_rr_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_parse_get_rr_resp.side_effect = IndyError(
            error_code=ErrorCode.PoolLedgerTimeout,
            error_details={"message": "bye"},
        )
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            with self.assertRaises(LedgerError):
                async with ledger:
                    await ledger.get_revoc_reg_entry("rr-id", 1234567890)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_get_revoc_reg_delta_request")
    @async_mock.patch("indy.ledger.parse_get_revoc_reg_delta_response")
    async def test_get_revoc_reg_delta(
        self,
        mock_indy_parse_get_rrd_resp,
        mock_indy_build_get_rrd_req,
        mock_submit,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_parse_get_rrd_resp.return_value = (
            "rr-id",
            '{"hello": "world"}',
            1234567890,
        )

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                (result, _) = await ledger.get_revoc_reg_delta("rr-id")
                assert result == {"hello": "world"}

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_revoc_reg_def_request")
    async def test_send_revoc_reg_def_public_did(
        self, mock_indy_build_rrdef_req, mock_submit, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_build_rrdef_req.return_value = '{"hello": "world"}'

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            IndySdkWallet, "get_local_did"
        ) as mock_wallet_get_local_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                await ledger.send_revoc_reg_def({"rr": "def"}, issuer_did=None)
                mock_wallet_get_public_did.assert_called_once()
                assert not mock_wallet_get_local_did.called
                mock_submit.assert_called_once_with(
                    mock_indy_build_rrdef_req.return_value,
                    True,
                    sign_did=self.test_did_info,
                    write_ledger=True,
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_revoc_reg_def_request")
    async def test_send_revoc_reg_def_local_did(
        self, mock_indy_build_rrdef_req, mock_submit, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_build_rrdef_req.return_value = '{"hello": "world"}'

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_local_did"
        ) as mock_wallet_get_local_did:
            mock_wallet_get_local_did.return_value = self.test_did_info
            async with ledger:
                await ledger.send_revoc_reg_def(
                    {"rr": "def"},
                    issuer_did=self.test_did,
                )
                mock_wallet_get_local_did.assert_called_once_with(self.test_did)
                mock_submit.assert_called_once_with(
                    mock_indy_build_rrdef_req.return_value,
                    True,
                    sign_did=self.test_did_info,
                    write_ledger=True,
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_revoc_reg_def_request")
    async def test_send_revoc_reg_def_x_no_did(
        self, mock_indy_build_rrdef_req, mock_submit, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_build_rrdef_req.return_value = '{"hello": "world"}'

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_local_did"
        ) as mock_wallet_get_local_did:
            mock_wallet_get_local_did.return_value = None
            async with ledger:
                with self.assertRaises(LedgerTransactionError) as context:
                    await ledger.send_revoc_reg_def(
                        {"rr": "def"},
                        issuer_did=self.test_did,
                    )
                assert "No issuer DID found for revocation registry definition" in str(
                    context.exception
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_revoc_reg_entry_request")
    async def test_send_revoc_reg_entry_public_did(
        self, mock_indy_build_rre_req, mock_submit, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_build_rre_req.return_value = '{"hello": "world"}'

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did, async_mock.patch.object(
            IndySdkWallet, "get_local_did"
        ) as mock_wallet_get_local_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                await ledger.send_revoc_reg_entry(
                    "rr-id", "CL_ACCUM", {"rev-reg": "entry"}, issuer_did=None
                )
                mock_wallet_get_public_did.assert_called_once()
                assert not mock_wallet_get_local_did.called
                mock_submit.assert_called_once_with(
                    mock_indy_build_rre_req.return_value,
                    True,
                    sign_did=self.test_did_info,
                    write_ledger=True,
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_revoc_reg_entry_request")
    async def test_send_revoc_reg_entry_local_did(
        self, mock_indy_build_rre_req, mock_submit, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_build_rre_req.return_value = '{"hello": "world"}'

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_local_did"
        ) as mock_wallet_get_local_did:
            mock_wallet_get_local_did.return_value = self.test_did_info
            async with ledger:
                result = await ledger.send_revoc_reg_entry(
                    "rr-id",
                    "CL_ACCUM",
                    {"rev-reg": "entry"},
                    issuer_did=self.test_did,
                )
                mock_wallet_get_local_did.assert_called_once_with(self.test_did)
                mock_submit.assert_called_once_with(
                    mock_indy_build_rre_req.return_value,
                    True,
                    sign_did=self.test_did_info,
                    write_ledger=True,
                )

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    @async_mock.patch("indy.ledger.build_revoc_reg_entry_request")
    async def test_send_revoc_reg_entry_x_no_did(
        self, mock_indy_build_rre_req, mock_submit, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        mock_indy_build_rre_req.return_value = '{"hello": "world"}'

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, read_only=True), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_local_did"
        ) as mock_wallet_get_local_did:
            mock_wallet_get_local_did.return_value = None
            async with ledger:
                with self.assertRaises(LedgerTransactionError) as context:
                    await ledger.send_revoc_reg_entry(
                        "rr-id",
                        "CL_ACCUM",
                        {"rev-reg": "entry"},
                        issuer_did=self.test_did,
                    )
                assert "No issuer DID found for revocation registry entry" in str(
                    context.exception
                )

    @async_mock.patch("indy.pool.open_pool_ledger")
    @async_mock.patch("indy.pool.close_pool_ledger")
    async def test_taa_digest_bad_value(
        self,
        mock_close_pool,
        mock_open_ledger,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                with self.assertRaises(ValueError):
                    await ledger.taa_digest(None, None)

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("indy.ledger.build_get_acceptance_mechanisms_request")
    @async_mock.patch("indy.ledger.build_get_txn_author_agreement_request")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger._submit")
    async def test_get_txn_author_agreement(
        self,
        mock_submit,
        mock_build_get_taa_req,
        mock_build_get_acc_mech_req,
        mock_close,
        mock_open,
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        txn_result_data = {"text": "text", "version": "1.0"}
        mock_submit.side_effect = [
            json.dumps({"result": {"data": txn_result_data}}) for i in range(2)
        ]
        ledger = IndySdkLedger(IndySdkLedgerPool("name", checked=True), self.profile)
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                response = await ledger.get_txn_author_agreement(reload=True)

                assert mock_build_get_acc_mech_req.called_once_with(
                    self.test_did, None, None
                )
                assert mock_build_get_taa_req.called_once_with(
                    self.test_did,
                    None,
                )
                mock_submit.assert_has_calls(
                    [
                        async_mock.call(
                            mock_build_get_acc_mech_req.return_value,
                            sign_did=mock_wallet_get_public_did.return_value,
                        ),
                        async_mock.call(
                            mock_build_get_taa_req.return_value,
                            sign_did=mock_wallet_get_public_did.return_value,
                        ),
                    ]
                )
                assert response == {
                    "aml_record": txn_result_data,
                    "taa_record": {
                        **txn_result_data,
                        "digest": ledger.taa_digest(
                            txn_result_data["version"], txn_result_data["text"]
                        ),
                    },
                    "taa_required": True,
                }

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.add_record")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.find_all_records")
    async def test_accept_and_get_latest_txn_author_agreement(
        self, mock_find_all_records, mock_add_record, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, cache=InMemoryCache()), self.profile
        )

        accept_time = ledger.taa_rough_timestamp()
        taa_record = {
            "text": "text",
            "version": "1.0",
            "digest": "abcd1234",
        }
        acceptance = {
            "text": taa_record["text"],
            "version": taa_record["version"],
            "digest": taa_record["digest"],
            "mechanism": "dummy",
            "time": accept_time,
        }

        mock_find_all_records.return_value = [
            StorageRecord(
                TAA_ACCEPTED_RECORD_TYPE,
                json.dumps(acceptance),
                {"pool_name": ledger.pool_name},
            )
        ]
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                await ledger.accept_txn_author_agreement(
                    taa_record=taa_record, mechanism="dummy", accept_time=None
                )

                await ledger.pool.cache.clear(
                    f"{TAA_ACCEPTED_RECORD_TYPE}::{ledger.pool_name}"
                )
                for i in range(2):  # populate, then get from, cache
                    response = await ledger.get_latest_txn_author_acceptance()
                    assert response == acceptance

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.storage.indy.IndySdkStorage.find_all_records")
    async def test_get_latest_txn_author_agreement_none(
        self, mock_find_all_records, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, cache=InMemoryCache()), self.profile
        )

        mock_find_all_records.return_value = []
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                await ledger.pool.cache.clear(
                    f"{TAA_ACCEPTED_RECORD_TYPE}::{ledger.pool_name}"
                )
                response = await ledger.get_latest_txn_author_acceptance()
                assert response == {}

    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_open")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedgerPool.context_close")
    @async_mock.patch("aries_cloudagent.ledger.indy.IndySdkLedger.get_schema")
    async def test_credential_definition_id2schema_id(
        self, mock_get_schema, mock_close, mock_open
    ):
        mock_wallet = async_mock.MagicMock()
        self.session.context.injector.bind_provider(BaseWallet, mock_wallet)
        S_ID = f"{self.test_did}:2:favourite_drink:1.0"
        SEQ_NO = "9999"
        mock_get_schema.return_value = {"id": S_ID}

        ledger = IndySdkLedger(
            IndySdkLedgerPool("name", checked=True, cache=InMemoryCache()), self.profile
        )
        with async_mock.patch.object(
            IndySdkWallet, "get_public_did"
        ) as mock_wallet_get_public_did:
            mock_wallet_get_public_did.return_value = self.test_did_info
            async with ledger:
                s_id_short = await ledger.credential_definition_id2schema_id(
                    f"{self.test_did}:3:CL:{SEQ_NO}:tag"
                )

                mock_get_schema.assert_called_once_with(SEQ_NO)

                assert s_id_short == S_ID
                s_id_long = await ledger.credential_definition_id2schema_id(
                    f"{self.test_did}:3:CL:{s_id_short}:tag"
                )
                assert s_id_long == s_id_short

    def test_error_handler(self):
        try:  # with self.assertRaises() makes a copy of exception, loses traceback!
            with IndyErrorHandler("message", LedgerTransactionError):
                try:
                    1 / 0
                except ZeroDivisionError as zx:
                    ix = IndyError(error_code=1, error_details={"message": "bye"})
                    ix.__traceback__ = zx.__traceback__
                    raise ix
        except LedgerTransactionError as err:
            assert type(err) == LedgerTransactionError
            assert type(err.__cause__) == IndyError
            assert err.__traceback__
            assert "bye" in err.message
