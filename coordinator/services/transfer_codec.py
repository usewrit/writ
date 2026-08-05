"""
transfer_codec — the `.writ` transfer-package container (`WRITPKG1`).

THE SPEC IS `DATA_PORTABILITY_SPEC.md` (repo root) §3-§5. Read it before touching
anything here; the byte layout is a cross-stack contract, not an implementation
detail.

TWIN FILE — this module exists TWICE, once per edition (cloud and self-host),
and the two copies must stay BYTE-IDENTICAL. Editing one without the other is a
bug even when both test suites pass, because the two editions must be able to
open each other's packages; CI diffs them. There is intentionally NO
edition-specific text anywhere in this file — that is what makes the diff a valid
check.

The desktop agent carries a third implementation, in another language; all three
are held to the same wire format by the shared golden fixtures under
`shared/transfer/golden/` (see DATA_PORTABILITY_SPEC §11).

WHAT THIS MODULE IS
-------------------
A *streaming* AEAD container. It knows nothing about workflows, monitors or slots
— that is `transfer_bundle.py`. This layer is:

    WRITPKG1 | u32be header_len | header_json | body section | u8 | [secrets section]
    section  ::= nonce_prefix(7B) , chunk+
    chunk    ::= u32be ct_len , ChaCha20Poly1305(key).seal(nonce_i, pt_i, aad=header_json)
    nonce_i  ::= nonce_prefix || u32be(i) || (last ? 0x01 : 0x00)

Design notes that are load-bearing (do not "simplify" these away):

* **Chunked AEAD (the STREAM construction, as `age` uses).** Memory is O(chunk),
  never O(package), on both the write and the read path. A one-shot
  `ChaCha20Poly1305.encrypt()` over a 400 MiB body would put the whole package in
  RAM twice, on every export AND every import, for every tenant concurrently.
* **Sections are self-delimiting** via the final-block flag in the last nonce
  byte. There is no length field and no digest anywhere in the file, so the writer
  needs no second pass / spool file, and there are no two size fields that can
  disagree. Truncation is caught by "the stream ended without a final chunk".
* **The cleartext header is the AAD of every chunk.** The import wizard shows the
  user provenance and counts from that header BEFORE they type a passphrase, so
  those bytes must not be forgeable. `aad` is always the exact bytes as they
  appear in the file — never a re-serialization (see `header_bytes` threading).
* **The KDF floor.** Parameters come from the header (so a future writer can
  retune), but a package below `MIN_KDF_*` is refused: otherwise an attacker
  rewrites `m=65536` to `m=8` and brute-forces the passphrase offline.
* **Everything here is synchronous** and takes/returns bytes. `derive_keys()` is
  ~100ms of CPU at 64 MiB and MUST be called off the event loop
  (`asyncio.to_thread`) by async callers; the frame-sealing calls are cheap enough
  to drive directly from an async generator, which is how export streams a body
  it is still reading out of the database.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import struct
import unicodedata
import zlib
from typing import Any, BinaryIO, Iterator, Optional

from argon2.low_level import Type as _Argon2Type, hash_secret_raw
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# ---------------------------------------------------------------------------
# Container constants. Changing any of these changes the on-disk format and
# requires a coordinated change in all three stacks + new golden fixtures.
# ---------------------------------------------------------------------------

#: Container generation. Bump ONLY for a change an older reader cannot parse at
#: all; readers reject unknown magic outright (same discipline as `WRITBKP1`).
CONTAINER_MAGIC = b"WRITPKG1"

#: Payload-contract generation, carried in `header["version"]`. A reader refuses a
#: version greater than this; within a known version, unknown fields/kinds are
#: reported rather than silently dropped (spec §6.6).
PACKAGE_VERSION = 1

FORMAT_NAME = "writ-pkg"
AEAD_NAME = "chacha20poly1305"
BODY_CODEC = "gzip+json"

#: Writer's plaintext frame size. Readers honour `header["body"]["chunk"]`.
DEFAULT_CHUNK_SIZE = 1 << 20  # 1 MiB
MIN_CHUNK_SIZE = 64 << 10     # 64 KiB
MAX_CHUNK_SIZE = 4 << 20      # 4 MiB

NONCE_PREFIX_LEN = 7          # || u32be counter || final-flag byte == 12
AEAD_TAG_LEN = 16
KEY_LEN = 32
SALT_LEN = 16

HKDF_INFO_BODY = b"writ-pkg/v1/body"
HKDF_INFO_SECRETS = b"writ-pkg/v1/secrets"

# Argon2id parameters the writer emits. Interop is by VALUE (they travel in the
# header), not by agreement — but all three stacks default to the same ones so a
# package looks the same wherever it was made.
KDF_ALG = "argon2id"
KDF_VERSION = 19             # 0x13
KDF_MEMORY_KIB = 65536       # 64 MiB
KDF_TIME_COST = 3
KDF_PARALLELISM = 1

# The floor a package must clear to be opened at all (anti-downgrade).
MIN_KDF_MEMORY_KIB = 19456   # OWASP's Argon2id floor (19 MiB)
MIN_KDF_TIME_COST = 2
MIN_KDF_PARALLELISM = 1

# Reader caps, enforced as the stream is consumed (spec §3).
MAX_HEADER_BYTES = 64 << 10
MAX_CHUNKS_PER_SECTION = 524_288
MAX_INFLATED_BYTES = 8 * (1 << 30)      # 8 GiB — the absolute ceiling on work
# Gzip-bomb guard. A DATA section of half a million similar JSON rows legitimately
# compresses 100-1000:1, so a tight ratio would reject real packages; a bomb is
# 10^6:1 and up. The effective limit is
#   min(MAX_INFLATED_BYTES, max(MAX_INFLATED_FLOOR, ciphertext_seen * RATIO))
# which bounds a TINY hostile package to the floor while letting a genuinely large
# export through, and never lets anything past 8 GiB.
MAX_INFLATION_RATIO = 1000
MAX_INFLATED_FLOOR = 256 << 20          # 256 MiB

_HEADER_PRELUDE_LEN = len(CONTAINER_MAGIC) + 4


# ---------------------------------------------------------------------------
# Errors. `code` is what the REST edge maps to an HTTP status + message key, so
# callers branch on the variant instead of matching on prose.
# ---------------------------------------------------------------------------

class PackageError(Exception):
    """Base for every container-level failure."""

    code = "PACKAGE_ERROR"


class MalformedPackage(PackageError):
    """Not a package, or a damaged/hand-edited one (bad magic, bad framing,
    truncated stream, out-of-band header values)."""

    code = "MALFORMED"


class UnsupportedVersion(PackageError):
    """Made by a newer Writ than this one. Carries the producer so the message can
    say what to upgrade."""

    code = "UNSUPPORTED_VERSION"

    def __init__(self, message: str, *, producer: Optional[dict] = None, version: Optional[int] = None):
        super().__init__(message)
        self.producer = producer or {}
        self.version = version


class WeakKdf(PackageError):
    """KDF parameters below the floor — a downgrade attempt, or a package from a
    writer we would not want to trust anyway."""

    code = "KDF_TOO_WEAK"


class BadPassphrase(PackageError):
    """Wrong passphrase OR a damaged package. These are indistinguishable at the
    AEAD layer and we never pretend otherwise (mirrors `BackupError::Decrypt`)."""

    code = "BAD_PASSPHRASE"


class PackageTooLarge(PackageError):
    """A reader cap was hit mid-stream."""

    code = "TOO_LARGE"


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

class PackageKeys:
    """The two section keys derived from one passphrase.

    Held together so no call site can accidentally seal the body with the secrets
    key. `master` is retained only for the golden-vector test; nothing else reads
    it, and it is never logged or returned over an API.
    """

    __slots__ = ("master", "body", "secrets")

    def __init__(self, master: bytes, body: bytes, secrets: bytes):
        self.master = master
        self.body = body
        self.secrets = secrets


def normalize_passphrase(passphrase: str) -> bytes:
    """NFKC + UTF-8. A passphrase typed on macOS and retyped on Windows must
    derive the same key, so composed/decomposed forms have to normalize to one
    encoding. Deliberately NOT trimmed — leading/trailing space is part of a
    passphrase — but an empty or all-whitespace one is refused at export."""
    return unicodedata.normalize("NFKC", passphrase).encode("utf-8")


def _hkdf(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LEN, salt=b"", info=info).derive(master)


def derive_keys(
    passphrase: str,
    salt: bytes,
    *,
    memory_kib: int = KDF_MEMORY_KIB,
    time_cost: int = KDF_TIME_COST,
    parallelism: int = KDF_PARALLELISM,
) -> PackageKeys:
    """Argon2id → master → HKDF-SHA256 → (body_key, secrets_key).

    CPU-bound (~100ms at 64 MiB): async callers MUST run this in a thread. The two
    subkeys are independent so the sealed-credentials section can be stripped from
    a package without touching the body ciphertext, and so a reader that refuses to
    handle that lane still opens the body.
    """
    if len(salt) < SALT_LEN:
        raise WeakKdf(f"salt must be at least {SALT_LEN} bytes")
    master = hash_secret_raw(
        secret=normalize_passphrase(passphrase),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_kib,
        parallelism=parallelism,
        hash_len=KEY_LEN,
        type=_Argon2Type.ID,
        version=KDF_VERSION,
    )
    return PackageKeys(master, _hkdf(master, HKDF_INFO_BODY), _hkdf(master, HKDF_INFO_SECRETS))


def derive_keys_for_header(passphrase: str, header: dict) -> PackageKeys:
    """`derive_keys` with the parameters a package declares, after enforcing the
    floor. This is the ONLY way the read path derives keys — never with local
    defaults, which would silently succeed on a package written with other
    (legitimate) parameters and fail on nothing."""
    kdf = validate_kdf(header)
    return derive_keys(
        passphrase,
        base64.b64decode(kdf["salt"]),
        memory_kib=int(kdf["m"]),
        time_cost=int(kdf["t"]),
        parallelism=int(kdf["p"]),
    )


def validate_kdf(header: dict) -> dict:
    """Enforce the anti-downgrade floor and return the validated kdf block."""
    kdf = header.get("kdf")
    if not isinstance(kdf, dict):
        raise MalformedPackage("package header has no kdf block")
    if kdf.get("alg") != KDF_ALG:
        raise MalformedPackage(f"unsupported kdf algorithm: {kdf.get('alg')!r}")
    if int(kdf.get("v") or 0) != KDF_VERSION:
        raise MalformedPackage(f"unsupported argon2 version: {kdf.get('v')!r}")
    try:
        salt = base64.b64decode(str(kdf.get("salt") or ""), validate=True)
    except Exception:
        raise MalformedPackage("kdf salt is not valid base64")
    memory = int(kdf.get("m") or 0)
    time_cost = int(kdf.get("t") or 0)
    parallelism = int(kdf.get("p") or 0)
    if len(salt) < SALT_LEN:
        raise WeakKdf("package salt is too short")
    if memory < MIN_KDF_MEMORY_KIB or time_cost < MIN_KDF_TIME_COST or parallelism < MIN_KDF_PARALLELISM:
        # An attacker who can edit the header would otherwise rewrite these to
        # m=8,t=1 and brute-force the passphrase at millions of guesses/sec.
        raise WeakKdf(
            "this package declares password-hashing parameters below the minimum "
            "Writ accepts, so it cannot be opened safely"
        )
    return kdf


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def canonical_header_bytes(header: dict) -> bytes:
    """Canonical JSON: UTF-8, sorted keys, tight separators, no trailing newline.

    Canonical because these bytes are AAD — writer and reader must agree on them
    exactly, and a reader in another language must be able to reproduce them from
    the parsed object for the golden-vector test.
    """
    return json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_header(
    *,
    salt: bytes,
    producer_app: str,
    producer_version: str,
    producer_edition: str,
    producer_schema: Optional[int],
    bundle_id: str,
    created_at: str,
    contents: dict,
    requires: dict,
    secrets_count: int = 0,
    label: Optional[str] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict:
    """Assemble the cleartext header.

    Carries COUNTS AND SHAPE ONLY (spec invariant §2.4). No asset names, no URLs,
    no key names, no domains: an unencrypted `ACME_PAYROLL_PASSWORD` in a header is
    a leak even though the value is not there. `label` is the single exception —
    the user typed it knowing it would be readable.
    """
    header = {
        "aead": AEAD_NAME,
        "body": {"chunk": int(chunk_size), "codec": BODY_CODEC},
        "bundle_id": str(bundle_id),
        "contents": {str(k): int(v) for k, v in sorted((contents or {}).items())},
        "created_at": created_at,
        "format": FORMAT_NAME,
        "kdf": {
            "alg": KDF_ALG,
            "m": KDF_MEMORY_KIB,
            "p": KDF_PARALLELISM,
            "salt": base64.b64encode(salt).decode("ascii"),
            "t": KDF_TIME_COST,
            "v": KDF_VERSION,
        },
        "producer": {
            "app": producer_app,
            "edition": producer_edition,
            "schema": producer_schema,
            "version": producer_version,
        },
        "requires": {str(k): int(v) for k, v in sorted((requires or {}).items())},
        "secrets": {"count": int(secrets_count)},
        "version": PACKAGE_VERSION,
    }
    if label:
        header["label"] = str(label)[:120]
    return header


def parse_header(buf: bytes) -> tuple[dict, bytes, int]:
    """Parse the prelude + header from the FIRST bytes of a package.

    Returns `(header, header_bytes, total_prelude_len)` where `header_bytes` is the
    exact slice from the file (the AAD) and `total_prelude_len` is where the body
    section begins. Deliberately tolerant of a short buffer only in that it says
    so: callers hand it at least `_HEADER_PRELUDE_LEN` bytes and then, once the
    length is known, the full header.

    This is also the whole implementation of "inspect a package without its
    passphrase" — the wizard's step 1.
    """
    if len(buf) < _HEADER_PRELUDE_LEN:
        raise MalformedPackage("file is too short to be a Writ package")
    if buf[: len(CONTAINER_MAGIC)] != CONTAINER_MAGIC:
        raise MalformedPackage("not a Writ package (bad magic)")
    (header_len,) = struct.unpack(">I", buf[len(CONTAINER_MAGIC) : _HEADER_PRELUDE_LEN])
    if header_len == 0 or header_len > MAX_HEADER_BYTES:
        raise MalformedPackage("package header length is out of range")
    end = _HEADER_PRELUDE_LEN + header_len
    if len(buf) < end:
        raise MalformedPackage("package header is truncated")
    header_bytes = bytes(buf[_HEADER_PRELUDE_LEN : end])
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except Exception:
        raise MalformedPackage("package header is not valid JSON")
    if not isinstance(header, dict):
        raise MalformedPackage("package header is not an object")
    _validate_header_shape(header)
    return header, header_bytes, end


def _validate_header_shape(header: dict) -> None:
    if header.get("format") != FORMAT_NAME:
        raise MalformedPackage("not a Writ package (unexpected format marker)")
    version = header.get("version")
    if not isinstance(version, int) or version < 1:
        raise MalformedPackage("package header has no usable version")
    if version > PACKAGE_VERSION:
        producer = header.get("producer") if isinstance(header.get("producer"), dict) else {}
        raise UnsupportedVersion(
            "this package was made by a newer version of Writ than this one can read",
            producer=producer,
            version=version,
        )
    if header.get("aead") != AEAD_NAME:
        raise MalformedPackage(f"unsupported encryption: {header.get('aead')!r}")
    body = header.get("body")
    if not isinstance(body, dict) or body.get("codec") != BODY_CODEC:
        raise MalformedPackage("unsupported package body encoding")
    chunk = body.get("chunk")
    if not isinstance(chunk, int) or not (MIN_CHUNK_SIZE <= chunk <= MAX_CHUNK_SIZE):
        # A tiny chunk size is an amplification attack: 16 bytes of tag and 4 of
        # framing per byte of payload.
        raise MalformedPackage("package frame size is out of range")
    validate_kdf(header)


def header_summary(header: dict) -> dict:
    """The passphrase-free preview the import wizard's first step renders."""
    producer = header.get("producer") if isinstance(header.get("producer"), dict) else {}
    secrets = header.get("secrets") if isinstance(header.get("secrets"), dict) else {}
    return {
        "bundle_id": header.get("bundle_id"),
        "label": header.get("label"),
        "created_at": header.get("created_at"),
        "producer_app": producer.get("app"),
        "producer_version": producer.get("version"),
        "producer_edition": producer.get("edition"),
        "producer_schema": producer.get("schema"),
        "package_version": header.get("version"),
        "contents": header.get("contents") or {},
        "requires": header.get("requires") or {},
        "has_sealed_credentials": int(secrets.get("count") or 0) > 0,
        "sealed_credential_count": int(secrets.get("count") or 0),
    }


