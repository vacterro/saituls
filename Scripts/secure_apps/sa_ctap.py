"""CTAP2 client for SAITULS Secure Apps.

Implements exactly what the ``yubikey-fido2-hmac-secret`` provider needs and
nothing more:

* CTAPHID framing (INIT, CBOR, CANCEL, KEEPALIVE, ERROR) over any connection
  object with ``packet_size``, ``write_packet`` and ``read_packet``;
* ``authenticatorGetInfo``, ``authenticatorMakeCredential``,
  ``authenticatorGetAssertion`` and ``authenticatorClientPIN``;
* PIN/UV auth protocols 1 and 2 (CTAP 2.1 section 6.5);
* the ``hmac-secret`` extension, input encryption and output decryption.

Key material produced here (the PIN/UV shared secret, the PIN token and the
decrypted hmac-secret output) lives in ``bytearray`` buffers that are wiped
before they go out of scope. The one value that leaves this module is the
32-byte hmac-secret output, wrapped in :class:`sa_crypto.SecretBuffer`.

The salt handed to the authenticator is the WebAuthn PRF evaluation of the
enrolled input (``SHA-256("WebAuthn PRF" || 0x00 || input)``), so a future
provider that talks to the same credential through a platform PRF API derives
the same output without re-enrolling.
"""
import hashlib
import hmac
import os
import struct
import threading
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import sa_cbor
import sa_crypto

# ----------------------------------------------------------------- CTAPHID
CTAPHID_PING = 0x01
CTAPHID_MSG = 0x03
CTAPHID_INIT = 0x06
CTAPHID_WINK = 0x08
CTAPHID_CBOR = 0x10
CTAPHID_CANCEL = 0x11
CTAPHID_KEEPALIVE = 0x3B
CTAPHID_ERROR = 0x3F
TYPE_INIT = 0x80
BROADCAST_CID = 0xFFFFFFFF

CAPABILITY_WINK = 0x01
CAPABILITY_CBOR = 0x04
CAPABILITY_NMSG = 0x08

KEEPALIVE_PROCESSING = 1
KEEPALIVE_UPNEEDED = 2

# ---------------------------------------------------------------- commands
CMD_MAKE_CREDENTIAL = 0x01
CMD_GET_ASSERTION = 0x02
CMD_GET_INFO = 0x04
CMD_CLIENT_PIN = 0x06

PIN_GET_RETRIES = 0x01
PIN_GET_KEY_AGREEMENT = 0x02
PIN_GET_TOKEN = 0x05
PIN_GET_TOKEN_USING_PIN_WITH_PERMISSIONS = 0x09

PERMISSION_MAKE_CREDENTIAL = 0x01
PERMISSION_GET_ASSERTION = 0x02

ALG_ES256 = -7
ALG_EDDSA = -8
ALG_ECDH_ES_HKDF_256 = -25

# ------------------------------------------------------------ status codes
ERR_SUCCESS = 0x00
ERR_INVALID_COMMAND = 0x01
ERR_INVALID_PARAMETER = 0x02
ERR_INVALID_LENGTH = 0x03
ERR_INVALID_SEQ = 0x04
ERR_TIMEOUT = 0x05
ERR_CHANNEL_BUSY = 0x06
ERR_INVALID_CHANNEL = 0x0B
ERR_CBOR_UNEXPECTED_TYPE = 0x11
ERR_INVALID_CBOR = 0x12
ERR_MISSING_PARAMETER = 0x14
ERR_UNSUPPORTED_EXTENSION = 0x16
ERR_CREDENTIAL_EXCLUDED = 0x19
ERR_PROCESSING = 0x21
ERR_INVALID_CREDENTIAL = 0x22
ERR_USER_ACTION_PENDING = 0x23
ERR_UNSUPPORTED_ALGORITHM = 0x26
ERR_OPERATION_DENIED = 0x27
ERR_KEY_STORE_FULL = 0x28
ERR_UNSUPPORTED_OPTION = 0x2B
ERR_INVALID_OPTION = 0x2C
ERR_KEEPALIVE_CANCEL = 0x2D
ERR_NO_CREDENTIALS = 0x2E
ERR_USER_ACTION_TIMEOUT = 0x2F
ERR_NOT_ALLOWED = 0x30
ERR_PIN_INVALID = 0x31
ERR_PIN_BLOCKED = 0x32
ERR_PIN_AUTH_INVALID = 0x33
ERR_PIN_AUTH_BLOCKED = 0x34
ERR_PIN_NOT_SET = 0x35
ERR_PUAT_REQUIRED = 0x36
ERR_PIN_POLICY_VIOLATION = 0x37
ERR_REQUEST_TOO_LARGE = 0x39
ERR_ACTION_TIMEOUT = 0x3A
ERR_UP_REQUIRED = 0x3B
ERR_UV_BLOCKED = 0x3C
ERR_UV_INVALID = 0x3F
ERR_UNAUTHORIZED_PERMISSION = 0x40
ERR_OTHER = 0x7F

