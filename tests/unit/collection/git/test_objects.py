from __future__ import annotations

from io import BytesIO

import pytest
from workfold.collection.git.objects import (
    BatchObject,
    GitObjectParseError,
    InvalidBatchCommit,
    ParsedCommit,
    RevListScanSpec,
    UnexpectedBatchObject,
    inspect_rev_list_scan,
    parse_cat_file_batch,
    parse_commit_object,
    read_cat_file_batch_commit,
    read_cat_file_batch_record,
)

COMMIT_ID = "a" * 40
TREE_ID = "b" * 40
PARENT_ID = "c" * 40


def make_commit_data(
    *,
    author: bytes = b"Ada Person <ada@example.test> 1704067200 +0530",
    committer: bytes = b"Commit Bot <bot@example.test> 1704070800 -0230",
    extra_headers: bytes = b"",
    message: bytes = b"A precise subject\n\nBody\n",
) -> bytes:
    return (
        b"tree "
        + TREE_ID.encode("ascii")
        + b"\nparent "
        + PARENT_ID.encode("ascii")
        + b"\nauthor "
        + author
        + b"\ncommitter "
        + committer
        + b"\n"
        + extra_headers
        + b"\n"
        + message
    )


def test_parse_commit_preserves_exact_signatures_and_subject() -> None:
    parsed = parse_commit_object(
        COMMIT_ID,
        make_commit_data(
            author="Ada Üser <ada@example.test> 1704067200 +0530".encode(),
            extra_headers=b"encoding UTF-8\ngpgsig -----BEGIN\n continuation\n",
        ),
    )

    assert parsed.object_id == COMMIT_ID
    assert parsed.tree_id == TREE_ID
    assert parsed.parent_ids == (PARENT_ID,)
    assert parsed.author.identity.name == "Ada Üser"
    assert parsed.author.identity.email == "ada@example.test"
    assert parsed.author.epoch_seconds == 1_704_067_200
    assert parsed.author.epoch_nanoseconds == 1_704_067_200_000_000_000
    assert parsed.author.offset_seconds == 5 * 3_600 + 30 * 60
    assert parsed.author.raw_timestamp == "1704067200 +0530"
    assert parsed.author.raw.endswith(b"1704067200 +0530")
    assert parsed.committer.offset_seconds == -(2 * 3_600 + 30 * 60)
    assert parsed.subject == "A precise subject"
    assert parsed.raw_subject == b"A precise subject"
    assert parsed.declared_encoding == "UTF-8"


def test_parse_commit_honors_declared_message_encoding_without_losing_raw_bytes() -> None:
    raw_subject = "Grüße".encode("iso-8859-1")

    parsed = parse_commit_object(
        COMMIT_ID,
        make_commit_data(
            extra_headers=b"encoding ISO-8859-1\n",
            message=raw_subject + b"\n",
        ),
    )

    assert parsed.subject == "Grüße"
    assert parsed.raw_subject == raw_subject
    assert parsed.declared_encoding == "ISO-8859-1"


def test_rev_list_scan_parses_only_requested_ascii_timestamp_roles() -> None:
    author_only = RevListScanSpec(("author",))
    scanned = inspect_rev_list_scan(
        b"a" * 40 + b"\0" + b"1704067200\0\n",
        author_only,
    )

    assert scanned.object_id == COMMIT_ID
    assert scanned.instant_utc_ns("author") == 1_704_067_200_000_000_000
    with pytest.raises(ValueError, match="committer"):
        scanned.instant_utc_ns("committer")

    both = RevListScanSpec(("author", "committer"))
    scanned_both = inspect_rev_list_scan(
        b"a" * 40 + b"\0" + b"1704067200\0" + b"1704070800\0\n",
        both,
    )
    assert scanned_both.instant_utc_ns("author") == 1_704_067_200_000_000_000
    assert scanned_both.instant_utc_ns("committer") == 1_704_070_800_000_000_000


def test_rev_list_scan_spec_builds_minimal_machine_safe_formats() -> None:
    assert RevListScanSpec(("author",)).pretty_format == "%H%x00%at%x00"
    assert RevListScanSpec(("committer",)).pretty_format == "%H%x00%ct%x00"
    assert RevListScanSpec(("author", "committer")).pretty_format == "%H%x00%at%x00%ct%x00"

    with pytest.raises(ValueError, match="at least one"):
        RevListScanSpec(())
    with pytest.raises(ValueError, match="unique"):
        RevListScanSpec(("author", "author"))


