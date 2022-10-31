import json

from asynctest import TestCase as AsyncTestCase, mock as async_mock
from marshmallow import EXCLUDE, fields

from ....cache.base import BaseCache
from ....core.event_bus import EventBus, MockEventBus, Event
from ....core.in_memory import InMemoryProfile
from ....storage.base import (
    BaseStorage,
    StorageDuplicateError,
    StorageError,
    StorageRecord,
)
from ....messaging.models.base import BaseModelError

from ...util import time_now

from ..base_record import BaseRecord, BaseRecordSchema, LOGGER


class BaseRecordImpl(BaseRecord):
    class Meta:
        schema_class = "BaseRecordImplSchema"

    RECORD_TYPE = "record"


class BaseRecordImplSchema(BaseRecordSchema):
    class Meta:
        model_class = BaseRecordImpl
        unknown = EXCLUDE


class ARecordImpl(BaseRecord):
    class Meta:
        schema_class = "ARecordImplSchema"

    RECORD_TYPE = "a-record"
    RECORD_ID_NAME = "ident"
    TAG_NAMES = {"code"}

    def __init__(self, *, ident=None, a, b, code=None, **kwargs):
        super().__init__(ident, **kwargs)
        self.a = a
        self.b = b
        self.code = code

    @property
    def record_value(self) -> dict:
        return {"a": self.a, "b": self.b}


class ARecordImplSchema(BaseRecordSchema):
    class Meta:
        model_class = BaseRecordImpl
        unknown = EXCLUDE

    ident = fields.Str(attribute="_id")
    a = fields.Str()
    b = fields.Str()
    code = fields.Str()


class UnencTestImpl(BaseRecord):
    TAG_NAMES = {"~a", "~b", "c"}