STATUS_NAMES = {value: name for name, value in globals().items()
                if name.startswith("ERR_") and isinstance(value, int)}

CANCELLED_CODES = (ERR_KEEPALIVE_CANCEL, ERR_OPERATION_DENIED, ERR_USER_ACTION_TIMEOUT,
                   ERR_ACTION_TIMEOUT)
PIN_RETRY_CODES = (ERR_PIN_INVALID,)
PIN_FATAL_CODES = (ERR_PIN_BLOCKED, ERR_PIN_AUTH_BLOCKED, ERR_UV_BLOCKED)

PRF_LABEL = b"WebAuthn PRF\x00"


class CtapError(Exception):
    """The authenticator answered with a non-success CTAP status."""

    def __init__(self, code):
        self.code = int(code)
        Exception.__init__(self, "CTAP status %s"
                           % STATUS_NAMES.get(self.code, "0x%02X" % self.code))


class CtapTransportError(Exception):
    """CTAPHID framing failed, the device vanished or an I/O wait timed out."""

    def __init__(self, message, code=None):
        Exception.__init__(self, message)
        self.code = code


class CtapCancelled(Exception):
    """The caller cancelled before the authenticator answered."""


def prf_salt(prf_input):
    """hmac-secret salt for a WebAuthn PRF input."""
    return hashlib.sha256(PRF_LABEL + bytes(prf_input)).digest()