# ---------------------------------------------------------------------------
# Framing primitives
# ---------------------------------------------------------------------------

def _nonce(prefix: bytes, counter: int, final: bool) -> bytes:
    if counter > 0xFFFFFFFF:  # pragma: no cover - unreachable under the chunk cap
        raise PackageTooLarge("package has too many frames")
    return prefix + struct.pack(">I", counter) + (b"\x01" if final else b"\x00")


class _SectionWriter:
    """Seals a plaintext stream into `nonce_prefix || (u32be len || chunk)+`.

    Owns the gzip compressor so callers just feed JSON bytes. Emits a frame only
    once `chunk_size` compressed bytes have accumulated, which is what keeps
    memory flat while the caller streams assets in one at a time.
    """

    def __init__(self, key: bytes, aad: bytes, chunk_size: int, prefix: Optional[bytes] = None):
        self._cipher = ChaCha20Poly1305(key)
        self._aad = aad
        self._chunk_size = chunk_size
        self._prefix = prefix if prefix is not None else os.urandom(NONCE_PREFIX_LEN)
        if len(self._prefix) != NONCE_PREFIX_LEN:
            raise ValueError("nonce prefix must be exactly 7 bytes")
        self._gz = zlib.compressobj(6, zlib.DEFLATED, 31)  # 31 => gzip container
        self._buf = bytearray()
        self._counter = 0
        self._done = False
        self._started = False

    def begin(self) -> bytes:
        self._started = True
        return self._prefix

    def write(self, data: bytes) -> bytes:
        if self._done:
            raise RuntimeError("section already finished")
        if data:
            self._buf += self._gz.compress(data)
        return self._drain()

    def finish(self) -> bytes:
        if self._done:
            raise RuntimeError("section already finished")
        self._buf += self._gz.flush()
        self._done = True
        out = bytearray()
        # `>` not `>=`: when what remains is exactly one chunk, it becomes the
        # FINAL frame instead of a full frame followed by an empty final one.
        # Exactly one frame per section carries the final flag, always.
        while len(self._buf) > self._chunk_size:
            out += self._frame(bytes(self._buf[: self._chunk_size]), final=False)
            del self._buf[: self._chunk_size]
        out += self._frame(bytes(self._buf), final=True)
        self._buf = bytearray()
        return bytes(out)

    def _drain(self) -> bytes:
        out = bytearray()
        while len(self._buf) >= self._chunk_size:
            out += self._frame(bytes(self._buf[: self._chunk_size]), final=False)
            del self._buf[: self._chunk_size]
        return bytes(out)

    def _frame(self, plaintext: bytes, *, final: bool) -> bytes:
        nonce = _nonce(self._prefix, self._counter, final)
        self._counter += 1
        if self._counter > MAX_CHUNKS_PER_SECTION:
            raise PackageTooLarge("package has too many frames")
        ct = self._cipher.encrypt(nonce, plaintext, self._aad)
        return struct.pack(">I", len(ct)) + ct