@pytest.mark.parametrize(
    "record",
    [
        b"",
        b"a" * 40 + b"\0\n",
        b"not-an-object\0" + b"1704067200\0\n",
        b"a" * 40 + b"\0not-an-epoch\0\n",
        b"a" * 40 + b"\0" + b"253402214400\0\n",
    ],
)
def test_rev_list_scan_rejects_malformed_records(record: bytes) -> None:
    with pytest.raises(GitObjectParseError):
        inspect_rev_list_scan(record, RevListScanSpec(("author",)))


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"tree " + TREE_ID.encode() + b"\n\nmessage", "invalid_commit_header"),
        (make_commit_data(author=b"incomplete"), "invalid_git_signature"),
        (make_commit_data(author=b"No email 1704067200 +0000"), "invalid_git_identity"),
        (make_commit_data(author=b"Ada <a@b> nope +0000"), "invalid_git_timestamp"),
        (make_commit_data(author=b"Ada <a@b> " + b"9" * 5_000 + b" +0000"), "invalid_git_timestamp"),
        (make_commit_data(author=b"Ada <a@b> 253402214400 +0000"), "invalid_git_timestamp"),
        (make_commit_data(author=b"Ada <a@b> 1 0000"), "invalid_git_timestamp"),
        (make_commit_data(author=b"Ada <a@b> 1 +2460"), "invalid_git_timestamp"),
        (make_commit_data().replace(b"tree ", b"tree nope", 1), "invalid_commit_header"),
        (b" orphan\ntree " + TREE_ID.encode() + b"\n\nmessage", "invalid_commit_header"),
        (
            make_commit_data()
            .replace(b"tree ", b"tree ", 1)
            .replace(b"\nparent", b"\ntree " + TREE_ID.encode() + b"\nparent", 1),
            "invalid_commit_header",
        ),
        (make_commit_data().replace(b"parent ", b"malformed\nparent ", 1), "invalid_commit_header"),
        (make_commit_data().split(b"\n\n", 1)[0], "invalid_commit_object"),
    ],
)
def test_parse_commit_rejects_malformed_objects(data: bytes, code: str) -> None:
    with pytest.raises(GitObjectParseError) as error:
        parse_commit_object(COMMIT_ID, data)

    assert error.value.code == code
    assert error.value.object_id == COMMIT_ID


def test_parse_commit_rejects_invalid_object_id() -> None:
    with pytest.raises(GitObjectParseError) as error:
        parse_commit_object("not-an-object", make_commit_data())

    assert error.value.code == "invalid_object_id"


def test_parse_cat_file_batch_uses_byte_lengths_not_message_delimiters() -> None:
    first_data = make_commit_data(message=b"subject\nline with arbitrary\nnewlines\n")
    second_id = "d" * 40
    second_data = make_commit_data(message=b"other\n")
    payload = (
        f"{COMMIT_ID} commit {len(first_data)}\n".encode()
        + first_data
        + b"\n"
        + f"{second_id} commit {len(second_data)}\n".encode()
        + second_data
        + b"\n"
    )

    result = parse_cat_file_batch(payload, (COMMIT_ID, second_id))

    assert [item.data for item in result.objects] == [first_data, second_data]
    assert not result.unavailable


def test_parse_cat_file_batch_accounts_for_missing_objects() -> None:
    result = parse_cat_file_batch(f"{COMMIT_ID} missing\n".encode(), (COMMIT_ID,))

    assert not result.objects
    assert result.unavailable[0].requested_id == COMMIT_ID
    assert result.unavailable[0].reason == "missing"


def test_streaming_cat_file_record_preserves_ascii_request_metadata() -> None:
    data = make_commit_data()
    stream = BytesIO(f"{COMMIT_ID} commit {len(data)} ac\n".encode() + data + b"\n")

    result, metadata = read_cat_file_batch_record(stream, expect_metadata=True)

    assert isinstance(result, BatchObject)
    assert result.object_id == COMMIT_ID
    assert result.data == data
    assert metadata == b"ac"


def test_streaming_cat_file_unavailable_response_has_no_request_metadata() -> None:
    stream = BytesIO(f"{COMMIT_ID} missing\n".encode())

    result, metadata = read_cat_file_batch_record(stream, expect_metadata=True)

    assert not isinstance(result, BatchObject)
    assert result.requested_id == COMMIT_ID
    assert result.reason == "missing"
    assert metadata is None


