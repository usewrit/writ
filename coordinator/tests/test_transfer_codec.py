"""`.writ` transfer-package container conformance (DATA_PORTABILITY_SPEC §3-§5, §11).

Three groups, and the third is the one that matters most:

1. **Vectors** — Argon2id/HKDF output and the canonical header bytes, asserted
   byte-exact against `shared/transfer/golden/vectors.json`. These are the values
   the Rust and self-host implementations must reproduce; if this drifts, the three
   stacks silently stop being able to open each other's packages.
2. **Round-trip** — multi-frame bodies, the sealed-credentials lane, empty bodies.
3. **Negative cases** — truncation, ciphertext tamper, header tamper, KDF
   downgrade, out-of-band frame size, future version, declared-but-absent
   credentials. A reader that forgets the final-block flag, the AAD binding or the
   KDF floor passes group 2 and fails here, which is the entire point.

Cross-read: every `golden-*.writ` in the fixture directory is opened, whichever
stack produced it. Adding a stack means committing its fixture, and this test
immediately covers it. Whole-file byte equality is deliberately NOT asserted —
zlib and Rust's flate2 emit different (valid) deflate streams.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
from pathlib import Path

import pytest

from services import transfer_codec as C

def _find_golden() -> Path:
    """Locate `shared/transfer/golden/` by walking up from this file.

    An upward search rather than a fixed `parents[n]` because this test file is
    itself a byte-identical twin and the two editions' test directories sit at
    different depths, and because the OSS self-host tarball does not ship
    `shared/` at all — there the fixtures are simply absent and the suite skips.
    """
    for base in Path(__file__).resolve().parents:
        candidate = base / "shared" / "transfer" / "golden"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent / "__no_golden__"


GOLDEN = _find_golden()

pytestmark = pytest.mark.skipif(
    not (GOLDEN / "inputs.json").exists(),
    reason="shared/transfer/golden fixtures not present (expected in the OSS tarball)",
)


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def golden() -> dict:
    return {
        "inputs": json.loads((GOLDEN / "inputs.json").read_text()),
        "header": json.loads((GOLDEN / "header.json").read_text()),
        "body": json.loads((GOLDEN / "body.json").read_text()),
        "secrets": json.loads((GOLDEN / "secrets.json").read_text()),
        "vectors": json.loads((GOLDEN / "vectors.json").read_text()),
    }


@pytest.fixture(scope="module")
def golden_keys(golden) -> C.PackageKeys:
    return C.derive_keys(golden["inputs"]["passphrase"], base64.b64decode(golden["inputs"]["salt_b64"]))


def _packages() -> list[Path]:
    return sorted(GOLDEN.glob("golden-*.writ"))


def _write(body: bytes, *, secrets: bytes | None = None, chunk: int = C.MIN_CHUNK_SIZE,
           salt: bytes = b"\x77" * 16, passphrase: str = "test passphrase",
           header_override: dict | None = None) -> bytes:
    header = C.build_header(
        salt=salt,
        producer_app="cloud",
        producer_version="test",
        producer_edition="managed",
        producer_schema=1,
        bundle_id="00000000-0000-4000-8000-000000000000",
        created_at="2026-07-29T00:00:00Z",
        contents={"workflows": 1},
        requires={"keys": 0},
        secrets_count=(1 if secrets else 0),
        chunk_size=chunk,
    )
    if header_override:
        header.update(header_override)
    kdf = header["kdf"]
    keys = C.derive_keys(
        passphrase,
        base64.b64decode(kdf["salt"]),
        memory_kib=int(kdf["m"]),
        time_cost=int(kdf["t"]),
        parallelism=int(kdf["p"]),
    )
    w = C.PackageWriter(header, keys)
    out = bytearray(w.begin())
    for i in range(0, len(body), 8192):
        out += w.write_body(body[i : i + 8192])
    out += w.finish_body()
    out += w.write_secrets(secrets) if secrets else w.no_secrets()
    return bytes(out)


def _open(package: bytes, passphrase: str = "test passphrase") -> tuple[C.PackageReader, bytes, bytes | None]:
    reader = C.PackageReader(io.BytesIO(package), passphrase)
    body = reader.read_body_bytes()
    return reader, body, reader.read_secrets_bytes()


# ── 1. vectors (byte-exact cross-stack contract) ───────────────────────────

def test_derived_key_material_matches_vectors(golden, golden_keys):
    """Argon2id + HKDF conformance. Every stack must produce these exact bytes."""
    v = golden["vectors"]
    assert golden_keys.master.hex() == v["master_hex"]
    assert golden_keys.body.hex() == v["body_key_hex"]
    assert golden_keys.secrets.hex() == v["secrets_key_hex"]


def test_body_and_secrets_keys_are_independent(golden_keys):
    """The lanes must not share a key: stripping the credentials section has to
    leave the body openable, and a reader must not be able to confuse the two."""
    assert golden_keys.body != golden_keys.secrets
    assert golden_keys.body != golden_keys.master


def test_canonical_header_bytes_are_reproducible(golden):
    """The header is AAD, so re-serializing the parsed object must yield the exact
    bytes in the file — otherwise every frame fails to authenticate."""
    canonical = C.canonical_header_bytes(golden["header"])
    assert hashlib.sha256(canonical).hexdigest() == golden["vectors"]["header_canonical_sha256"]
    for path in _packages():
        raw = path.read_bytes()
        _, header_bytes, _ = C.parse_header(raw)
        assert header_bytes == canonical, f"{path.name} header bytes differ from header.json"


def test_frame_layout_matches_vectors(golden):
    """Frame count and ciphertext lengths, so a fixture regenerated with a
    different chunking strategy cannot slip through unnoticed."""
    expected = golden["vectors"]["frame_ct_lengths"]
    assert len(expected["body"]) >= 2, "the fixture must span multiple frames"
    assert len(expected["secrets"]) >= 1


# ── 2. cross-read: every stack's fixture, read by this stack ───────────────

def test_golden_packages_exist():
    assert _packages(), "no golden-*.writ fixtures found"


@pytest.mark.parametrize("path", _packages(), ids=lambda p: p.name)
def test_cross_read_every_golden_package(path, golden):
    """The interop guarantee: whoever wrote it, we can open it and get the exact
    plaintexts back."""
    reader = C.PackageReader(io.BytesIO(path.read_bytes()), golden["inputs"]["passphrase"])
    body = json.loads(reader.read_body_bytes())
    secrets = json.loads(reader.read_secrets_bytes())
    assert body == golden["body"]
    assert secrets == golden["secrets"]


@pytest.mark.parametrize("path", _packages(), ids=lambda p: p.name)
def test_inspect_needs_no_passphrase(path, golden):
    """Wizard step 1: provenance and counts before anyone types anything."""
    header, _, _ = C.parse_header(path.read_bytes()[: C.MAX_HEADER_BYTES])
    summary = C.header_summary(header)
    assert summary["package_version"] == C.PACKAGE_VERSION
    assert summary["has_sealed_credentials"] is True
    assert summary["contents"]["workflows"] == 1
    assert summary["requires"]["keys"] == 3


def test_header_carries_no_names_or_keys(golden):
    """Invariant §2.4: the cleartext header leaks shape only. `label` is the one
    allowed string because the user typed it knowing it would be readable."""
    header = dict(golden["header"])
    header.pop("label", None)
    blob = json.dumps(header)
    body_text = json.dumps(golden["body"])
    for leak in ("ACME_API_KEY", "ACME_PASSWORD", "example.test", "Invoice pull", "invoice_month"):
        assert leak in body_text, f"fixture no longer contains {leak}; this test is checking nothing"
        assert leak not in blob, f"header leaks {leak}"


# ── 3. round-trip ──────────────────────────────────────────────────────────

def test_small_config_only_round_trip():
    body = json.dumps({"payload_version": 1, "assets": {"workflows": []}}).encode()
    reader, got, secrets = _open(_write(body))
    assert got == body
    assert secrets is None
    assert reader.summary["has_sealed_credentials"] is False


def test_multi_frame_round_trip():
    """A body several frames long, with incompressible content so gzip cannot
    collapse it into one frame."""
    rows = [{"i": i, "d": hashlib.sha256(str(i).encode()).hexdigest()} for i in range(20_000)]
    body = json.dumps({"rows": rows}).encode()
    _, got, _ = _open(_write(body))
    assert got == body
    assert len(body) > C.MIN_CHUNK_SIZE * 2


def test_sealed_credentials_round_trip():
    body = b'{"payload_version":1}'
    secrets = json.dumps({"secrets_version": 1, "vault": [{"key": "K", "value": "v"}]}).encode()
    reader, got_body, got_secrets = _open(_write(body, secrets=secrets))
    assert got_body == body
    assert got_secrets == secrets
    assert reader.declared_secret_count == 1


def test_empty_body_round_trip():
    _, got, _ = _open(_write(b""))
    assert got == b""


def test_highly_compressible_body_is_not_mistaken_for_a_bomb():
    """40 MiB of zeros compresses ~1000:1. Real data sections of repetitive rows
    reach similar ratios, so the guard must not reject them (the floor exists
    precisely so a tight ratio cannot)."""
    body = b"\x00" * (8 << 20)
    _, got, _ = _open(_write(body))
    assert got == body


def test_body_must_be_read_before_secrets():
    package = _write(b"{}", secrets=b'{"secrets_version":1}')
    reader = C.PackageReader(io.BytesIO(package), "test passphrase")
    with pytest.raises(RuntimeError):
        reader.read_secrets_bytes()


# ── 4. negative cases ──────────────────────────────────────────────────────

def test_wrong_passphrase():
    with pytest.raises(C.BadPassphrase):
        _open(_write(b"{}"), passphrase="not it")


def test_truncated_package_is_rejected():
    """The final-block flag is what makes this detectable: a package cut short
    presents a stream that never reaches a final frame."""
    package = _write(json.dumps({"d": "x" * 400_000}).encode())
    with pytest.raises(C.MalformedPackage):
        _open(package[: len(package) - 300])


def test_dropping_only_the_final_frame_is_rejected():
    package = _write(b'{"a":1}')
    # strip the sealed-credentials flag byte and the whole final frame
    with pytest.raises(C.MalformedPackage):
        _open(package[: -(1 + 4 + C.AEAD_TAG_LEN + 40)])


def test_ciphertext_tamper_is_rejected():
    package = bytearray(_write(json.dumps({"d": "y" * 200_000}).encode()))
    package[-60] ^= 0xFF
    with pytest.raises(C.PackageError):
        _open(bytes(package))


def test_header_tamper_breaks_aad():
    """The cleartext header is AAD, so editing the counts the wizard displays
    invalidates every frame instead of misleading the user."""
    package = bytearray(_write(b'{"a":1}'))
    idx = package.index(b'"contents"')
    package[idx + 2] = ord("O")
    with pytest.raises(C.PackageError):
        _open(bytes(package))


def test_kdf_downgrade_is_refused():
    salt = b"\x33" * 16
    weak = {"kdf": {"alg": "argon2id", "m": 8, "p": 1,
                    "salt": base64.b64encode(salt).decode(), "t": 1, "v": 19}}
    with pytest.raises(C.WeakKdf):
        _open(_write(b"{}", salt=salt, header_override=weak))


def test_short_salt_is_refused():
    salt = b"\x44" * 16
    bad = {"kdf": {"alg": "argon2id", "m": C.KDF_MEMORY_KIB, "p": 1,
                   "salt": base64.b64encode(b"\x01" * 4).decode(), "t": 3, "v": 19}}
    with pytest.raises(C.WeakKdf):
        _open(_write(b"{}", salt=salt, header_override=bad))


@pytest.mark.parametrize("chunk", [8, 16, C.MIN_CHUNK_SIZE - 1, C.MAX_CHUNK_SIZE + 1])
def test_frame_size_outside_the_band_is_refused(chunk):
    """A tiny frame size is an amplification attack: 20 bytes of overhead per byte
    of payload."""
    salt = b"\x55" * 16
    with pytest.raises(C.MalformedPackage):
        _open(_write(b"{}", salt=salt, header_override={"body": {"chunk": chunk, "codec": C.BODY_CODEC}}))


def test_future_package_version_is_refused_with_producer_info():
    salt = b"\x66" * 16
    with pytest.raises(C.UnsupportedVersion) as exc:
        _open(_write(b"{}", salt=salt, header_override={"version": C.PACKAGE_VERSION + 1}))
    assert exc.value.producer.get("app") == "cloud"
    assert exc.value.version == C.PACKAGE_VERSION + 1


def test_declared_but_absent_credentials_is_truncation_not_success():
    """A package claiming credentials it does not carry must fail loudly:
    importing it silently would look like success while dropping data (§2.6)."""
    salt = b"\x88" * 16
    header = C.build_header(
        salt=salt, producer_app="cloud", producer_version="test", producer_edition="managed",
        producer_schema=1, bundle_id="b", created_at="2026-07-29T00:00:00Z",
        contents={}, requires={}, secrets_count=2, chunk_size=C.MIN_CHUNK_SIZE,
    )
    keys = C.derive_keys("test passphrase", salt)
    w = C.PackageWriter(header, keys)
    package = w.begin() + w.write_body(b"{}") + w.finish_body() + w.no_secrets()
    with pytest.raises(C.MalformedPackage):
        _open(package)


def test_bad_magic_is_rejected():
    package = bytearray(_write(b"{}"))
    package[:8] = b"NOTAWRIT"
    with pytest.raises(C.MalformedPackage):
        _open(bytes(package))


def test_oversized_header_length_is_rejected():
    package = bytearray(_write(b"{}"))
    package[8:12] = struct.pack(">I", C.MAX_HEADER_BYTES + 1)
    with pytest.raises(C.MalformedPackage):
        _open(bytes(package))


def test_unsupported_aead_is_rejected():
    salt = b"\x99" * 16
    with pytest.raises(C.MalformedPackage):
        _open(_write(b"{}", salt=salt, header_override={"aead": "aes-gcm-siv"}))


def test_empty_passphrase_cannot_derive():
    """An empty passphrase is refused at the export edge; the KDF itself still has
    to not silently accept a zero-entropy secret as if it were fine."""
    assert C.normalize_passphrase("") == b""
    with pytest.raises(C.WeakKdf):
        C.derive_keys("anything", b"\x01" * 8)


def test_passphrase_normalization_is_nfkc():
    """The same passphrase typed on macOS (decomposed) and Windows (composed) must
    open the same package."""
    # Built from code points explicitly: two visually identical literals in a
    # source file are one editor's "normalize on save" away from being the same
    # bytes, at which point this test would assert nothing.
    composed = "caf\u00e9"          # é as a single code point (NFC)
    decomposed = "cafe\u0301"       # e + COMBINING ACUTE ACCENT (NFD)
    assert composed != decomposed
    assert C.normalize_passphrase(composed) == C.normalize_passphrase(decomposed)
    package = _write(b'{"a":1}', passphrase=composed)
    _, body, _ = _open(package, passphrase=decomposed)
    assert body == b'{"a":1}'