class TestBaseRecord(AsyncTestCase):
    def test_init_undef(self):
        with self.assertRaises(TypeError):
            BaseRecord()

    def test_from_storage_values(self):
        record_id = "record_id"
        stored = {"created_at": time_now(), "updated_at": time_now()}
        inst = BaseRecordImpl.from_storage(record_id, stored)
        assert isinstance(inst, BaseRecordImpl)
        assert inst._id == record_id
        assert inst.value == stored

        stored[BaseRecordImpl.RECORD_ID_NAME] = inst._id
        with self.assertRaises(ValueError):
            BaseRecordImpl.from_storage(record_id, stored)

    async def test_post_save_new(self):
        session = InMemoryProfile.test_session()
        mock_storage = async_mock.MagicMock()
        mock_storage.add_record = async_mock.CoroutineMock()
        session.context.injector.bind_instance(BaseStorage, mock_storage)
        record = BaseRecordImpl()
        with async_mock.patch.object(
            record, "post_save", async_mock.CoroutineMock()
        ) as post_save:
            await record.save(session, reason="reason", event=True)
            post_save.assert_called_once_with(session, True, None, True)
        mock_storage.add_record.assert_called_once()

    async def test_post_save_exist(self):
        session = InMemoryProfile.test_session()
        mock_storage = async_mock.MagicMock()
        mock_storage.update_record = async_mock.CoroutineMock()
        session.context.injector.bind_instance(BaseStorage, mock_storage)
        record = BaseRecordImpl()
        last_state = "last_state"
        record._last_state = last_state
        record._id = "id"
        with async_mock.patch.object(
            record, "post_save", async_mock.CoroutineMock()
        ) as post_save:
            await record.save(session, reason="reason", event=False)
            post_save.assert_called_once_with(session, False, last_state, False)
        mock_storage.update_record.assert_called_once()

    async def test_cache(self):
        assert not await BaseRecordImpl.get_cached_key(None, None)
        await BaseRecordImpl.set_cached_key(None, None, None)
        await BaseRecordImpl.clear_cached_key(None, None)
        session = InMemoryProfile.test_session()
        mock_cache = async_mock.MagicMock(BaseCache, autospec=True)
        session.context.injector.bind_instance(BaseCache, mock_cache)
        record = BaseRecordImpl()
        cache_key = "cache_key"
        cache_result = await BaseRecordImpl.get_cached_key(session, cache_key)
        mock_cache.get.assert_awaited_once_with(cache_key)
        assert cache_result is mock_cache.get.return_value

        await record.set_cached_key(session, cache_key, record)
        mock_cache.set.assert_awaited_once_with(
            cache_key, record, record.DEFAULT_CACHE_TTL
        )

        await record.clear_cached_key(session, cache_key)
        mock_cache.clear.assert_awaited_once_with(cache_key)

    async def test_retrieve_by_tag_filter_multi_x_delete(self):
        session = InMemoryProfile.test_session()
        records = []
        for i in range(3):
            records.append(ARecordImpl(a="1", b=str(i), code="one"))
            await records[i].save(session)
        with self.assertRaises(StorageDuplicateError):
            await ARecordImpl.retrieve_by_tag_filter(
                session, {"code": "one"}, {"a": "1"}
            )
        await records[0].delete_record(session)

    async def test_save_x(self):
        session = InMemoryProfile.test_session()
        rec = ARecordImpl(a="1", b="0", code="one")
        with async_mock.patch.object(
            session, "inject", async_mock.MagicMock()
        ) as mock_inject:
            mock_inject.return_value = async_mock.MagicMock(
                add_record=async_mock.CoroutineMock(side_effect=ZeroDivisionError())
            )
            with self.assertRaises(ZeroDivisionError):
                await rec.save(session)

    async def test_neq(self):
        a_rec = ARecordImpl(a="1", b="0", code="one")
        b_rec = BaseRecordImpl()
        assert a_rec != b_rec

    async def test_query(self):
        session = InMemoryProfile.test_session()
        mock_storage = async_mock.MagicMock(BaseStorage, autospec=True)
        session.context.injector.bind_instance(BaseStorage, mock_storage)
        record_id = "record_id"
        record_value = {"created_at": time_now(), "updated_at": time_now()}
        tag_filter = {"tag": "filter"}
        stored = StorageRecord(
            BaseRecordImpl.RECORD_TYPE, json.dumps(record_value), {}, record_id
        )

        mock_storage.find_all_records.return_value = [stored]
        result = await BaseRecordImpl.query(session, tag_filter)
        mock_storage.find_all_records.assert_awaited_once_with(
            BaseRecordImpl.RECORD_TYPE, tag_filter, options={"retrieveTags": False}
        )
        assert result and isinstance(result[0], BaseRecordImpl)
        assert result[0]._id == record_id
        assert result[0].value == record_value

    async def test_query_x(self):
        session = InMemoryProfile.test_session()
        mock_storage = async_mock.MagicMock(BaseStorage, autospec=True)
        session.context.injector.bind_instance(BaseStorage, mock_storage)
        record_id = "record_id"
        record_value = {"created_at": time_now(), "updated_at": time_now()}
        tag_filter = {"tag": "filter"}
        stored = StorageRecord(
            BaseRecordImpl.RECORD_TYPE, json.dumps(record_value), {}, record_id
        )

        mock_storage.find_all_records.return_value = [stored]
        with async_mock.patch.object(
            BaseRecordImpl,
            "from_storage",
            async_mock.MagicMock(side_effect=BaseModelError),
        ):
            with self.assertRaises(BaseModelError):
                await BaseRecordImpl.query(session, tag_filter)

    async def test_query_post_filter(self):
        session = InMemoryProfile.test_session()
        mock_storage = async_mock.MagicMock(BaseStorage, autospec=True)
        session.context.injector.bind_instance(BaseStorage, mock_storage)
        record_id = "record_id"
        a_record = ARecordImpl(ident=record_id, a="one", b="two", code="red")
        record_value = a_record.record_value
        record_value.update({"created_at": time_now(), "updated_at": time_now()})
        tag_filter = {"code": "red"}
        post_filter_pos_alt = {"a": ["one", "a suffusion of yellow"]}
        post_filter_neg_alt = {"a": ["three", "no, five"]}
        stored = StorageRecord(
            ARecordImpl.RECORD_TYPE,
            json.dumps(record_value),
            {"code": "red"},
            record_id,
        )
        mock_storage.find_all_records.return_value = [stored]

        # positive match
        result = await ARecordImpl.query(
            session, tag_filter, post_filter_positive={"a": "one"}
        )
        mock_storage.find_all_records.assert_awaited_once_with(
            ARecordImpl.RECORD_TYPE,
            tag_filter,
            options={"retrieveTags": False},
        )
        assert result and isinstance(result[0], ARecordImpl)
        assert result[0]._id == record_id
        assert result[0].value == record_value
        assert result[0].a == "one"

        # positive match by list of alternatives to hit
        result = await ARecordImpl.query(
            session, tag_filter, post_filter_positive=post_filter_pos_alt, alt=True
        )
        assert result and isinstance(result[0], ARecordImpl)
        assert result[0]._id == record_id
        assert result[0].value == record_value
        assert result[0].a == "one"

        # negative match by list of alternatives to miss (result complies)
        result = await ARecordImpl.query(
            session, tag_filter, post_filter_negative=post_filter_neg_alt, alt=True
        )
        assert result and isinstance(result[0], ARecordImpl)
        assert result[0]._id == record_id
        assert result[0].value == record_value
        assert result[0].a == "one"

        # negative match by list of alternatives to miss, with one hit spoiling result
        post_filter_neg_alt = {"a": ["one", "three", "no, five"]}
        result = await ARecordImpl.query(
            session, tag_filter, post_filter_negative=post_filter_neg_alt, alt=True
        )
        assert not result

    @async_mock.patch("builtins.print")
    def test_log_state(self, mock_print):
        test_param = "test.log"
        with async_mock.patch.object(
            BaseRecordImpl, "LOG_STATE_FLAG", test_param
        ) as cls:
            record = BaseRecordImpl()
            record.log_state(
                msg="state",
                params={"a": "1", "b": "2"},
                settings=async_mock.MagicMock(get={test_param: 1}.get),
            )
        mock_print.assert_called_once()

    @async_mock.patch("builtins.print")
    def test_skip_log(self, mock_print):
        record = BaseRecordImpl()
        record.log_state("state", settings=None)
        mock_print.assert_not_called()

    async def test_emit_event(self):
        session = InMemoryProfile.test_session()
        mock_event_bus = MockEventBus()
        session.profile.context.injector.bind_instance(EventBus, mock_event_bus)
        record = BaseRecordImpl()
        payload = {"test": "payload"}

        # Records must have topic to emit events
        record.RECORD_TOPIC = None
        await record.emit_event(session, payload)
        assert mock_event_bus.events == []

        record.RECORD_TOPIC = "topic"

        # Stateless record with no payload emits event with serialized record
        await record.emit_event(session)
        assert mock_event_bus.events == [
            (session.profile, Event("acapy::record::topic", {}))
        ]
        mock_event_bus.events.clear()

        # Stateless record with payload emits event
        await record.emit_event(session, payload)
        assert mock_event_bus.events == [
            (session.profile, Event("acapy::record::topic", payload))
        ]
        mock_event_bus.events.clear()

        # Statefull record with payload emits event
        record.state = "test_state"
        await record.emit_event(session, payload)
        assert mock_event_bus.events == [
            (session.profile, Event("acapy::record::topic::test_state", payload))
        ]

    async def test_tag_prefix(self):
        tags = {"~x": "a", "y": "b"}
        assert UnencTestImpl.strip_tag_prefix(tags) == {"x": "a", "y": "b"}

        tags = {"a": "x", "b": "y", "c": "z"}
        assert UnencTestImpl.prefix_tag_filter(tags) == {"~a": "x", "~b": "y", "c": "z"}

        tags = {"$not": {"a": "x", "b": "y", "c": "z"}}
        expect = {"$not": {"~a": "x", "~b": "y", "c": "z"}}
        actual = UnencTestImpl.prefix_tag_filter(tags)
        assert {**expect} == {**actual}

        tags = {"$or": [{"a": "x"}, {"c": "z"}]}
        assert UnencTestImpl.prefix_tag_filter(tags) == {
            "$or": [{"~a": "x"}, {"c": "z"}]
        }
