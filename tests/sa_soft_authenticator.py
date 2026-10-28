"""Software CTAP2 authenticator for the Secure Apps hermetic tests.

A deterministic stand-in for a YubiKey that speaks real CTAPHID framing and
real CTAP2 CBOR, with real cryptography: ECDH key agreement, PIN/UV auth
protocols 1 and 2, pinUvAuthTokens, and the hmac-secret extension with
separate UV and non-UV credential secrets.

It is written from the CTAP 2.1 specification text independently of
``Scripts/secure_apps/sa_ctap.py`` -- it deliberately does not import the
client's protocol classes -- so a symmetric mistake in the client (wrong key
half, wrong IV handling, wrong truncation) shows up as a failure here instead
of cancelling itself out.

Test-only. Never imported by production code.
"""
import collections
import hashlib
import hmac
import os
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import sa_cbor

CTAPHID_INIT = 0x06
CTAPHID_CBOR = 0x10
CTAPHID_CANCEL = 0x11
CTAPHID_KEEPALIVE = 0x3B
CTAPHID_ERROR = 0x3F
BROADCAST = 0xFFFFFFFF


class HidTimeout(Exception):
    """Same class name the real transport raises; the client matches on it."""


class Refusal(Exception):
    def __init__(self, code):
        Exception.__init__(self, "status 0x%02x" % code)
        self.code = code


def _cose(public_key):
    n = public_key.public_numbers()
    return {1: 2, 3: -25, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}


def _public(cose):
    return ec.EllipticCurvePublicNumbers(
        int.from_bytes(cose[-2], "big"), int.from_bytes(cose[-3], "big"),
        ec.SECP256R1()).public_key()


# -- the two PIN/UV auth protocols, authenticator side ---------------------
def _p1_kdf(z):
    return hashlib.sha256(z).digest()


def _p2_kdf(z):
    def hkdf(info):
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=bytes(32), info=info).derive(z)
    return hkdf(b"CTAP2 HMAC key") + hkdf(b"CTAP2 AES key")


def _cbc(key, iv, data, decrypt):
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    ctx = cipher.decryptor() if decrypt else cipher.encryptor()
    return ctx.update(data) + ctx.finalize()


def _encrypt(version, shared, plaintext):
    if version == 1:
        return _cbc(shared, bytes(16), plaintext, False)
    iv = os.urandom(16)
    return iv + _cbc(shared[32:], iv, plaintext, False)


def _decrypt(version, shared, ciphertext):
    if version == 1:
        return _cbc(shared, bytes(16), ciphertext, True)
    if len(ciphertext) < 16:
        raise Refusal(0x02)
    return _cbc(shared[32:], ciphertext[:16], ciphertext[16:], True)


def _authenticate(version, key, message):
    if version == 1:
        return hmac.new(key, message, hashlib.sha256).digest()[:16]
    return hmac.new(key[:32], message, hashlib.sha256).digest()