# =========================================================== CTAPHID device
class CtapHidDevice(object):
    """One CTAPHID channel on one connection."""

    def __init__(self, connection, descriptor=None, packet_timeout=1.0):
        self.connection = connection
        self.descriptor = descriptor
        self.packet_timeout = packet_timeout
        self.packet_size = int(getattr(connection, "packet_size", 64))
        self._lock = threading.RLock()
        self.cid = BROADCAST_CID
        self.capabilities = 0
        self.ctaphid_version = 0
        self.device_version = (0, 0, 0)
        self._init_channel()

    # -- framing --------------------------------------------------------
    def _write(self, cid, command, payload):
        size = self.packet_size
        payload = bytes(payload)
        if len(payload) > (size - 7) + 0x80 * (size - 5):
            raise CtapTransportError("request too large for CTAPHID")
        first = payload[:size - 7]
        packet = struct.pack(">IBH", cid, TYPE_INIT | command, len(payload)) + first
        self.connection.write_packet(packet.ljust(size, b"\x00"))
        offset = len(first)
        sequence = 0
        while offset < len(payload):
            chunk = payload[offset:offset + size - 5]
            packet = struct.pack(">IB", cid, sequence) + chunk
            self.connection.write_packet(packet.ljust(size, b"\x00"))
            offset += len(chunk)
            sequence += 1

    def _read_packet(self, timeout):
        try:
            return self.connection.read_packet(timeout=timeout)
        except Exception as exc:
            if type(exc).__name__ == "HidTimeout":
                return None
            raise CtapTransportError("the authenticator stopped responding (%s)"
                                     % type(exc).__name__)

    def _send_cancel(self, cid):
        packet = struct.pack(">IBH", cid, TYPE_INIT | CTAPHID_CANCEL, 0)
        try:
            self.connection.write_packet(packet.ljust(self.packet_size, b"\x00"))
        except Exception:
            pass

    def _receive(self, cid, command, on_keepalive=None, cancel=None, timeout=None):
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        cancel_sent = False
        while True:
            if cancel is not None and cancel.is_set() and not cancel_sent:
                self._send_cancel(cid)
                cancel_sent = True
            if deadline is not None and time.monotonic() > deadline:
                self._send_cancel(cid)
                raise CtapTransportError("the authenticator did not answer in time")
            packet = self._read_packet(self.packet_timeout)
            if packet is None:
                continue
            packet = bytes(packet)
            rcid, rcmd = struct.unpack(">IB", packet[:5])
            if rcid != cid or not rcmd & TYPE_INIT:
                continue
            rcmd &= 0x7F
            length = struct.unpack(">H", packet[5:7])[0]
            if rcmd == CTAPHID_KEEPALIVE:
                if on_keepalive is not None:
                    try:
                        on_keepalive(packet[7])
                    except Exception:
                        pass
                continue
            if rcmd == CTAPHID_ERROR:
                raise CtapTransportError("CTAPHID error 0x%02X" % packet[7], packet[7])
            if rcmd != command:
                raise CtapTransportError("unexpected CTAPHID response 0x%02X" % rcmd)
            data = bytearray(packet[7:7 + min(length, self.packet_size - 7)])
            sequence = 0
            while len(data) < length:
                packet = self._read_packet(self.packet_timeout * 3)
                if packet is None:
                    raise CtapTransportError("CTAPHID continuation timed out")
                packet = bytes(packet)
                rcid, rseq = struct.unpack(">IB", packet[:5])
                if rcid != cid:
                    continue
                if rseq != sequence:
                    raise CtapTransportError("CTAPHID sequence error")
                data.extend(packet[5:5 + min(length - len(data), self.packet_size - 5)])
                sequence += 1
            return bytes(data)

    def _init_channel(self):
        nonce = os.urandom(8)
        with self._lock:
            self._write(BROADCAST_CID, CTAPHID_INIT, nonce)
            deadline = time.monotonic() + 5.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CtapTransportError("CTAPHID INIT timed out")
                response = self._receive(BROADCAST_CID, CTAPHID_INIT, timeout=remaining)
                if len(response) >= 17 and response[:8] == nonce:
                    break
        self.cid = struct.unpack(">I", response[8:12])[0]
        self.ctaphid_version = response[12]
        self.device_version = (response[13], response[14], response[15])
        self.capabilities = response[16]

    def call(self, command, payload, on_keepalive=None, cancel=None, timeout=None):
        with self._lock:
            self._write(self.cid, command, payload)
            return self._receive(self.cid, command, on_keepalive, cancel, timeout)

    def close(self):
        try:
            self.connection.close()
        except Exception:
            pass


# ================================================================== CTAP2
class Info(object):
    """authenticatorGetInfo, reduced to public fields."""

    def __init__(self, raw):
        self.versions = list(raw.get(1) or [])
        self.extensions = list(raw.get(2) or [])
        aaguid = raw.get(3) or b""
        self.aaguid = bytes(aaguid).hex()
        self.options = dict(raw.get(4) or {})
        self.max_msg_size = int(raw.get(5) or 1024)
        self.pin_uv_protocols = [int(v) for v in (raw.get(6) or [])]
        self.max_credential_count_in_list = raw.get(7)
        self.max_credential_id_length = raw.get(8)
        self.firmware_version = raw.get(14)

    @property
    def hmac_secret(self):
        return "hmac-secret" in self.extensions

    @property
    def client_pin_set(self):
        return self.options.get("clientPin") is True

    def to_dict(self):
        return {
            "versions": self.versions,
            "extensions": self.extensions,
            "aaguid": self.aaguid,
            "options": self.options,
            "pin_uv_protocols": self.pin_uv_protocols,
        }


