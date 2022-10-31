"""Indy holder implementation."""

import asyncio
import json
import logging
import re
import uuid

from typing import Dict, Sequence, Tuple, Union

from aries_askar import AskarError, AskarErrorCode
from indy_credx import (
    CredxError,
    Credential,
    CredentialRequest,
    CredentialRevocationState,
    MasterSecret,
    Presentation,
    PresentCredentials,
)

from ...askar.profile import AskarProfile
from ...ledger.base import BaseLedger
from ...wallet.error import WalletNotFoundError

from ..holder import IndyHolder, IndyHolderError

LOGGER = logging.getLogger(__name__)

CATEGORY_CREDENTIAL = "credential"
CATEGORY_MASTER_SECRET = "master_secret"


def _make_cred_info(cred_id, cred: Credential):
    cred_info = cred.to_dict()  # not secure!
    rev_info = cred_info["signature"]["r_credential"]
    return {
        "referent": cred_id,
        "schema_id": cred_info["schema_id"],
        "cred_def_id": cred_info["cred_def_id"],
        "rev_reg_id": cred_info["rev_reg_id"],
        "cred_rev_id": str(rev_info["i"]) if rev_info else None,
        "attrs": {name: val["raw"] for (name, val) in cred_info["values"].items()},
    }


def _normalize_attr_name(name: str) -> str:
    return name.replace(" ", "")