class SoftAuthenticator(object):
    """CTAP2 state machine. Behaviour knobs are plain attributes."""

    ERR_INVALID_PARAMETER = 0x02
    ERR_UNSUPPORTED_ALGORITHM = 0x26
    ERR_OPERATION_DENIED = 0x27
    ERR_CREDENTIAL_EXCLUDED = 0x19
    ERR_KEEPALIVE_CANCEL = 0x2D
    ERR_NO_CREDENTIALS = 0x2E
    ERR_USER_ACTION_TIMEOUT = 0x2F
    ERR_PIN_INVALID = 0x31
    ERR_PIN_BLOCKED = 0x32
    ERR_PIN_AUTH_INVALID = 0x33
    ERR_PIN_NOT_SET = 0x35
    ERR_PUAT_REQUIRED = 0x36

    def __init__(self, pin=None, hmac_secret=True, pin_protocols=(2, 1),
                 versions=("FIDO_2_0", "FIDO_2_1"), make_cred_uv_not_rqd=True,
                 permissions_tokens=True, keepalives=2):
        self.pin = pin
        self.hmac_secret_supported = hmac_secret
        self.pin_protocols = list(pin_protocols)
        self.versions = list(versions)
        self.make_cred_uv_not_rqd = make_cred_uv_not_rqd
        self.permissions_tokens = permissions_tokens
        self.keepalives = keepalives
        self.aaguid = os.urandom(16)
        self.retries = 8
        self.credentials = collections.OrderedDict()
        self.presence = "touch"        # touch | deny | timeout | wait_for_cancel
        self.commands = []
        self.pin_token_requests = 0
        self._agreement = ec.generate_private_key(ec.SECP256R1())
        self._token = None
        self._token_version = None

    # -- helpers ------------------------------------------------------------
    def _shared(self, version, platform_cose):
        z = self._agreement.exchange(ec.ECDH(), _public(platform_cose))
        return _p1_kdf(z) if version == 1 else _p2_kdf(z)

    def _check_param(self, cdh, param, version):
        if self._token is None or version != self._token_version:
            raise Refusal(self.ERR_PIN_AUTH_INVALID)
        if not hmac.compare_digest(_authenticate(version, self._token, cdh), bytes(param)):
            raise Refusal(self.ERR_PIN_AUTH_INVALID)

    def info(self):
        options = {"rk": True, "up": True, "clientPin": self.pin is not None}
        if self.make_cred_uv_not_rqd:
            options["makeCredUvNotRqd"] = True
        if self.permissions_tokens:
            options["pinUvAuthToken"] = True
        extensions = ["credProtect"] + (["hmac-secret"] if self.hmac_secret_supported else [])
        return {1: self.versions, 2: extensions, 3: self.aaguid, 4: options,
                5: 1200, 6: self.pin_protocols}

    # -- dispatch --------------------------------------------------------------
    def handle(self, request, presence_hook):
        command = request[0]
        args = sa_cbor.decode(request[1:]) if len(request) > 1 else {}
        self.commands.append(command)
        try:
            if command == 0x04:
                body = self.info()
            elif command == 0x06:
                body = self.client_pin(args)
            elif command == 0x01:
                body = self.make_credential(args, presence_hook)
            elif command == 0x02:
                body = self.get_assertion(args, presence_hook)
            else:
                return bytes([0x01])
        except Refusal as refusal:
            return bytes([refusal.code])
        return b"\x00" + (sa_cbor.encode(body) if body else b"")

    def client_pin(self, args):
        version = args.get(1)
        sub = args.get(2)
        if version not in self.pin_protocols:
            raise Refusal(self.ERR_INVALID_PARAMETER)
        if sub == 0x01:
            return {3: self.retries}
        if sub == 0x02:
            return {1: _cose(self._agreement.public_key())}
        if sub in (0x05, 0x09):
            self.pin_token_requests += 1
            if self.pin is None:
                raise Refusal(self.ERR_PIN_NOT_SET)
            if self.retries == 0:
                raise Refusal(self.ERR_PIN_BLOCKED)
            if sub == 0x09 and not self.permissions_tokens:
                raise Refusal(self.ERR_INVALID_PARAMETER)
            shared = self._shared(version, args[3])
            pin_hash = _decrypt(version, shared, args[6])
            expected = hashlib.sha256(self.pin.encode("utf-8")).digest()[:16]
            if not hmac.compare_digest(pin_hash, expected):
                self.retries -= 1
                self._agreement = ec.generate_private_key(ec.SECP256R1())
                raise Refusal(self.ERR_PIN_BLOCKED if self.retries == 0 else self.ERR_PIN_INVALID)
            self.retries = 8
            self._token = os.urandom(32)
            self._token_version = version
            return {2: _encrypt(version, shared, self._token)}
        raise Refusal(self.ERR_INVALID_PARAMETER)

    def _presence(self, presence_hook):
        outcome = presence_hook(self.presence, self.keepalives)
        if outcome == "cancelled":
            raise Refusal(self.ERR_KEEPALIVE_CANCEL)
        if self.presence == "deny":
            raise Refusal(self.ERR_OPERATION_DENIED)
        if self.presence == "timeout":
            raise Refusal(self.ERR_USER_ACTION_TIMEOUT)

    def make_credential(self, args, presence_hook):
        cdh = args[1]
        rp_id = args[2]["id"]
        rp_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
        if not any(p.get("alg") == -7 for p in args[4]):
            raise Refusal(self.ERR_UNSUPPORTED_ALGORITHM)
        uv = False
        if 8 in args:
            self._check_param(cdh, args[8], args.get(9, 1))
            uv = True
        elif self.pin is not None and not self.make_cred_uv_not_rqd:
            raise Refusal(self.ERR_PUAT_REQUIRED)
        for descriptor in args.get(5) or []:
            entry = self.credentials.get(bytes(descriptor["id"]))
            if entry and entry["rp_hash"] == rp_hash:
                self._presence(presence_hook)
                raise Refusal(self.ERR_CREDENTIAL_EXCLUDED)
        self._presence(presence_hook)
        credential_id = os.urandom(48)
        private_key = ec.generate_private_key(ec.SECP256R1())
        self.credentials[credential_id] = {
            "rp_hash": rp_hash,
            "key": private_key,
            "random_uv": os.urandom(32),
            "random_no_uv": os.urandom(32),
            "hmac_secret": bool((args.get(6) or {}).get("hmac-secret")),
        }
        n = private_key.public_key().public_numbers()
        public_cose = {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"),
                       -3: n.y.to_bytes(32, "big")}
        extensions = {}
        if (args.get(6) or {}).get("hmac-secret") and self.hmac_secret_supported:
            extensions["hmac-secret"] = True
        flags = 0x01 | 0x40 | (0x04 if uv else 0) | (0x80 if extensions else 0)
        auth_data = (rp_hash + bytes([flags]) + struct.pack(">I", 1) + self.aaguid
                     + struct.pack(">H", len(credential_id)) + credential_id
                     + sa_cbor.encode(public_cose)
                     + (sa_cbor.encode(extensions) if extensions else b""))
        return {1: "none", 2: auth_data, 3: {}}

    def get_assertion(self, args, presence_hook):
        rp_id = args[1]
        cdh = args[2]
        rp_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
        chosen = None
        for descriptor in args.get(3) or []:
            entry = self.credentials.get(bytes(descriptor["id"]))
            if entry and entry["rp_hash"] == rp_hash:
                chosen = bytes(descriptor["id"])
                break
        if chosen is None:
            raise Refusal(self.ERR_NO_CREDENTIALS)
        entry = self.credentials[chosen]
        uv = False
        if 6 in args:
            self._check_param(cdh, args[6], args.get(7, 1))
            uv = True
        up = (args.get(5) or {}).get("up", True)
        if up:
            self._presence(presence_hook)
        extensions = {}
        request = (args.get(4) or {}).get("hmac-secret")
        if request is not None and self.hmac_secret_supported and entry["hmac_secret"]:
            version = request.get(4, 1)
            shared = self._shared(version, request[1])
            if not hmac.compare_digest(_authenticate(version, shared, request[2]),
                                       bytes(request[3])):
                raise Refusal(self.ERR_PIN_AUTH_INVALID)
            salts = _decrypt(version, shared, request[2])
            secret = entry["random_uv"] if uv else entry["random_no_uv"]
            output = b"".join(hmac.new(secret, salts[i:i + 32], hashlib.sha256).digest()
                              for i in range(0, len(salts), 32))
            extensions["hmac-secret"] = _encrypt(version, shared, output)
        flags = (0x01 if up else 0) | (0x04 if uv else 0) | (0x80 if extensions else 0)
        auth_data = (rp_hash + bytes([flags]) + struct.pack(">I", 2)
                     + (sa_cbor.encode(extensions) if extensions else b""))
        from cryptography.hazmat.primitives.asymmetric import ec as _ec
        signature = entry["key"].sign(auth_data + cdh, _ec.ECDSA(hashes.SHA256()))
        return {1: {"id": chosen, "type": "public-key"}, 2: auth_data, 3: signature}

    # -- test hooks ---------------------------------------------------------
    def hmac_output(self, credential_id, prf_input, uv):
        """What a correct client must have derived. For assertions only."""
        salt = hashlib.sha256(b"WebAuthn PRF\x00" + bytes(prf_input)).digest()
        entry = self.credentials[bytes(credential_id)]
        secret = entry["random_uv"] if uv else entry["random_no_uv"]
        return hmac.new(secret, salt, hashlib.sha256).digest()