class AuthenticatorData(object):
    FLAG_UP = 0x01
    FLAG_UV = 0x04
    FLAG_AT = 0x40
    FLAG_ED = 0x80

    def __init__(self, raw):
        raw = bytes(raw)
        if len(raw) < 37:
            raise CtapTransportError("authenticator data too short")
        self.rp_id_hash = raw[:32]
        self.flags = raw[32]
        self.counter = struct.unpack(">I", raw[33:37])[0]
        offset = 37
        self.aaguid = None
        self.credential_id = None
        self.public_key = None
        if self.flags & self.FLAG_AT:
            if len(raw) < offset + 18:
                raise CtapTransportError("attested credential data truncated")
            self.aaguid = raw[offset:offset + 16]
            offset += 16
            (length,) = struct.unpack(">H", raw[offset:offset + 2])
            offset += 2
            self.credential_id = raw[offset:offset + length]
            if len(self.credential_id) != length:
                raise CtapTransportError("credential id truncated")
            offset += length
            self.public_key, offset = sa_cbor.decode_from(raw, offset)
        self.extensions = {}
        if self.flags & self.FLAG_ED:
            self.extensions, offset = sa_cbor.decode_from(raw, offset)
            if not isinstance(self.extensions, dict):
                raise CtapTransportError("extension data is not a map")
        if offset != len(raw):
            raise CtapTransportError("trailing bytes in authenticator data")

    @property
    def user_present(self):
        return bool(self.flags & self.FLAG_UP)

    @property
    def user_verified(self):
        return bool(self.flags & self.FLAG_UV)


class Ctap2(object):
    def __init__(self, device):
        self.device = device
        self.info = self.get_info()

    def send(self, command, arguments=None, on_keepalive=None, cancel=None, timeout=None):
        request = bytes([command])
        if arguments:
            request += sa_cbor.encode({k: v for k, v in arguments.items() if v is not None})
        response = self.device.call(CTAPHID_CBOR, request, on_keepalive, cancel, timeout)
        if not response:
            raise CtapTransportError("empty CTAP2 response")
        if response[0] != ERR_SUCCESS:
            raise CtapError(response[0])
        body = response[1:]
        if not body:
            return {}
        decoded = sa_cbor.decode(body)
        if not isinstance(decoded, dict):
            raise CtapTransportError("CTAP2 response is not a map")
        return decoded

    def get_info(self):
        return Info(self.send(CMD_GET_INFO, timeout=10))


# ======================================================= PIN/UV protocols
def _cose_from_public(public_key):
    numbers = public_key.public_numbers()
    return {1: 2, 3: ALG_ECDH_ES_HKDF_256, -1: 1,
            -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}


def _public_from_cose(cose):
    try:
        x = int.from_bytes(bytes(cose[-2]), "big")
        y = int.from_bytes(bytes(cose[-3]), "big")
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except (KeyError, TypeError, ValueError):
        raise CtapTransportError("authenticator key agreement key is malformed")


class PinUvAuthProtocolV1(object):
    VERSION = 1

    def encapsulate(self, peer_cose):
        """ECDH with the authenticator. Returns (platform COSE key, shared secret)."""
        private_key = ec.generate_private_key(ec.SECP256R1())
        raw = private_key.exchange(ec.ECDH(), _public_from_cose(peer_cose))
        z = bytearray(raw)
        sa_crypto.wipe_bytes(raw)
        try:
            return _cose_from_public(private_key.public_key()), self.kdf(z)
        finally:
            sa_crypto.wipe(z)

    def kdf(self, z):
        digest = hashlib.sha256(z).digest()
        out = bytearray(digest)
        sa_crypto.wipe_bytes(digest)
        return out

    def _aes_key(self, key):
        return bytes(memoryview(key)[:32])

    def encrypt(self, key, plaintext):
        aes_key = self._aes_key(key)
        try:
            encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(b"\x00" * 16)).encryptor()
            return encryptor.update(plaintext) + encryptor.finalize()
        finally:
            sa_crypto.wipe_bytes(aes_key)

    def decrypt(self, key, ciphertext):
        return self._cbc_decrypt(self._aes_key(key), b"\x00" * 16, bytes(ciphertext))

    @staticmethod
    def _cbc_decrypt(aes_key, iv, ciphertext):
        if len(ciphertext) % 16:
            raise CtapTransportError("encrypted CTAP value has an invalid length")
        buffer = bytearray(len(ciphertext) + 16)
        try:
            decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
            written = decryptor.update_into(ciphertext, buffer)
            decryptor.finalize()
            return bytearray(buffer[:written])
        finally:
            sa_crypto.wipe(buffer)
            sa_crypto.wipe_bytes(aes_key)

    def authenticate(self, key, message):
        return hmac.new(key, bytes(message), hashlib.sha256).digest()[:16]


