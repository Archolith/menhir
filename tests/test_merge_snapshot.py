from __future__ import annotations

import pytest
from neo4j.spatial import CartesianPoint, WGS84Point
from neo4j.time import Date, DateTime, Duration, Time

from menhir.domain import merge_snapshot as ms

pytestmark = pytest.mark.unit


class TestValueCodec:
    """encode_value / decode_value round-trips and edge cases."""

    @pytest.mark.parametrize(
        "v",
        [
            "text",
            7,
            1.5,
            True,
            False,
            None,
        ],
    )
    def test_primitives_round_trip(self, v: object) -> None:
        assert ms.decode_value(ms.encode_value(v)) == v
        if v is not None:
            assert type(ms.decode_value(ms.encode_value(v))) is type(v)

    def test_bool_is_not_encoded_as_int(self) -> None:
        assert ms.encode_value(True) is True
        assert ms.encode_value(False) is False
        decoded = ms.decode_value(ms.encode_value(True))
        assert isinstance(decoded, bool)
        assert not isinstance(decoded, int) or isinstance(decoded, bool)

    def test_temporal_types_round_trip_exactly(self) -> None:
        values = [
            DateTime(2026, 7, 13, 10, 30, 15, 123456789),
            Date(2026, 7, 13),
            Time(10, 30, 15, 5),
            Duration(months=2, days=3, seconds=4, nanoseconds=5),
        ]
        for original in values:
            decoded = ms.decode_value(ms.encode_value(original))
            assert decoded == original
            assert type(decoded) is type(original)

    def test_duration_components_preserved(self) -> None:
        d = Duration(months=14, days=3, seconds=4, nanoseconds=5)
        decoded = ms.decode_value(ms.encode_value(d))
        assert decoded.months == 14
        assert decoded.days == 3
        assert decoded.seconds == 4
        assert decoded.nanoseconds == 5

    def test_points_round_trip_with_srid(self) -> None:
        points = [
            CartesianPoint((1.0, 2.0)),
            CartesianPoint((1.0, 2.0, 3.0)),
            WGS84Point((12.5, 55.7)),
        ]
        for p in points:
            decoded = ms.decode_value(ms.encode_value(p))
            assert decoded == p
            assert type(decoded) is type(p)
            assert decoded.srid == p.srid

    def test_nested_list_and_map_round_trip(self) -> None:
        lst = [1, "a", Date(2020, 1, 1)]
        assert ms.decode_value(ms.encode_value(lst)) == lst

        dct = {"k": Date(2021, 2, 2), "n": [1, 2]}
        assert ms.decode_value(ms.encode_value(dct)) == dct

    def test_map_containing_type_key_is_not_misread(self) -> None:
        original = {"$type": "not-a-tag", "x": 1}
        decoded = ms.decode_value(ms.encode_value(original))
        assert decoded == original
        assert type(decoded) is dict

    def test_bytes_round_trip(self) -> None:
        data = b"\x00\x01\xff"
        assert ms.decode_value(ms.encode_value(data)) == data

    def test_unencodable_value_raises(self) -> None:
        with pytest.raises(ms.SnapshotSchemaError):
            ms.encode_value(object())


class TestSnapshotRoundTrip:
    """build_snapshot / dumps / loads / load_snapshot integration."""

    def test_build_and_load_snapshot_round_trip(self) -> None:
        snap = ms.build_snapshot(
            survivor={"uuid": "s"},
            absorbed={"uuid": "a"},
            similarity=0.97,
        )
        text = ms.dumps(snap)
        parsed = ms.loads(text)
        body = ms.load_snapshot(parsed)
        assert body["survivor"]["uuid"] == "s"
        assert body["similarity"] == 0.97

    def test_checksum_mismatch_fails_closed(self) -> None:
        snap = ms.build_snapshot(
            survivor={"uuid": "s"},
            absorbed={"uuid": "a"},
            similarity=0.97,
        )
        snap["body"]["similarity"] = 0.1
        with pytest.raises(ms.SnapshotSchemaError):
            ms.load_snapshot(snap)

    def test_unsupported_schema_version_fails_closed(self) -> None:
        snap = ms.build_snapshot(
            survivor={"uuid": "s"},
            absorbed={"uuid": "a"},
            similarity=0.97,
        )
        snap["schema_version"] = 99
        with pytest.raises(ms.SnapshotSchemaError):
            ms.load_snapshot(snap)


class TestNodeEncoding:
    """encode_node / decode_node."""

    def test_encode_node_preserves_parallel_relationships(self) -> None:
        relationships = [
            {
                "type": "RELATES_TO",
                "direction": "out",
                "properties": {"w": 0.9},
                "peer_uuid": "p",
                "peer_labels": ["Entity"],
                "peer_identity": "p",
            },
            {
                "type": "RELATES_TO",
                "direction": "out",
                "properties": {"w": 0.3},
                "peer_uuid": "p",
                "peer_labels": ["Entity"],
                "peer_identity": "p",
            },
        ]
        encoded = ms.encode_node(
            uuid="a",
            labels=["Entity"],
            properties={},
            relationships=relationships,
        )
        assert len(encoded["relationships"]) == 2
        decoded = ms.decode_node(encoded)
        assert decoded["relationships"][0]["properties"] == {"w": 0.9}
        assert decoded["relationships"][1]["properties"] == {"w": 0.3}


class TestRestorableProperties:
    """restorable_properties filtering."""

    def test_restorable_properties_excludes_system_managed(self) -> None:
        assert ms.restorable_properties({"name": "x", "last_accessed": 1}) == {"name": "x"}
        assert "last_accessed" in ms.SYSTEM_MANAGED_PROPERTIES


class TestSizeLimit:
    """Invariant 15: an oversized snapshot abstains; it is never truncated to fit."""

    def test_dumps_raises_when_over_the_limit(self, monkeypatch) -> None:
        monkeypatch.setattr(ms, "MAX_SNAPSHOT_BYTES", 100)
        snap = ms.build_snapshot(
            survivor={"uuid": "s", "blob": "x" * 500}, absorbed={"uuid": "a"}, similarity=0.9
        )
        with pytest.raises(ms.SnapshotTooLargeError) as exc:
            ms.dumps(snap)
        assert exc.value.size_bytes > 100
        assert exc.value.limit == 100

    def test_too_large_is_a_schema_error_subclass(self) -> None:
        # Existing fail-closed handlers that catch SnapshotSchemaError must still catch this.
        assert issubclass(ms.SnapshotTooLargeError, ms.SnapshotSchemaError)

    def test_dumps_succeeds_under_the_limit(self) -> None:
        snap = ms.build_snapshot(survivor={"uuid": "s"}, absorbed={"uuid": "a"}, similarity=0.9)
        assert ms.loads(ms.dumps(snap))["schema_version"] == ms.SCHEMA_VERSION

    def test_snapshot_size_bytes_reports_without_raising(self, monkeypatch) -> None:
        # Observe-mode reporting must be able to measure a snapshot that would be rejected.
        monkeypatch.setattr(ms, "MAX_SNAPSHOT_BYTES", 10)
        snap = ms.build_snapshot(survivor={"uuid": "s"}, absorbed={"uuid": "a"}, similarity=0.9)
        assert ms.snapshot_size_bytes(snap) > 10