class PackageWriter:
    """Incremental, single-pass package writer.

    Usage (the async export path drives it exactly like this):

        keys   = await asyncio.to_thread(derive_keys, passphrase, salt)
        writer = PackageWriter(header, keys)
        yield writer.begin()
        async for piece in body_json_pieces():
            yield writer.write_body(piece)
        yield writer.finish_body()
        yield writer.write_secrets(secrets_json_bytes)   # or writer.no_secrets()
        # done — nothing to flush, the sections are self-delimiting

    Every method returns bytes to send onward; nothing is buffered beyond one
    frame, so the caller's memory is O(chunk) no matter how large the export.
    """

    def __init__(
        self,
        header: dict,
        keys: PackageKeys,
        *,
        body_prefix: Optional[bytes] = None,
        secrets_prefix: Optional[bytes] = None,
    ):
        self.header = header
        self.header_bytes = canonical_header_bytes(header)
        if len(self.header_bytes) > MAX_HEADER_BYTES:
            raise MalformedPackage("package header is too large")
        chunk = int(header["body"]["chunk"])
        self._body = _SectionWriter(keys.body, self.header_bytes, chunk, body_prefix)
        self._secrets_key = keys.secrets
        self._secrets_prefix = secrets_prefix
        self._chunk = chunk
        self._body_finished = False
        self._closed = False

    def begin(self) -> bytes:
        return (
            CONTAINER_MAGIC
            + struct.pack(">I", len(self.header_bytes))
            + self.header_bytes
            + self._body.begin()
        )

    def write_body(self, data: bytes) -> bytes:
        return self._body.write(data)

    def finish_body(self) -> bytes:
        out = self._body.finish()
        self._body_finished = True
        return out

    def no_secrets(self) -> bytes:
        """Close the package without a sealed-credentials lane."""
        self._require_body_done()
        self._closed = True
        return b"\x00"

    def write_secrets(self, plaintext: bytes) -> bytes:
        """Seal the credentials lane (spec §8). Small by construction — key/value
        pairs — so it is written in one call, still framed."""
        self._require_body_done()
        sec = _SectionWriter(self._secrets_key, self.header_bytes, self._chunk, self._secrets_prefix)
        self._closed = True
        return b"\x01" + sec.begin() + sec.write(plaintext) + sec.finish()

    def _require_body_done(self) -> None:
        if not self._body_finished:
            raise RuntimeError("finish_body() must be called before closing the package")
        if self._closed:
            raise RuntimeError("package already closed")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