class PinUvAuthProtocolV2(PinUvAuthProtocolV1):
    VERSION = 2

    def kdf(self, z):
        out = bytearray()
        for info in (b"CTAP2 HMAC key", b"CTAP2 AES key"):
            derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"\x00" * 32,
                           info=info).derive(z)
            out.extend(derived)
            sa_crypto.wipe_bytes(derived)
        return out

    def _aes_key(self, key):
        return bytes(memoryview(key)[32:64])

    def encrypt(self, key, plaintext):
        iv = os.urandom(16)
        aes_key = self._aes_key(key)
        try:
            encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
            return iv + encryptor.update(plaintext) + encryptor.finalize()
        finally:
            sa_crypto.wipe_bytes(aes_key)

    def decrypt(self, key, ciphertext):
        ciphertext = bytes(ciphertext)
        if len(ciphertext) < 32:
            raise CtapTransportError("encrypted CTAP value is too short")
        return self._cbc_decrypt(self._aes_key(key), ciphertext[:16], ciphertext[16:])

    def authenticate(self, key, message):
        hmac_key = bytearray(memoryview(key)[:32])
        try:
            return hmac.new(hmac_key, bytes(message), hashlib.sha256).digest()
        finally:
            sa_crypto.wipe(hmac_key)


PROTOCOLS = {1: PinUvAuthProtocolV1, 2: PinUvAuthProtocolV2}


class ClientPin(object):
    def __init__(self, ctap2, version=None):
        self.ctap2 = ctap2
        supported = ctap2.info.pin_uv_protocols or [1]
        if version is None:
            version = 2 if 2 in supported else 1
        if version not in supported or version not in PROTOCOLS:
            raise CtapTransportError("PIN/UV auth protocol %s is not supported" % version)
        self.protocol = PROTOCOLS[version]()

    def shared_secret(self):
        response = self.ctap2.send(CMD_CLIENT_PIN, {1: self.protocol.VERSION,
                                                    2: PIN_GET_KEY_AGREEMENT}, timeout=10)
        if 1 not in response:
            raise CtapTransportError("authenticator returned no key agreement key")
        return self.protocol.encapsulate(response[1])

    def retries(self):
        response = self.ctap2.send(CMD_CLIENT_PIN, {1: self.protocol.VERSION,
                                                    2: PIN_GET_RETRIES}, timeout=10)
        return response.get(3)

    def pin_token(self, pin, permissions, rp_id):
        """Exchange a PIN (bytes-like, UTF-8) for a pinUvAuthToken bytearray."""
        platform_key, shared = self.shared_secret()
        digest = hashlib.sha256(pin).digest()
        try:
            pin_hash = memoryview(digest)[:16]
            pin_hash_enc = self.protocol.encrypt(shared, pin_hash)
            if self.ctap2.info.options.get("pinUvAuthToken"):
                arguments = {1: self.protocol.VERSION,
                             2: PIN_GET_TOKEN_USING_PIN_WITH_PERMISSIONS,
                             3: platform_key, 6: pin_hash_enc,
                             9: permissions, 10: rp_id}
            else:
                arguments = {1: self.protocol.VERSION, 2: PIN_GET_TOKEN,
                             3: platform_key, 6: pin_hash_enc}
            response = self.ctap2.send(CMD_CLIENT_PIN, arguments, timeout=15)
            if 2 not in response:
                raise CtapTransportError("authenticator returned no PIN token")
            return self.protocol.decrypt(shared, response[2])
        finally:
            sa_crypto.wipe_bytes(digest)
            sa_crypto.wipe(shared)


# ============================================================ high level
class PinRequest(object):
    """What a PIN prompt is told. Carries no secret."""

    __slots__ = ("attempt", "retries", "reason")

    def __init__(self, attempt, retries=None, reason="unlock"):
        self.attempt = attempt
        self.retries = retries
        self.reason = reason


