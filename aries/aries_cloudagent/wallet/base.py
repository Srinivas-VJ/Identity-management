"""Wallet base class."""

from abc import ABC, abstractmethod
from typing import List, Sequence, Tuple, Union

from ..ledger.base import BaseLedger
from ..ledger.endpoint_type import EndpointType
from .error import WalletError

from .did_info import DIDInfo, KeyInfo
from .key_type import KeyType
from .did_method import SOV, DIDMethod


class BaseWallet(ABC):
    """Abstract wallet interface."""

    @abstractmethod
    async def create_signing_key(
        self, key_type: KeyType, seed: str = None, metadata: dict = None
    ) -> KeyInfo:
        """Create a new public/private signing keypair.

        Args:
            key_type: Key type to create
            seed: Optional seed allowing deterministic key creation
            metadata: Optional metadata to store with the keypair

        Returns:
            A `KeyInfo` representing the new record

        """

    @abstractmethod
    async def get_signing_key(self, verkey: str) -> KeyInfo:
        """
        Fetch info for a signing keypair.

        Args:
            verkey: The verification key of the keypair

        Returns:
            A `KeyInfo` representing the keypair

        """

    @abstractmethod
    async def replace_signing_key_metadata(self, verkey: str, metadata: dict):
        """
        Replace the metadata associated with a signing keypair.

        Args:
            verkey: The verification key of the keypair
            metadata: The new metadata to store

        """

    @abstractmethod
    async def rotate_did_keypair_start(self, did: str, next_seed: str = None) -> str:
        """
        Begin key rotation for DID that wallet owns: generate new keypair.

        Args:
            did: signing DID
            next_seed: seed for incoming ed25519 key pair (default random)

        Returns:
            The new verification key

        Raises:
            WalletNotFoundError: if wallet does not own DID

        """

    @abstractmethod
    async def rotate_did_keypair_apply(self, did: str) -> None:
        """
        Apply temporary keypair as main for DID that wallet owns.

        Args:
            did: signing DID

        Raises:
            WalletNotFoundError: if wallet does not own DID
            WalletError: if wallet has not started key rotation

        """

    @abstractmethod
    async def create_local_did(
        self,
        method: DIDMethod,
        key_type: KeyType,
        seed: str = None,
        did: str = None,
        metadata: dict = None,
    ) -> DIDInfo:
        """
        Create and store a new local DID.

        Args:
            method: The method to use for the DID
            key_type: The key type to use for the DID
            seed: Optional seed to use for DID
            did: The DID to use
            metadata: Metadata to store with DID

        Returns:
            The created `DIDInfo`

        """

    async def create_public_did(
        self,
        method: DIDMethod,
        key_type: KeyType,
        seed: str = None,
        did: str = None,
        metadata: dict = {},
    ) -> DIDInfo:
        """
        Create and store a new public DID.

        Args:
            seed: Optional seed to use for DID
            did: The DID to use
            metadata: Metadata to store with DID

        Returns:
            The created `DIDInfo`

        """
        metadata = metadata or {}
        metadata.setdefault("posted", True)
        did_info = await self.create_local_did(
            method=method, key_type=key_type, seed=seed, did=did, metadata=metadata
        )
        return await self.set_public_did(did_info)

    @abstractmethod
    async def get_public_did(self) -> DIDInfo:
        """
        Retrieve the public DID.

        Returns:
            The currently public `DIDInfo`, if any

        """

    @abstractmethod
    async def set_public_did(self, did: Union[str, DIDInfo]) -> DIDInfo:
        """
        Assign the public DID.

        Returns:
            The updated `DIDInfo`

        """

    @abstractmethod
    async def get_local_dids(self) -> Sequence[DIDInfo]:
        """
        Get list of defined local DIDs.

        Returns:
            A list of `DIDInfo` instances

        """

    @abstractmethod
    async def get_local_did(self, did: str) -> DIDInfo:
        """
        Find info for a local DID.

        Args:
            did: The DID for which to get info

        Returns:
            A `DIDInfo` instance for the DID

        """

    @abstractmethod
    async def get_local_did_for_verkey(self, verkey: str) -> DIDInfo:
        """
        Resolve a local DID from a verkey.

        Args:
            verkey: Verkey for which to get DID info

        Returns:
            A `DIDInfo` instance for the DID

        """

    @abstractmethod
    async def replace_local_did_metadata(self, did: str, metadata: dict):
        """
        Replace the metadata associated with a local DID.

        Prefer `set_did_endpoint()` to set endpoint in metadata.

        Args:
            did: DID for which to replace metadata
            metadata: The new metadata

        """

    async def get_posted_dids(self) -> Sequence[DIDInfo]:
        """
        Get list of defined posted DIDs.

        Returns:
            A list of `DIDInfo` instances

        """
        return [
            info for info in await self.get_local_dids() if info.metadata.get("posted")
        ]

    async def set_did_endpoint(
        self,
        did: str,
        endpoint: str,
        _ledger: BaseLedger,
        endpoint_type: EndpointType = None,
        write_ledger: bool = True,
        endorser_did: str = None,
        routing_keys: List[str] = None,
    ):
        """
        Update the endpoint for a DID in the wallet, send to ledger if public or posted.

        Args:
            did: DID for which to set endpoint
            endpoint: the endpoint to set, None to clear
            ledger: the ledger to which to send endpoint update if
                DID is public or posted
            endpoint_type: the type of the endpoint/service. Only endpoint_type
                'endpoint' affects local wallet
        """
        did_info = await self.get_local_did(did)

        if did_info.method != SOV:
            raise WalletError("Setting DID endpoint is only allowed for did:sov DIDs")
        metadata = {**did_info.metadata}
        if not endpoint_type:
            endpoint_type = EndpointType.ENDPOINT
        if endpoint_type == EndpointType.ENDPOINT:
            metadata[endpoint_type.indy] = endpoint

        await self.replace_local_did_metadata(did, metadata)

    @abstractmethod
    async def sign_message(
        self, message: Union[List[bytes], bytes], from_verkey: str
    ) -> bytes:
        """
        Sign message(s) using the private key associated with a given verkey.

        Args:
            message: The message(s) to sign
            from_verkey: Sign using the private key related to this verkey

        Returns:
            The signature

        """

    @abstractmethod
    async def verify_message(
        self,
        message: Union[List[bytes], bytes],
        signature: bytes,
        from_verkey: str,
        key_type: KeyType,
    ) -> bool:
        """
        Verify a signature against the public key of the signer.

        Args:
            message: The message to verify
            signature: The signature to verify
            from_verkey: Verkey to use in verification
            key_type: The key type to derive the signature verification algorithm from

        Returns:
            True if verified, else False

        """

    @abstractmethod
    async def pack_message(
        self, message: str, to_verkeys: Sequence[str], from_verkey: str = None
    ) -> bytes:
        """
        Pack a message for one or more recipients.

        Args:
            message: The message to pack
            to_verkeys: The verkeys to pack the message for
            from_verkey: The sender verkey

        Returns:
            The packed message

        """

    @abstractmethod
    async def unpack_message(self, enc_message: bytes) -> Tuple[str, str, str]:
        """
        Unpack a message.

        Args:
            enc_message: The encrypted message

        Returns:
            A tuple: (message, from_verkey, to_verkey)

        """

    def __repr__(self) -> str:
        """Get a human readable string."""
        return "<{}>".format(self.__class__.__name__)
