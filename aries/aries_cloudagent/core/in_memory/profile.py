"""Manage in-memory profile interaction."""

from collections import OrderedDict
from typing import Any, Mapping, Type
from weakref import ref

from ...config.injection_context import InjectionContext
from ...config.provider import ClassProvider
from ...storage.base import BaseStorage
from ...storage.vc_holder.base import VCHolder
from ...utils.classloader import DeferLoad
from ...wallet.base import BaseWallet

from ..profile import Profile, ProfileManager, ProfileSession

STORAGE_CLASS = DeferLoad("aries_cloudagent.storage.in_memory.InMemoryStorage")
WALLET_CLASS = DeferLoad("aries_cloudagent.wallet.in_memory.InMemoryWallet")


class InMemoryProfile(Profile):
    """
    Provide access to in-memory profile management.

    Used primarily for testing.
    """

    BACKEND_NAME = "in_memory"
    TEST_PROFILE_NAME = "test-profile"

    def __init__(self, *, context: InjectionContext = None, name: str = None):
        """Create a new InMemoryProfile instance."""
        global STORAGE_CLASS, WALLET_CLASS
        super().__init__(context=context, name=name, created=True)
        self.keys = {}
        self.local_dids = {}
        self.pair_dids = {}
        self.records = OrderedDict()
        self.bind_providers()

    def bind_providers(self):
        """Initialize the profile-level instance providers."""
        injector = self._context.injector

        injector.bind_provider(
            VCHolder,
            ClassProvider(
                "aries_cloudagent.storage.vc_holder.in_memory.InMemoryVCHolder",
                ref(self),
            ),
        )

    def session(self, context: InjectionContext = None) -> "ProfileSession":
        """Start a new interactive session with no transaction support requested."""
        return InMemoryProfileSession(self, context=context)

    def transaction(self, context: InjectionContext = None) -> "ProfileSession":
        """
        Start a new interactive session with commit and rollback support.

        If the current backend does not support transactions, then commit
        and rollback operations of the session will not have any effect.
        """
        return InMemoryProfileSession(self, context=context)

    @classmethod
    def test_profile(
        cls, settings: Mapping[str, Any] = None, bind: Mapping[Type, Any] = None
    ) -> "InMemoryProfile":
        """Used in tests to create a standard InMemoryProfile."""
        profile = InMemoryProfile(
            context=InjectionContext(enforce_typing=False, settings=settings),
            name=InMemoryProfile.TEST_PROFILE_NAME,
        )
        if bind:
            for k, v in bind.items():
                if v:
                    profile.context.injector.bind_instance(k, v)
                else:
                    profile.context.injector.clear_binding(k)
        return profile

    @classmethod
    def test_session(
        cls, settings: Mapping[str, Any] = None, bind: Mapping[Type, Any] = None
    ) -> "InMemoryProfileSession":
        """Used in tests to quickly create InMemoryProfileSession."""
        session = InMemoryProfileSession(cls.test_profile(), settings=settings)
        session._active = True
        session._init_context()
        if bind:
            for k, v in bind.items():
                if v:
                    session.context.injector.bind_instance(k, v)
                else:
                    session.context.injector.clear_binding(k)
        return session


class InMemoryProfileSession(ProfileSession):
    """An active connection to the profile management backend."""

    def __init__(
        self,
        profile: Profile,
        *,
        context: InjectionContext = None,
        settings: Mapping[str, Any] = None
    ):
        """Create a new InMemoryProfileSession instance."""
        super().__init__(profile=profile, context=context, settings=settings)

    async def _setup(self):
        """Create the session or transaction connection, if needed."""
        await super()._setup()
        self._init_context()

    def _init_context(self):
        """Initialize the session context."""
        self._context.injector.bind_instance(BaseStorage, STORAGE_CLASS(self.profile))
        self._context.injector.bind_instance(BaseWallet, WALLET_CLASS(self.profile))

    @property
    def storage(self) -> BaseStorage:
        """Get the `BaseStorage` implementation (helper specific to in-memory profile)."""
        return self._context.inject(BaseStorage)

    @property
    def wallet(self) -> BaseWallet:
        """Get the `BaseWallet` implementation (helper specific to in-memory profile)."""
        return self._context.inject(BaseWallet)


class InMemoryProfileManager(ProfileManager):
    """Manager for producing in-memory wallet/storage implementation."""

    async def provision(
        self, context: InjectionContext, config: Mapping[str, Any] = None
    ) -> Profile:
        """Provision a new instance of a profile."""
        return InMemoryProfile(context=context, name=(config or {}).get("name"))

    async def open(
        self, context: InjectionContext, config: Mapping[str, Any] = None
    ) -> Profile:
        """Open an instance of an existing profile."""
        return await self.provision(context, config)