class PinSession(object):
    """A pinUvAuthToken obtained once and reused for one enrollment or unlock.

    One PIN entry covers makeCredential and the hmac-secret assertion that
    follows it. :meth:`close` wipes the token; the object is also a context
    manager.
    """

    def __init__(self, client_pin, token, permissions, rp_id):
        self.client_pin = client_pin
        self._token = token
        self.permissions = permissions
        self.rp_id = rp_id

    @property
    def protocol(self):
        return self.client_pin.protocol

    def param(self, client_data_hash):
        if self._token is None:
            raise CtapTransportError("PIN session already closed")
        return self.protocol.authenticate(self._token, client_data_hash)

    def close(self):
        if self._token is not None:
            sa_crypto.wipe(self._token)
            self._token = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Authenticator(object):
    """The operations Secure Apps performs on one connected authenticator."""

    MAX_PIN_ATTEMPTS = 3

    def __init__(self, device):
        self.device = device
        self.ctap2 = Ctap2(device)

    @property
    def info(self):
        return self.ctap2.info

    def close(self):
        self.device.close()

    # -- PIN ---------------------------------------------------------------
    def pin_session(self, pin_provider, permissions, rp_id, reason, cancel=None):
        """Ask for the PIN (up to three attempts) and return a :class:`PinSession`."""
        if not self.info.client_pin_set:
            raise CtapError(ERR_PIN_NOT_SET)
        client_pin = ClientPin(self.ctap2)
        retries = None
        try:
            retries = client_pin.retries()
        except CtapError:
            retries = None
        for attempt in range(1, self.MAX_PIN_ATTEMPTS + 1):
            if cancel is not None and cancel.is_set():
                raise CtapCancelled("cancelled before the PIN was entered")
            if pin_provider is None:
                raise CtapError(ERR_PUAT_REQUIRED)
            pin = pin_provider(PinRequest(attempt, retries, reason))
            if pin is None:
                raise CtapCancelled("PIN entry was cancelled")
            try:
                token = client_pin.pin_token(pin, permissions, rp_id)
                return PinSession(client_pin, token, permissions, rp_id)
            except CtapError as exc:
                if exc.code not in PIN_RETRY_CODES or attempt == self.MAX_PIN_ATTEMPTS:
                    raise
                try:
                    retries = client_pin.retries()
                except CtapError:
                    retries = None
            finally:
                sa_crypto.wipe(pin)
        raise CtapError(ERR_PIN_INVALID)

    def make_credential_needs_pin(self):
        return self.info.client_pin_set and not self.info.options.get("makeCredUvNotRqd")

    # -- makeCredential -----------------------------------------------------
    def make_hmac_secret_credential(self, rp_id, rp_name, user_id, user_name,
                                    display_name, exclude_ids=(), pin_session=None,
                                    on_keepalive=None, cancel=None, timeout=90):
        if not self.info.hmac_secret:
            raise CtapError(ERR_UNSUPPORTED_EXTENSION)
        client_data_hash = os.urandom(32)
        pin_uv_param = None
        protocol_version = None
        if pin_session is not None:
            pin_uv_param = pin_session.param(client_data_hash)
            protocol_version = pin_session.protocol.VERSION
        elif self.make_credential_needs_pin():
            raise CtapError(ERR_PUAT_REQUIRED)
        arguments = {
            1: client_data_hash,
            2: {"id": rp_id, "name": rp_name},
            3: {"id": bytes(user_id), "name": user_name, "displayName": display_name},
            4: [{"alg": ALG_ES256, "type": "public-key"},
                {"alg": ALG_EDDSA, "type": "public-key"}],
            5: [{"id": bytes(c), "type": "public-key"} for c in exclude_ids] or None,
            6: {"hmac-secret": True},
            7: {"rk": False},
            8: pin_uv_param,
            9: protocol_version,
        }
        response = self.ctap2.send(CMD_MAKE_CREDENTIAL, arguments, on_keepalive,
                                   cancel, timeout)
        if 2 not in response:
            raise CtapTransportError("makeCredential returned no authenticator data")
        auth_data = AuthenticatorData(response[2])
        if not auth_data.credential_id:
            raise CtapTransportError("makeCredential returned no credential")
        return {
            "credential_id": bytes(auth_data.credential_id),
            "aaguid": bytes(auth_data.aaguid or b"").hex(),
            "hmac_secret": auth_data.extensions.get("hmac-secret") is True,
        }

    # -- getAssertion ---------------------------------------------------------
    def has_credential(self, rp_id, credential_ids, timeout=10):
        """Silent (up=false) probe: does this authenticator hold any of them?"""
        arguments = {
            1: rp_id,
            2: os.urandom(32),
            3: [{"id": bytes(c), "type": "public-key"} for c in credential_ids],
            5: {"up": False},
        }
        try:
            self.ctap2.send(CMD_GET_ASSERTION, arguments, timeout=timeout)
            return True
        except CtapError as exc:
            if exc.code in (ERR_NO_CREDENTIALS, ERR_INVALID_CREDENTIAL):
                return False
            raise

    def hmac_secret(self, rp_id, credential_ids, prf_input, pin_session=None,
                    on_keepalive=None, cancel=None, timeout=90):
        """One assertion with hmac-secret. Returns ``(credential_id, SecretBuffer)``.

        With *pin_session* the assertion is user-verified, which selects the
        authenticator's UV-bound hmac-secret key; without it only presence is
        proven. The caller must use the same choice the credential was
        enrolled with, or the output (and therefore the KEK) differs.
        """
        if not self.info.hmac_secret:
            raise CtapError(ERR_UNSUPPORTED_EXTENSION)
        client_pin = pin_session.client_pin if pin_session is not None else ClientPin(self.ctap2)
        protocol = client_pin.protocol
        client_data_hash = os.urandom(32)
        shared = None
        salt = prf_salt(prf_input)
        try:
            pin_uv_param = None
            if pin_session is not None:
                pin_uv_param = pin_session.param(client_data_hash)
            platform_key, shared = client_pin.shared_secret()
            salt_enc = protocol.encrypt(shared, salt)
            salt_auth = protocol.authenticate(shared, salt_enc)
            extension_input = {1: platform_key, 2: salt_enc, 3: salt_auth}
            if protocol.VERSION != 1:
                extension_input[4] = protocol.VERSION
            arguments = {
                1: rp_id,
                2: client_data_hash,
                3: [{"id": bytes(c), "type": "public-key"} for c in credential_ids],
                4: {"hmac-secret": extension_input},
                6: pin_uv_param,
                7: protocol.VERSION if pin_uv_param is not None else None,
            }
            response = self.ctap2.send(CMD_GET_ASSERTION, arguments, on_keepalive,
                                       cancel, timeout)
            if 2 not in response:
                raise CtapTransportError("getAssertion returned no authenticator data")
            auth_data = AuthenticatorData(response[2])
            if pin_session is not None and not auth_data.user_verified:
                raise CtapTransportError("the authenticator did not verify the user")
            encrypted = auth_data.extensions.get("hmac-secret")
            if not isinstance(encrypted, bytes):
                raise CtapError(ERR_UNSUPPORTED_EXTENSION)
            output = protocol.decrypt(shared, encrypted)
            try:
                if len(output) < 32:
                    raise CtapTransportError("hmac-secret output too short")
                material = sa_crypto.SecretBuffer(memoryview(output)[:32])
            finally:
                sa_crypto.wipe(output)
            descriptor = response.get(1) or {}
            used = descriptor.get("id") if isinstance(descriptor, dict) else None
            if used is None:
                if len(credential_ids) != 1:
                    material.zeroize()
                    raise CtapTransportError(
                        "authenticator did not say which credential answered")
                used = credential_ids[0]
            return bytes(used), material
        finally:
            sa_crypto.wipe_bytes(salt)
            if shared is not None:
                sa_crypto.wipe(shared)


def open_authenticators(list_devices=None, connect=None):
    """Open every connected FIDO HID authenticator. Caller closes them.

    Returns ``(authenticators, enumeration, error_names)``; *enumeration* is
    the transport's :class:`sa_hid.EnumerationResult` so a caller can tell
    "no key plugged in" apart from "keys present but access denied".
    """
    if list_devices is None or connect is None:
        import sa_hid
        list_devices = list_devices or sa_hid.list_devices
        connect = connect or sa_hid.WindowsHidConnection
    enumeration = list_devices()
    opened = []
    errors = []
    for descriptor in enumeration.devices:
        try:
            device = CtapHidDevice(connect(descriptor), descriptor)
            opened.append(Authenticator(device))
        except Exception as exc:
            errors.append(type(exc).__name__)
    return opened, enumeration, errors