class _SectionReader:
    """Streams one section back to plaintext, enforcing every reader cap as it
    goes rather than after buffering.

    A hostile package is therefore rejected after ~one frame of work, and a gzip
    bomb is aborted mid-inflate.
    """

    def __init__(self, stream: BinaryIO, key: bytes, aad: bytes, chunk_size: int):
        self._stream = stream
        self._cipher = ChaCha20Poly1305(key)
        self._aad = aad
        self._max_ct = chunk_size + AEAD_TAG_LEN
        self._prefix = _read_exact(stream, NONCE_PREFIX_LEN, "section nonce")
        self._counter = 0
        self._gz = zlib.decompressobj(31)
        self._ct_bytes = 0
        self._pt_bytes = 0
        self._final_seen = False

    @property
    def final_seen(self) -> bool:
        return self._final_seen

    def chunks(self) -> Iterator[bytes]:
        """Yield inflated plaintext pieces until the final frame is consumed."""
        while not self._final_seen:
            (ct_len,) = struct.unpack(">I", _read_exact(self._stream, 4, "frame length"))
            # `< AEAD_TAG_LEN` — a frame whose plaintext is empty is legitimate
            # (it is exactly the tag), and a writer whose gzip flush lands on a
            # chunk boundary can produce one.
            if ct_len < AEAD_TAG_LEN or ct_len > self._max_ct:
                raise MalformedPackage("package frame length is out of range")
            ct = _read_exact(self._stream, ct_len, "frame")
            self._ct_bytes += ct_len
            if self._counter >= MAX_CHUNKS_PER_SECTION:
                raise PackageTooLarge("package has too many frames")

            # We do not know whether this is the final frame until we try: the flag
            # is inside the nonce. Try non-final first (the common case), then
            # final. A tag failure on BOTH is a wrong passphrase or damage.
            plain = None
            for final in (False, True):
                try:
                    plain = self._cipher.decrypt(_nonce(self._prefix, self._counter, final), ct, self._aad)
                    self._final_seen = final
                    break
                except Exception:
                    continue
            if plain is None:
                raise BadPassphrase("could not open this package (wrong passphrase or damaged file)")
            self._counter += 1

            out = self._gz.decompress(plain)
            if out:
                self._account(len(out))
                yield out

        tail = self._gz.flush()
        if tail:
            self._account(len(tail))
            yield tail
        if not self._gz.eof:
            # Final AEAD frame consumed but the gzip stream is incomplete: the
            # package was assembled wrong, not merely truncated.
            raise MalformedPackage("package body is incomplete")

    def _account(self, n: int) -> None:
        self._pt_bytes += n
        if self._pt_bytes > MAX_INFLATED_BYTES:
            raise PackageTooLarge("package expands to more data than Writ will import")
        allowed = min(MAX_INFLATED_BYTES, max(MAX_INFLATED_FLOOR, self._ct_bytes * MAX_INFLATION_RATIO))
        if self._pt_bytes > allowed:
            raise PackageTooLarge("package expansion ratio is implausible (possible zip bomb)")


