"""Key hierarchy primitives for SAITULS Secure Apps.

    FIDO2 credential
        -> hmac-secret output              (never persisted)
        -> HKDF-SHA256 + profile context   (domain separation)
        -> Key Encryption Key              (never persisted)
        -> AES-256-GCM unwrap              (AAD: profile, container, credential, schema)
        -> BitLocker volume unlock secret  (never persisted; 32 random bytes,
                                            used as a BitLocker external key)
        -> BitLocker encrypted VHDX

Only ciphertext and public metadata are written to disk. The raw hmac-secret
output, the derived KEK and the unwrapped volume secret exist as
:class:`SecretBuffer` instances and are zeroized when their scope ends.

Zeroization in CPython is best effort, and this module is explicit about the
two mechanisms it uses:

* every secret this code owns is a ``bytearray``, overwritten in place;
* where a library can only return an immutable ``bytes`` object (HKDF, the
  ECDH exchange, a hash digest), :func:`wipe_bytes` overwrites that object's
  storage in place before the last reference is dropped. It refuses the
  interpreter-shared empty and one-byte objects and is a no-op on any
  interpreter other than CPython.

Plaintext produced by AES-GCM is decrypted straight into a ``bytearray``
(``update_into``) and authenticated before it is returned; a failed tag check
wipes the buffer first.
"""
import ctypes
import hashlib
import hmac
import json
import os
import platform
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SCHEMA_VERSION = 1
KEK_BYTES = 32
SALT_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
VOLUME_SECRET_BYTES = 32
HKDF_INFO_PREFIX = b"saituls.secure-apps.kek.v1"

_CPYTHON = platform.python_implementation() == "CPython"
_BYTES_DATA_OFFSET = bytes.__basicsize__ - 1


class CryptoError(Exception):
    """Base for every failure in the key hierarchy."""

    category = "internal"


class UnwrapError(CryptoError):
    """The wrapped volume secret did not authenticate.

    Raised for a tampered ciphertext, a wrong profile AAD and a wrong key
    alike: distinguishing them for the caller would be an oracle.
    """

    category = "unwrap_failed"


def wipe(buffer):
    """Overwrite a bytearray (or writable memoryview) with zeros in place."""
    if buffer is None:
        return
    if isinstance(buffer, SecretBuffer):
        buffer.zeroize()
        return
    if isinstance(buffer, bytearray):
        for index in range(len(buffer)):
            buffer[index] = 0
        return
    if isinstance(buffer, memoryview) and not buffer.readonly:
        buffer[:] = b"\x00" * len(buffer)
        return
    if isinstance(buffer, bytes):
        wipe_bytes(buffer)


def wipe_bytes(value):
    """Best-effort in-place wipe of an immutable ``bytes`` object's storage.

    Only for objects this code created and is about to drop. Never call it on
    a literal: a constant folded into a code object would be corrupted.
    """
    if not _CPYTHON or not isinstance(value, bytes) or len(value) < 2:
        return
    ctypes.memset(id(value) + _BYTES_DATA_OFFSET, 0, len(value))


class SecretBuffer(object):
    """A mutable byte buffer that is zeroized on close.

    Use as a context manager wherever the lifetime is a block. :meth:`view`
    hands out the live ``bytearray`` for libraries that accept bytes-like
    input; :meth:`bytes` makes an immutable copy and is reserved for the few
    boundaries that cannot take anything else.
    """

    __slots__ = ("_buf", "_closed")

    def __init__(self, data=b""):
        self._buf = bytearray(data)
        self._closed = False

    @classmethod
    def random(cls, length):
        raw = secrets.token_bytes(length)
        try:
            return cls(raw)
        finally:
            wipe_bytes(raw)

    def __len__(self):
        return 0 if self._closed else len(self._buf)

    def __bool__(self):
        return len(self) > 0

    def bytes(self):
        if self._closed:
            raise CryptoError("secret buffer already zeroized")
        return bytes(self._buf)

    def view(self):
        if self._closed:
            raise CryptoError("secret buffer already zeroized")
        return self._buf

    def copy(self):
        return SecretBuffer(self.view())

    def equals(self, other):
        mine = self.view()
        theirs = other.view() if isinstance(other, SecretBuffer) else other
        return hmac.compare_digest(mine, theirs)

    @property
    def closed(self):
        return self._closed

    def zeroize(self):
        if not self._closed:
            for index in range(len(self._buf)):
                self._buf[index] = 0
            del self._buf[:]
            self._closed = True

    close = zeroize

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.zeroize()
        return False

    def __repr__(self):
        # Never leaks content -- this object shows up in tracebacks.
        return "<SecretBuffer %s len=%d>" % (
            "zeroized" if self._closed else "live",
            0 if self._closed else len(self._buf))

    __str__ = __repr__

    def __reduce__(self):
        raise CryptoError("a SecretBuffer cannot be pickled")


