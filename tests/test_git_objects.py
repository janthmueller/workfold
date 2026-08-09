from __future__ import annotations

import pytest
from workfold.collectors.git_objects import GitObjectParseError, parse_cat_file_batch, parse_commit_object

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