def _read_exact(stream: BinaryIO, n: int, what: str) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        got = stream.read(n - len(buf))
        if not got:
            # The single most important error in this module: a section that ends
            # before its final-flagged frame IS a truncated package.
            raise MalformedPackage(f"package is truncated (incomplete {what})")
        buf += got
    return bytes(buf)


def read_package_header(stream: BinaryIO) -> tuple[dict, bytes]:
    """Read and validate just the header from a seekable/streamable file object,
    leaving the cursor at the start of the body section."""
    prelude = _read_exact(stream, _HEADER_PRELUDE_LEN, "prelude")
    (header_len,) = struct.unpack(">I", prelude[len(CONTAINER_MAGIC) :])
    if prelude[: len(CONTAINER_MAGIC)] != CONTAINER_MAGIC:
        raise MalformedPackage("not a Writ package (bad magic)")
    if header_len == 0 or header_len > MAX_HEADER_BYTES:
        raise MalformedPackage("package header length is out of range")
    header, header_bytes, _ = parse_header(prelude + _read_exact(stream, header_len, "header"))
    return header, header_bytes


class PackageReader:
    """Streaming reader. Open the body, optionally then the secrets lane.

    The body MUST be fully consumed before `read_secrets()`, because the sections
    are sequential in the file and self-delimiting — there is no offset to seek to.
    `open_body()` returns an iterator specifically so a caller can spill a large
    body to its staging store without materializing it.
    """

    def __init__(self, stream: BinaryIO, passphrase: str):
        self.header, self.header_bytes = read_package_header(stream)
        self._stream = stream
        self._chunk = int(self.header["body"]["chunk"])
        # Argon2id: the caller is responsible for keeping this off the event loop
        # (the REST edge constructs PackageReader inside a worker thread).
        self._keys = derive_keys_for_header(passphrase, self.header)
        self._body_reader: Optional[_SectionReader] = None
        self._body_done = False

    @property
    def summary(self) -> dict:
        return header_summary(self.header)

    @property
    def declared_secret_count(self) -> int:
        secrets = self.header.get("secrets") or {}
        return int(secrets.get("count") or 0)

    def open_body(self) -> Iterator[bytes]:
        if self._body_reader is not None:
            raise RuntimeError("body already opened")
        self._body_reader = _SectionReader(self._stream, self._keys.body, self.header_bytes, self._chunk)
        for piece in self._body_reader.chunks():
            yield piece
        self._body_done = True

    def read_body_bytes(self) -> bytes:
        """Convenience for a config-only package, whose body is a few hundred KiB.
        Callers handling a package that may carry a data section stream
        `open_body()` instead."""
        return b"".join(self.open_body())

    def read_secrets_bytes(self) -> Optional[bytes]:
        """The sealed-credentials lane, or None when the package has none.

        Bounded by construction (key/value pairs), so this one is materialized.
        """
        if not self._body_done:
            raise RuntimeError("the package body must be read before its credentials lane")
        declared = self.declared_secret_count
        flag = self._stream.read(1)
        if not flag or flag == b"\x00":
            # Cross-check against the header rather than shrugging: a package that
            # ADVERTISED credentials but carries no section was truncated, and
            # silently importing it without them would look like success (§2.6).
            if declared > 0:
                raise MalformedPackage(
                    "package is truncated: it declares sealed credentials but does not contain them"
                )
            return None
        if flag != b"\x01":
            raise MalformedPackage("package has a malformed credentials marker")
        reader = _SectionReader(self._stream, self._keys.secrets, self.header_bytes, self._chunk)
        return b"".join(reader.chunks())


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def new_nonce_prefix() -> bytes:
    return os.urandom(NONCE_PREFIX_LEN)


def constant_time_equals(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def package_filename(created_at: str) -> str:
    """`writ-export-YYYYMMDD-HHMM.writ` from an ISO-8601 timestamp."""
    digits = "".join(ch for ch in (created_at or "") if ch.isdigit())
    if len(digits) >= 12:
        return f"writ-export-{digits[:8]}-{digits[8:12]}.writ"
    return "writ-export.writ"


__all__ = [
    "AEAD_NAME",
    "BODY_CODEC",
    "CONTAINER_MAGIC",
    "DEFAULT_CHUNK_SIZE",
    "FORMAT_NAME",
    "MAX_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "PACKAGE_VERSION",
    "BadPassphrase",
    "MalformedPackage",
    "PackageError",
    "PackageKeys",
    "PackageReader",
    "PackageTooLarge",
    "PackageWriter",
    "UnsupportedVersion",
    "WeakKdf",
    "build_header",
    "canonical_header_bytes",
    "constant_time_equals",
    "derive_keys",
    "derive_keys_for_header",
    "header_summary",
    "new_nonce_prefix",
    "new_salt",
    "normalize_passphrase",
    "package_filename",
    "parse_header",
    "read_package_header",
    "validate_kdf",
]