def test_streaming_commit_reader_drains_large_messages_without_retaining_them() -> None:
    class TrackingStream(BytesIO):
        max_read_size = 0
        max_readline_size = 0

        def read(self, size: int | None = -1) -> bytes:
            self.max_read_size = max(self.max_read_size, -1 if size is None else size)
            return super().read(size)

        def readline(self, size: int | None = -1) -> bytes:
            self.max_readline_size = max(self.max_readline_size, -1 if size is None else size)
            return super().readline(size)

    data = make_commit_data(message=b"bounded subject\n" + b"x" * 4_000_000)
    stream = TrackingStream(f"{COMMIT_ID} commit {len(data)} ac\n".encode() + data + b"\n")

    result, metadata = read_cat_file_batch_commit(stream, expect_metadata=True)

    assert isinstance(result, ParsedCommit)
    assert result.subject == "bounded subject"
    assert result.raw_subject == b"bounded subject"
    assert not result.subject_truncated
    assert metadata == b"ac"
    assert stream.tell() == len(stream.getbuffer())
    assert stream.max_read_size <= 65_536
    assert stream.max_readline_size <= 1_048_578


def test_streaming_commit_reader_marks_an_oversized_subject_without_losing_timestamps() -> None:
    subject = b"s" * 1_100_000
    data = make_commit_data(message=subject + b"\nbody")
    stream = BytesIO(f"{COMMIT_ID} commit {len(data)} a\n".encode() + data + b"\n")

    result, metadata = read_cat_file_batch_commit(stream, expect_metadata=True)

    assert isinstance(result, ParsedCommit)
    assert result.author.epoch_seconds == 1_704_067_200
    assert result.subject_truncated
    assert len(result.raw_subject) == 1_048_576
    assert result.subject.endswith("…")
    assert metadata == b"a"


def test_streaming_commit_reader_accounts_for_one_bad_object_and_continues() -> None:
    malformed = b"tree only-no-boundary"
    valid_id = "d" * 40
    valid = make_commit_data(message=b"next\n")
    stream = BytesIO(
        f"{COMMIT_ID} commit {len(malformed)} a\n".encode()
        + malformed
        + b"\n"
        + f"{valid_id} commit {len(valid)} a\n".encode()
        + valid
        + b"\n"
    )

    first, _metadata = read_cat_file_batch_commit(stream, expect_metadata=True)
    second, _metadata = read_cat_file_batch_commit(stream, expect_metadata=True)

    assert isinstance(first, InvalidBatchCommit)
    assert first.code == "invalid_commit_object"
    assert isinstance(second, ParsedCommit)
    assert second.object_id == valid_id


def test_streaming_commit_reader_drains_non_commit_objects() -> None:
    stream = BytesIO(f"{COMMIT_ID} blob 3 a\nabc\n".encode())

    result, metadata = read_cat_file_batch_commit(stream, expect_metadata=True)

    assert isinstance(result, UnexpectedBatchObject)
    assert result.object_type == "blob"
    assert metadata == b"a"
    assert stream.read() == b""


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "truncated_cat_file_batch"),
        (f"{'d' * 40} missing\n".encode(), "unexpected_cat_file_object"),
        (f"{COMMIT_ID} too many header fields\n".encode(), "invalid_cat_file_header"),
        (f"{'z' * 40} commit 0\n\n".encode(), "unexpected_cat_file_object"),
        (f"{COMMIT_ID} commit nope\n".encode(), "invalid_cat_file_header"),
        (f"{COMMIT_ID} commit -1\n".encode(), "invalid_cat_file_header"),
        (f"{COMMIT_ID} commit 5\nabc\n".encode(), "truncated_cat_file_batch"),
        (f"{COMMIT_ID} commit 3\nabcX".encode(), "invalid_cat_file_terminator"),
        (f"{COMMIT_ID} missing\ntrailing".encode(), "unexpected_cat_file_output"),
    ],
)
def test_parse_cat_file_batch_rejects_broken_protocol(payload: bytes, code: str) -> None:
    with pytest.raises(GitObjectParseError) as error:
        parse_cat_file_batch(payload, (COMMIT_ID,))

    assert error.value.code == code
