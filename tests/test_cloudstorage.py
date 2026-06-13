"""Tests for Cloud Storage for Firebase (CloudStorage / StorageBucket).

Covers:
- bucket creation + listing
- upload / download / exists / delete
- metadata get + update (custom_metadata)
- list_objects with and without prefix
- download token generation + rotation
- ObjectNotFoundError for missing objects
- bucket deletion (all objects removed)
- binary round-trip fidelity (MD5 check)
- re-upload preserves created_at, rotates updated_at
"""

import hashlib

import pytest

from openfirebase.cloudstorage import CloudStorage, ObjectNotFoundError


@pytest.fixture
def cs():
    return CloudStorage()


@pytest.fixture
def bucket(cs):
    return cs.bucket("test-bucket")


# ---------------------------------------------------------------------------
# Bucket management
# ---------------------------------------------------------------------------

def test_bucket_created_on_first_use(cs):
    cs.bucket("my-bucket")
    assert "my-bucket" in cs.list_buckets()


def test_list_buckets_empty(cs):
    assert cs.list_buckets() == []


def test_multiple_buckets(cs):
    cs.bucket("a")
    cs.bucket("b")
    assert set(cs.list_buckets()) >= {"a", "b"}


def test_delete_bucket_removes_objects(cs):
    b = cs.bucket("del-bucket")
    b.upload("file.txt", b"hello", "text/plain")
    assert cs.delete_bucket("del-bucket") is True
    assert "del-bucket" not in cs.list_buckets()
    # object gone
    assert not cs.exists("del-bucket", "file.txt")


def test_delete_nonexistent_bucket(cs):
    assert cs.delete_bucket("ghost") is False


# ---------------------------------------------------------------------------
# Upload / Download
# ---------------------------------------------------------------------------

def test_upload_and_download_roundtrip(bucket):
    data = b"hello, cloud storage!"
    bucket.upload("greeting.txt", data, "text/plain")
    assert bucket.download("greeting.txt") == data


def test_upload_binary_fidelity(bucket):
    data = bytes(range(256)) * 64  # 16 KB with all byte values
    bucket.upload("binary.bin", data, "application/octet-stream")
    downloaded = bucket.download("binary.bin")
    assert downloaded == data
    assert hashlib.md5(downloaded).hexdigest() == hashlib.md5(data).hexdigest()


def test_download_missing_raises(bucket):
    with pytest.raises(ObjectNotFoundError):
        bucket.download("nope.txt")


def test_exists_true_and_false(bucket):
    assert bucket.exists("x.txt") is False
    bucket.upload("x.txt", b"x", "text/plain")
    assert bucket.exists("x.txt") is True


def test_upload_returns_metadata(bucket):
    meta = bucket.upload("f.txt", b"data", "text/plain")
    assert meta["name"] == "f.txt"
    assert meta["bucket"] == "test-bucket"
    assert meta["size"] == 4
    assert meta["content_type"] == "text/plain"
    assert "download_token" in meta
    assert "md5" in meta
    assert "created_at" in meta
    assert "updated_at" in meta


def test_upload_sets_md5(bucket):
    data = b"checksum me"
    meta = bucket.upload("f.txt", data, "text/plain")
    assert meta["md5"] == hashlib.md5(data).hexdigest()


def test_upload_non_bytes_raises(bucket):
    with pytest.raises(TypeError):
        bucket.upload("f.txt", "not bytes", "text/plain")  # type: ignore


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_existing(bucket):
    bucket.upload("f.txt", b"data", "text/plain")
    assert bucket.delete("f.txt") is True
    assert bucket.exists("f.txt") is False


def test_delete_missing_returns_false(bucket):
    assert bucket.delete("ghost.txt") is False


def test_delete_then_download_raises(bucket):
    bucket.upload("f.txt", b"data", "text/plain")
    bucket.delete("f.txt")
    with pytest.raises(ObjectNotFoundError):
        bucket.download("f.txt")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_get_metadata_fields(bucket):
    bucket.upload("img.png", b"\x89PNG", "image/png",
                  custom_metadata={"author": "test"})
    meta = bucket.get_metadata("img.png")
    assert meta["name"] == "img.png"
    assert meta["content_type"] == "image/png"
    assert meta["custom_metadata"]["author"] == "test"


def test_get_metadata_missing_raises(bucket):
    with pytest.raises(ObjectNotFoundError):
        bucket.get_metadata("nope.png")