class SoftHidConnection(object):
    """CTAPHID over an in-memory packet queue, bound to one SoftAuthenticator."""

    packet_size = 64

    def __init__(self, authenticator):
        self.authenticator = authenticator
        self.outbox = collections.deque()
        self.partial = {}
        self.cancel_requested = False
        self.pending = None
        self.closed = False
        self.written_packets = []

    # -- host -> device ---------------------------------------------------
    def write_packet(self, packet, timeout=5.0):
        packet = bytes(packet)
        assert len(packet) == 64, "CTAPHID packets are 64 bytes"
        self.written_packets.append(packet)
        cid, first = struct.unpack(">IB", packet[:5])
        if first & 0x80:
            command = first & 0x7F
            length = struct.unpack(">H", packet[5:7])[0]
            if command == CTAPHID_CANCEL:
                self.cancel_requested = True
                return
            self.partial[cid] = [command, length, bytearray(packet[7:7 + min(length, 57)]), 0]
        else:
            state = self.partial.get(cid)
            assert state is not None, "continuation without an init packet"
            assert first == state[3], "continuation sequence out of order"
            state[3] += 1
            state[2].extend(packet[5:5 + min(state[1] - len(state[2]), 59)])
        command, length, data, _seq = self.partial[cid]
        if len(data) >= length:
            del self.partial[cid]
            self._dispatch(cid, command, bytes(data))

    def _frames(self, cid, command, payload):
        frames = []
        first = payload[:57]
        frames.append((struct.pack(">IBH", cid, 0x80 | command, len(payload)) + first).ljust(64, b"\x00"))
        offset, seq = len(first), 0
        while offset < len(payload):
            chunk = payload[offset:offset + 59]
            frames.append((struct.pack(">IB", cid, seq) + chunk).ljust(64, b"\x00"))
            offset += len(chunk)
            seq += 1
        return frames

    def _keepalive(self, cid, status=2):
        return (struct.pack(">IBH", cid, 0x80 | CTAPHID_KEEPALIVE, 1) + bytes([status])).ljust(64, b"\x00")

    def _dispatch(self, cid, command, payload):
        if command == CTAPHID_INIT:
            nonce = payload[:8]
            new_cid = 0x10000000 + len(self.written_packets)
            body = nonce + struct.pack(">I", new_cid) + bytes([2, 5, 8, 0, 0x05])
            self.outbox.extend(self._frames(cid, CTAPHID_INIT, body))
            return
        if command != CTAPHID_CBOR:
            self.outbox.extend(self._frames(cid, CTAPHID_ERROR, b"\x01"))
            return
        self.cancel_requested = False

        def presence(mode, count):
            if mode == "wait_for_cancel":
                self.pending = (cid, payload)
                raise _Suspend()
            for _ in range(count):
                self.outbox.append(self._keepalive(cid))
            return "touched"

        try:
            response = self.authenticator.handle(payload, presence)
        except _Suspend:
            return
        self.outbox.extend(self._frames(cid, CTAPHID_CBOR, response))

    # -- device -> host -----------------------------------------------------
    def read_packet(self, timeout=5.0):
        if self.outbox:
            return self.outbox.popleft()
        if self.pending is not None:
            cid, _payload = self.pending
            if self.cancel_requested:
                self.pending = None
                self.cancel_requested = False
                return self._frames(cid, CTAPHID_CBOR, b"\x2d")[0]
            return self._keepalive(cid)
        raise HidTimeout("no packet")

    def close(self):
        self.closed = True


class _Suspend(Exception):
    pass


class SoftDescriptor(object):
    def __init__(self, name="Soft Authenticator"):
        self.path = "soft://" + name
        self.vendor_id = 0x1050
        self.product_id = 0x0407
        self.product_name = name

    def to_dict(self):
        return {"vendor_id": "1050", "product_id": "0407", "product_name": self.product_name}


class SoftEnumeration(object):
    def __init__(self, devices, access_denied=0):
        self.devices = devices
        self.access_denied = access_denied
        self.hid_interfaces = len(devices)


class SoftKeyRing(object):
    """A set of plugged-in soft authenticators, pluggable into sa_ctap."""

    def __init__(self, *authenticators):
        self.plugged = list(authenticators)
        self.access_denied = 0
        self.connections = []

    def list_devices(self):
        return SoftEnumeration([SoftDescriptor("soft-%d" % i) for i in range(len(self.plugged))],
                               self.access_denied)

    def connect(self, descriptor):
        index = int(descriptor.product_name.rsplit("-", 1)[1])
        connection = SoftHidConnection(self.plugged[index])
        self.connections.append(connection)
        return connection