def _material(value):
    if isinstance(value, SecretBuffer):
        return value.view()
    if isinstance(value, (bytes, bytearray)):
        return value
    raise CryptoError("secret material must be bytes-like, got %s" % type(value).__name__)


def new_salt():
    return secrets.token_bytes(SALT_BYTES)


def new_volume_secret():
    """32 random bytes: the BitLocker external key the vault is unlocked with."""
    return SecretBuffer.random(VOLUME_SECRET_BYTES)


def build_aad(profile_id, container_id, credential_id, schema_version=SCHEMA_VERSION):
    """Canonical additional authenticated data for the wrapped volume secret.

    Binding all four values means a ciphertext lifted into another profile,
    pointed at another container or replayed against another credential fails
    to authenticate instead of unlocking the wrong thing.
    """
    if not profile_id or not container_id or not credential_id:
        raise CryptoError("AAD requires profile id, container id and credential id")
    payload = {
        "schema_version": int(schema_version),
        "profile_id": str(profile_id),
        "container_id": str(container_id),
        "credential_id": str(credential_id),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def derive_kek(hmac_secret_output, salt, profile_id, container_id):
    """HKDF-SHA256 over the authenticator output, with profile-scoped context."""
    ikm = _material(hmac_secret_output)
    if len(ikm) < 32:
        raise CryptoError("hmac-secret output too short: %d bytes" % len(ikm))
    if len(salt) < 16:
        raise CryptoError("salt too short: %d bytes" % len(salt))
    info = b"|".join([
        HKDF_INFO_PREFIX,
        str(profile_id).encode("utf-8"),
        str(container_id).encode("utf-8"),
    ])
    derived = HKDF(algorithm=hashes.SHA256(), length=KEK_BYTES, salt=bytes(salt),
                   info=info).derive(ikm)
    try:
        return SecretBuffer(derived)
    finally:
        wipe_bytes(derived)


def wrap_secret(kek, plaintext_secret, aad):
    """AES-256-GCM wrap. Returns ``(nonce, ciphertext_with_tag)`` as bytes."""
    key = _material(kek)
    if len(key) != KEK_BYTES:
        raise CryptoError("KEK must be %d bytes" % KEK_BYTES)
    data = _material(plaintext_secret)
    nonce = secrets.token_bytes(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)
    ciphertext = encryptor.update(data) + encryptor.finalize()
    return nonce, ciphertext + encryptor.tag


def unwrap_secret(kek, nonce, ciphertext, aad):
    """AES-256-GCM unwrap into a :class:`SecretBuffer`. Raises UnwrapError."""
    key = _material(kek)
    if len(key) != KEK_BYTES:
        raise CryptoError("KEK must be %d bytes" % KEK_BYTES)
    ciphertext = bytes(ciphertext)
    nonce = bytes(nonce)
    if len(nonce) != NONCE_BYTES or len(ciphertext) <= TAG_BYTES:
        raise UnwrapError("wrapped volume secret failed authentication")
    body, tag = ciphertext[:-TAG_BYTES], ciphertext[-TAG_BYTES:]
    buffer = bytearray(len(body) + 16)
    try:
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(aad)
        written = decryptor.update_into(body, buffer)
        decryptor.finalize()
    except (InvalidTag, ValueError):
        wipe(buffer)
        # Deliberately opaque: tampering, wrong AAD and wrong key look alike.
        raise UnwrapError("wrapped volume secret failed authentication")
    try:
        return SecretBuffer(buffer[:written])
    finally:
        wipe(buffer)


def credential_id_hash(credential_id):
    """Stable non-reversible identifier for audit records.

    A raw credential id is public per WebAuthn, but it is still a per-user
    correlator. Logs carry this instead.
    """
    raw = (bytes(credential_id) if isinstance(credential_id, (bytes, bytearray))
           else str(credential_id).encode("utf-8"))
    return hashlib.sha256(b"saituls.secure-apps.credid|" + raw).hexdigest()[:32]


def constant_time_equals(a, b):
    return hmac.compare_digest(bytes(a), bytes(b))


def fingerprint(value):
    """Short non-reversible fingerprint used to prove two secrets match.

    Used by tests; never used as a key and never logged.
    """
    raw = _material(value)
    return hashlib.sha256(b"saituls.secure-apps.fp|" + bytes(raw)).hexdigest()[:16]


def random_container_id():
    return os.urandom(16).hex()