def test_update_metadata_merges(bucket):
    bucket.upload("f.txt", b"x", "text/plain",
                  custom_metadata={"k1": "v1"})
    updated = bucket.update_metadata("f.txt", {"k2": "v2"})
    assert updated["custom_metadata"]["k1"] == "v1"
    assert updated["custom_metadata"]["k2"] == "v2"


def test_update_metadata_missing_raises(bucket):
    with pytest.raises(ObjectNotFoundError):
        bucket.update_metadata("ghost.txt", {"k": "v"})


def test_reupload_preserves_created_at_updates_updated_at(bucket):
    import time
    bucket.upload("f.txt", b"v1", "text/plain")
    meta1 = bucket.get_metadata("f.txt")
    time.sleep(0.01)
    bucket.upload("f.txt", b"v2", "text/plain")
    meta2 = bucket.get_metadata("f.txt")
    assert meta2["created_at"] == meta1["created_at"]
    assert meta2["updated_at"] >= meta1["updated_at"]


def test_reupload_preserves_token(bucket):
    bucket.upload("f.txt", b"v1", "text/plain")
    meta1 = bucket.get_metadata("f.txt")
    bucket.upload("f.txt", b"v2", "text/plain")
    meta2 = bucket.get_metadata("f.txt")
    assert meta2["download_token"] == meta1["download_token"]


# ---------------------------------------------------------------------------
# list_objects
# ---------------------------------------------------------------------------

def test_list_objects_empty_bucket(bucket):
    assert bucket.list_objects() == []


def test_list_objects_returns_all(bucket):
    bucket.upload("a.txt", b"a", "text/plain")
    bucket.upload("b.txt", b"b", "text/plain")
    objs = bucket.list_objects()
    names = {o["name"] for o in objs}
    assert names == {"a.txt", "b.txt"}


def test_list_objects_with_prefix(bucket):
    bucket.upload("images/cat.png", b"\x89PNG", "image/png")
    bucket.upload("images/dog.png", b"\x89PNG", "image/png")
    bucket.upload("docs/readme.txt", b"hi", "text/plain")
    imgs = bucket.list_objects(prefix="images/")
    names = {o["name"] for o in imgs}
    assert names == {"images/cat.png", "images/dog.png"}


def test_list_objects_after_delete(bucket):
    bucket.upload("a.txt", b"a", "text/plain")
    bucket.upload("b.txt", b"b", "text/plain")
    bucket.delete("a.txt")
    objs = bucket.list_objects()
    assert all(o["name"] != "a.txt" for o in objs)


# ---------------------------------------------------------------------------
# Download tokens
# ---------------------------------------------------------------------------

def test_download_token_generated_on_upload(bucket):
    meta = bucket.upload("f.txt", b"x", "text/plain")
    assert meta["download_token"]
    assert len(meta["download_token"]) == 32  # uuid4().hex


def test_rotate_token_changes_value(bucket):
    bucket.upload("f.txt", b"x", "text/plain")
    old_token = bucket.get_metadata("f.txt")["download_token"]
    new_token = bucket.rotate_token("f.txt")
    assert new_token != old_token
    assert bucket.get_metadata("f.txt")["download_token"] == new_token


def test_rotate_token_missing_raises(bucket):
    with pytest.raises(ObjectNotFoundError):
        bucket.rotate_token("ghost.txt")


# ---------------------------------------------------------------------------
# Objects with path-like names
# ---------------------------------------------------------------------------

def test_object_names_with_slashes(bucket):
    bucket.upload("a/b/c.txt", b"nested", "text/plain")
    assert bucket.download("a/b/c.txt") == b"nested"
    assert bucket.exists("a/b/c.txt")


# ---------------------------------------------------------------------------
# Service-level methods (bypass StorageBucket facade)
# ---------------------------------------------------------------------------

def test_service_upload_download(cs):
    cs.upload("svc-bucket", "obj", b"raw", "application/octet-stream")
    assert cs.download("svc-bucket", "obj") == b"raw"


def test_service_list_objects(cs):
    cs.upload("b", "x.txt", b"x", "text/plain")
    cs.upload("b", "y.txt", b"y", "text/plain")
    names = {o["name"] for o in cs.list_objects("b")}
    assert names == {"x.txt", "y.txt"}


def test_service_exists(cs):
    assert cs.exists("b2", "nope") is False
    cs.upload("b2", "yes.txt", b"", "text/plain")
    assert cs.exists("b2", "yes.txt") is True