class IndyCredxHolder(IndyHolder):
    """Indy-credx holder class."""

    MASTER_SECRET_ID = "default"

    def __init__(self, profile: AskarProfile):
        """
        Initialize an IndyCredxHolder instance.

        Args:
            profile: The active profile instance

        """
        self._profile = profile

    @property
    def profile(self) -> AskarProfile:
        """Accessor for the profile instance."""
        return self._profile

    async def get_master_secret(self) -> MasterSecret:
        """Get or create the default master secret."""

        while True:
            async with self._profile.session() as session:
                try:
                    record = await session.handle.fetch(
                        CATEGORY_MASTER_SECRET, IndyCredxHolder.MASTER_SECRET_ID
                    )
                except AskarError as err:
                    raise IndyHolderError("Error fetching master secret") from err
                if record:
                    try:
                        secret = MasterSecret.load(record.raw_value)
                    except CredxError as err:
                        raise IndyHolderError("Error loading master secret") from err
                    break
                else:
                    try:
                        secret = MasterSecret.create()
                    except CredxError as err:
                        raise IndyHolderError("Error creating master secret") from err
                    try:
                        await session.handle.insert(
                            CATEGORY_MASTER_SECRET,
                            IndyCredxHolder.MASTER_SECRET_ID,
                            secret.to_json_buffer(),
                        )
                    except AskarError as err:
                        if err.code != AskarErrorCode.DUPLICATE:
                            raise IndyHolderError("Error saving master secret") from err
                        # else: lost race to create record, retry
                    else:
                        break
        return secret

    async def create_credential_request(
        self, credential_offer: dict, credential_definition: dict, holder_did: str
    ) -> Tuple[str, str]:
        """
        Create a credential request for the given credential offer.

        Args:
            credential_offer: The credential offer to create request for
            credential_definition: The credential definition to create an offer for
            holder_did: the DID of the agent making the request

        Returns:
            A tuple of the credential request and credential request metadata

        """
        try:
            secret = await self.get_master_secret()
            (
                cred_req,
                cred_req_metadata,
            ) = await asyncio.get_event_loop().run_in_executor(
                None,
                CredentialRequest.create,
                holder_did,
                credential_definition,
                secret,
                IndyCredxHolder.MASTER_SECRET_ID,
                credential_offer,
            )
        except CredxError as err:
            raise IndyHolderError("Error creating credential request") from err
        cred_req_json, cred_req_metadata_json = (
            cred_req.to_json(),
            cred_req_metadata.to_json(),
        )

        LOGGER.debug(
            "Created credential request. "
            "credential_request_json=%s credential_request_metadata_json=%s",
            cred_req_json,
            cred_req_metadata_json,
        )

        return cred_req_json, cred_req_metadata_json

    async def store_credential(
        self,
        credential_definition: dict,
        credential_data: dict,
        credential_request_metadata: dict,
        credential_attr_mime_types: dict = None,
        credential_id: str = None,
        rev_reg_def: dict = None,
    ) -> str:
        """
        Store a credential in the wallet.

        Args:
            credential_definition: Credential definition for this credential
            credential_data: Credential data generated by the issuer
            credential_request_metadata: credential request metadata generated
                by the issuer
            credential_attr_mime_types: dict mapping attribute names to (optional)
                MIME types to store as non-secret record, if specified
            credential_id: optionally override the stored credential id
            rev_reg_def: revocation registry definition in json

        Returns:
            the ID of the stored credential

        """
        try:
            secret = await self.get_master_secret()
            cred = Credential.load(credential_data)
            cred_recvd = await asyncio.get_event_loop().run_in_executor(
                None,
                cred.process,
                credential_request_metadata,
                secret,
                credential_definition,
                rev_reg_def,
            )
        except CredxError as err:
            raise IndyHolderError("Error processing received credential") from err

        schema_id = cred_recvd.schema_id
        schema_id_parts = re.match(r"^(\w+):2:([^:]+):([^:]+)$", schema_id)
        if not schema_id_parts:
            raise IndyHolderError(f"Error parsing credential schema ID: {schema_id}")
        cred_def_id = cred_recvd.cred_def_id
        cdef_id_parts = re.match(r"^(\w+):3:CL:([^:]+):([^:]+)$", cred_def_id)
        if not cdef_id_parts:
            raise IndyHolderError(
                f"Error parsing credential definition ID: {cred_def_id}"
            )

        credential_id = credential_id or str(uuid.uuid4())
        tags = {
            "schema_id": schema_id,
            "schema_issuer_did": schema_id_parts[1],
            "schema_name": schema_id_parts[2],
            "schema_version": schema_id_parts[3],
            "issuer_did": cdef_id_parts[1],
            "cred_def_id": cred_def_id,
            "rev_reg_id": cred_recvd.rev_reg_id or "None",
        }

        # FIXME - sdk has some special handling for fully qualified DIDs here

        mime_types = {}
        for k, attr_value in credential_data["values"].items():
            attr_name = _normalize_attr_name(k)
            # tags[f"attr::{attr_name}::marker"] = "1"
            tags[f"attr::{attr_name}::value"] = attr_value["raw"]
            if credential_attr_mime_types and k in credential_attr_mime_types:
                mime_types[k] = credential_attr_mime_types[k]

        try:
            async with self._profile.transaction() as txn:
                await txn.handle.insert(
                    CATEGORY_CREDENTIAL,
                    credential_id,
                    cred_recvd.to_json_buffer(),
                    tags=tags,
                )
                if mime_types:
                    await txn.handle.insert(
                        IndyHolder.RECORD_TYPE_MIME_TYPES,
                        credential_id,
                        value_json=mime_types,
                    )
                await txn.commit()
        except AskarError as err:
            raise IndyHolderError("Error storing credential") from err

        return credential_id

    async def get_credentials(self, start: int, count: int, wql: dict):
        """
        Get credentials stored in the wallet.

        Args:
            start: Starting index
            count: Number of records to return
            wql: wql query dict

        """

        result = []

        try:
            rows = self._profile.store.scan(
                CATEGORY_CREDENTIAL,
                wql,
                start,
                count,
                self._profile.settings.get("wallet.askar_profile"),
            )
            async for row in rows:
                cred = Credential.load(row.raw_value)
                result.append(_make_cred_info(row.name, cred))
        except AskarError as err:
            raise IndyHolderError("Error retrieving credentials") from err
        except CredxError as err:
            raise IndyHolderError("Error loading stored credential") from err

        return result

    async def get_credentials_for_presentation_request_by_referent(
        self,
        presentation_request: dict,
        referents: Sequence[str],
        start: int,
        count: int,
        extra_query: dict = {},
    ):
        """
        Get credentials stored in the wallet.

        Args:
            presentation_request: Valid presentation request from issuer
            referents: Presentation request referents to use to search for creds
            start: Starting index
            count: Maximum number of records to return
            extra_query: wql query dict

        """

        if not referents:
            referents = (
                *presentation_request["requested_attributes"],
                *presentation_request["requested_predicates"],
            )

        creds = {}

        for reft in referents:
            names = set()
            if reft in presentation_request["requested_attributes"]:
                attr = presentation_request["requested_attributes"][reft]
                if "name" in attr:
                    names.add(_normalize_attr_name(attr["name"]))
                elif "names" in attr:
                    names.update(_normalize_attr_name(name) for name in attr["names"])
                # for name in names:
                #     tag_filter[f"attr::{_normalize_attr_name(name)}::marker"] = "1"
                restr = attr.get("restrictions")
            elif reft in presentation_request["requested_predicates"]:
                pred = presentation_request["requested_predicates"][reft]
                if "name" in pred:
                    names.add(_normalize_attr_name(pred["name"]))
                # tag_filter[f"attr::{_normalize_attr_name(name)}::marker"] = "1"
                restr = pred.get("restrictions")
            else:
                raise IndyHolderError(f"Unknown presentation request referent: {reft}")

            tag_filter = {"$exist": list(f"attr::{name}::value" for name in names)}
            if restr:
                # FIXME check if restr is a list or dict? validate WQL format
                tag_filter = {"$and": [tag_filter] + restr}
            if extra_query:
                tag_filter = {"$and": [tag_filter, extra_query]}

            rows = self._profile.store.scan(
                CATEGORY_CREDENTIAL,
                tag_filter,
                start,
                count,
                self._profile.settings.get("wallet.askar_profile"),
            )
            async for row in rows:
                if row.name in creds:
                    creds[row.name]["presentation_referents"].add(reft)
                else:
                    cred_info = _make_cred_info(
                        row.name, Credential.load(row.raw_value)
                    )
                    creds[row.name] = {
                        "cred_info": cred_info,
                        "interval": presentation_request.get("non_revoked"),
                        "presentation_referents": {reft},
                    }

        for cred in creds.values():
            cred["presentation_referents"] = list(cred["presentation_referents"])

        return list(creds.values())

    async def get_credential(self, credential_id: str) -> str:
        """
        Get a credential stored in the wallet.

        Args:
            credential_id: Credential id to retrieve

        """
        cred = await self._get_credential(credential_id)
        return json.dumps(_make_cred_info(credential_id, cred))

    async def _get_credential(self, credential_id: str) -> Credential:
        """Get an unencoded Credential instance from the store."""
        try:
            async with self._profile.session() as session:
                cred = await session.handle.fetch(CATEGORY_CREDENTIAL, credential_id)
        except AskarError as err:
            raise IndyHolderError("Error retrieving credential") from err

        if not cred:
            raise WalletNotFoundError(
                f"Credential {credential_id} not found in wallet {self.profile.name}"
            )

        try:
            return Credential.load(cred.raw_value)
        except CredxError as err:
            raise IndyHolderError("Error loading requested credential") from err

    async def credential_revoked(
        self, ledger: BaseLedger, credential_id: str, fro: int = None, to: int = None
    ) -> bool:
        """
        Check ledger for revocation status of credential by cred id.

        Args:
            credential_id: Credential id to check

        """
        cred = await self._get_credential(credential_id)
        rev_reg_id = cred.rev_reg_id

        if rev_reg_id:
            cred_rev_id = cred.rev_reg_index
            (rev_reg_delta, _) = await ledger.get_revoc_reg_delta(
                rev_reg_id,
                fro,
                to,
            )
            return cred_rev_id in rev_reg_delta["value"].get("revoked", [])
        else:
            return False

    async def delete_credential(self, credential_id: str):
        """
        Remove a credential stored in the wallet.

        Args:
            credential_id: Credential id to remove

        """
        try:
            async with self._profile.session() as session:
                await session.handle.remove(CATEGORY_CREDENTIAL, credential_id)
                await session.handle.remove(
                    IndyHolder.RECORD_TYPE_MIME_TYPES, credential_id
                )
        except AskarError as err:
            if err.code == AskarErrorCode.NOT_FOUND:
                pass
            else:
                raise IndyHolderError("Error deleting credential") from err

    async def get_mime_type(
        self, credential_id: str, attr: str = None
    ) -> Union[dict, str]:
        """
        Get MIME type per attribute (or for all attributes).

        Args:
            credential_id: credential id
            attr: attribute of interest or omit for all

        Returns: Attribute MIME type or dict mapping attribute names to MIME types
            attr_meta_json = all_meta.tags.get(attr)

        """
        try:
            async with self._profile.session() as session:
                mime_types_record = await session.handle.fetch(
                    IndyHolder.RECORD_TYPE_MIME_TYPES,
                    credential_id,
                )
        except AskarError as err:
            raise IndyHolderError("Error retrieving credential mime types") from err
        values = mime_types_record and mime_types_record.value_json
        if values:
            return values.get(attr) if attr else values

    async def create_presentation(
        self,
        presentation_request: dict,
        requested_credentials: dict,
        schemas: dict,
        credential_definitions: dict,
        rev_states: dict = None,
    ) -> str:
        """
        Get credentials stored in the wallet.

        Args:
            presentation_request: Valid indy format presentation request
            requested_credentials: Indy format requested credentials
            schemas: Indy formatted schemas JSON
            credential_definitions: Indy formatted credential definitions JSON
            rev_states: Indy format revocation states JSON

        """

        creds: Dict[str, Credential] = {}

        def get_rev_state(cred_id: str, detail: dict):
            cred = creds[cred_id]
            rev_reg_id = cred.rev_reg_id
            timestamp = detail.get("timestamp") if rev_reg_id else None
            rev_state = None
            if timestamp:
                if not rev_states or rev_reg_id not in rev_states:
                    raise IndyHolderError(
                        f"No revocation states provided for credential '{cred_id}' "
                        f"with rev_reg_id '{rev_reg_id}'"
                    )
                rev_state = rev_states[rev_reg_id].get(timestamp)
                if not rev_state:
                    raise IndyHolderError(
                        f"No revocation states provided for credential '{cred_id}' "
                        f"with rev_reg_id '{rev_reg_id}' at timestamp {timestamp}"
                    )
            return timestamp, rev_state

        self_attest = requested_credentials.get("self_attested_attributes") or {}
        present_creds = PresentCredentials()
        req_attrs = requested_credentials.get("requested_attributes") or {}
        for reft, detail in req_attrs.items():
            cred_id = detail["cred_id"]
            if cred_id not in creds:
                # NOTE: could be optimized if multiple creds are requested
                creds[cred_id] = await self._get_credential(cred_id)
            timestamp, rev_state = get_rev_state(cred_id, detail)
            present_creds.add_attributes(
                creds[cred_id],
                reft,
                reveal=detail["revealed"],
                timestamp=timestamp,
                rev_state=rev_state,
            )
        req_preds = requested_credentials.get("requested_predicates") or {}
        for reft, detail in req_preds.items():
            cred_id = detail["cred_id"]
            if cred_id not in creds:
                # NOTE: could be optimized if multiple creds are requested
                creds[cred_id] = await self._get_credential(cred_id)
            timestamp, rev_state = get_rev_state(cred_id, detail)
            present_creds.add_predicates(
                creds[cred_id],
                reft,
                timestamp=timestamp,
                rev_state=rev_state,
            )

        try:
            secret = await self.get_master_secret()
            presentation = await asyncio.get_event_loop().run_in_executor(
                None,
                Presentation.create,
                presentation_request,
                present_creds,
                self_attest,
                secret,
                schemas.values(),
                credential_definitions.values(),
            )
        except CredxError as err:
            raise IndyHolderError("Error creating presentation") from err

        return presentation.to_json()

    async def create_revocation_state(
        self,
        cred_rev_id: str,
        rev_reg_def: dict,
        rev_reg_delta: dict,
        timestamp: int,
        tails_file_path: str,
    ) -> str:
        """
        Create current revocation state for a received credential.

        Args:
            cred_rev_id: credential revocation id in revocation registry
            rev_reg_def: revocation registry definition
            rev_reg_delta: revocation delta
            timestamp: delta timestamp

        Returns:
            the revocation state

        """

        try:
            rev_state = await asyncio.get_event_loop().run_in_executor(
                None,
                CredentialRevocationState.create,
                rev_reg_def,
                rev_reg_delta,
                int(cred_rev_id),
                timestamp,
                tails_file_path,
            )
        except CredxError as err:
            raise IndyHolderError("Error creating revocation state") from err
        return rev_state.to_json()
