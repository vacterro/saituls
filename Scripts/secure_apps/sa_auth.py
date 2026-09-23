"""Authentication providers for SAITULS Secure Apps.

A provider answers exactly one question: *can this authenticator produce the
key material that unwraps this profile's volume secret?* It knows nothing
about BitLocker, VHDX, mount points or application lifetime -- storage
backends and auth providers are independent axes, so a future provider can be
added without touching storage and vice versa.

The first provider is ``yubikey-fido2-hmac-secret``. It requires the CTAP2
``hmac-secret`` extension, and **enrollment refuses to continue without it**.
A presence-only authenticator cannot produce key material, so accepting one
would silently turn an encrypted vault into a doorbell. That downgrade is the
single most tempting mistake in this design and it is refused explicitly:
:class:`AuthCapabilityError`.

Dependency contract
-------------------

``yubikey-fido2-hmac-secret`` is written against **python-fido2 2.x**
(:data:`FIDO2_SUPPORTED_RANGE`). The 1.x surface -- ``Fido2Client(device,
origin)`` plus implicit extension handling -- does not exist any more, and it
is deliberately not probed for: a provider that quietly accepts two
incompatible library generations is a provider that breaks on the one machine
nobody tested. The installed version is checked every time the modules are
loaded and an out-of-range install raises :class:`AuthUnavailableError`
naming the supported range. ``tests/test_secure_apps_fido2_contract.py``
proves the construction path against the really installed package, without
hardware.

Transports
----------

Exactly one transport is chosen per host, and the choice is *reported*, never
inferred by the caller:

``WINDOWS_WEBAUTHN``
    ``fido2.client.windows.WindowsClient``. Preferred on Windows because the
    broker deliberately runs non-elevated, and Windows 10 1903+ hides FIDO
    HID devices from medium-integrity processes. Carrying an hmac-secret salt
    through the platform API needs ``WebAuthNGetAssertionOptions`` struct
    version 6, which the DLL's API version gates
    (:data:`WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION`). An older
    ``webauthn.dll`` can create an hmac-secret credential and can never read
    from it, so it is reported unusable instead of being tried and failing
    halfway through an enrollment.

``DIRECT_CTAP``
    ``fido2.client.Fido2Client`` over CTAPHID, built with
    ``DefaultClientDataCollector``, a ``UserInteraction`` and
    ``HmacSecretExtension(allow_hmac_secret=True)``. On Windows this needs an
    elevated process: a medium-integrity one does not even enumerate the
    authenticator, which is why the capability detail says so out loud.

``UNAVAILABLE``
    Neither path can produce key material on this host, and ``detail`` names
    the precondition that failed.

A ``WindowsClient`` failure is never retried on the HID path. The two have
different privilege, prompt and UX properties; silently downgrading from the
one the host selected is how "why did it suddenly want a PIN in a console"
bugs are born.

User verification
-----------------

A profile declares ``authentication.user_verification``. At ``required`` the
credential is created with UV required and **every** assertion is checked for
the UV bit in the returned authenticator data, so possession of the key alone
is not sufficient -- which is the entire point of a protected vault.
Enrollment fails with a capability error if UV is required and the
authenticator cannot do it (no FIDO2 PIN set, no built-in verification).

The PIN never becomes state here. It reaches this module only as the return
value of a caller-supplied callback, is handed straight to the library, and
is never stored, logged, written to a file, put on a command line or exported
into the environment. On Windows the platform API collects it in its own
native UX and the PIN never enters this process at all.

Three providers ship:

``yubikey-fido2-hmac-secret``
    In-process WebAuthn/CTAP2 via the ``fido2`` Python package.

``external-helper-fido2-hmac-secret``
    The same contract delegated to a separate helper executable, for hosts
    that would rather carry a modern .NET/WebAuthn binary than a Python HID
    stack. The contract is documented in :class:`ExternalHelperProvider` and
    in the subsystem README; key material crosses on stdout, never on a
    command line.

``fake-auth``
    Deterministic, test-only. Refused by :mod:`sa_config` unless the caller
    explicitly enables test providers.
"""
import base64
import copy
import json
import os
import subprocess
import time

import sa_crypto

RP_ID = "saituls.secure-apps.local"
RP_NAME = "SAITULS Secure Apps"
HMAC_SECRET_SALT_BYTES = 32
CREDENTIALS_SCHEMA = "saituls.secure-apps.credentials/1"
CREDENTIALS_SCHEMA_VERSION = 1

# -- python-fido2 dependency contract --------------------------------------
FIDO2_MIN_VERSION = (2, 0)
FIDO2_MAX_VERSION_EXCLUSIVE = (3, 0)
FIDO2_SUPPORTED_RANGE = "fido2>=2.0,<3"

# -- transports ------------------------------------------------------------
TRANSPORT_WINDOWS_WEBAUTHN = "WINDOWS_WEBAUTHN"
TRANSPORT_ELEVATED_CTAP_HELPER = "ELEVATED_CTAP_HELPER"
TRANSPORT_DIRECT_CTAP = "DIRECT_CTAP"
TRANSPORT_UNAVAILABLE = "UNAVAILABLE"
TRANSPORTS = (TRANSPORT_WINDOWS_WEBAUTHN, TRANSPORT_ELEVATED_CTAP_HELPER,
              TRANSPORT_DIRECT_CTAP, TRANSPORT_UNAVAILABLE)
DEFAULT_HMAC_TRANSPORT_SEMANTICS = "direct-ctap-v1"

# WebAuthNGetAssertionOptions carries pHmacSecretSaltValues only from struct
# version 6, and python-fido2 derives that version from
# WebAuthNGetApiVersionNumber(). Windows 10 22H2 ships API version 2, so the
# platform path there cannot return hmac-secret output at all.
WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION = 6

# -- user verification -----------------------------------------------------
UV_REQUIRED = "required"
UV_PREFERRED = "preferred"
UV_DISCOURAGED = "discouraged"
USER_VERIFICATION_VALUES = (UV_REQUIRED, UV_PREFERRED, UV_DISCOURAGED)

# CTAP2 status codes this module reacts to. A local table beats importing a
# transport module for five integers.
_CTAP_NO_CREDENTIALS = 0x2E
_CTAP_PIN_NOT_SET = 0x35
_CTAP_CANCELLED = (0x2D, 0x27, 0x2F, 0x3A)
_CTAP_PIN_FAILED = (0x31, 0x32, 0x33, 0x34, 0x3C, 0x3F)
_CTAP_UV_REQUIRED = (0x36, 0x3B)


class AuthError(Exception):
    """Base class. Carries an audit ``category`` and never a secret."""
    category = "auth_failed"


class AuthUnavailableError(AuthError):
    """No authenticator, or the provider's dependency is not installed."""
    category = "auth_unavailable"


class AuthCapabilityError(AuthError):
    """The authenticator cannot produce the required key material.

    Raised when ``hmac-secret`` is absent. Never downgraded to a presence
    check -- see the module docstring.
    """
    category = "auth_capability"


class AuthCancelledError(AuthError):
    """The user dismissed the prompt or the touch timed out."""
    category = "auth_cancelled"


class AuthWrongCredentialError(AuthError):
    """A key was present but it is not the enrolled credential."""
    category = "auth_wrong_credential"


def b64e(raw):
    return base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")


def b64d(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def parse_library_version(text):
    """``"2.2.1"`` -> ``(2, 2)``. Tolerates suffixes like ``2.0.0b1``."""
    parts = []
    for chunk in str(text).split(".")[:2]:
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits or 0))
    while len(parts) < 2:
        parts.append(0)
    return tuple(parts)


def require_supported_fido2(version):
    """Fail closed on a python-fido2 outside :data:`FIDO2_SUPPORTED_RANGE`."""
    parsed = parse_library_version(version)
    if not (FIDO2_MIN_VERSION <= parsed < FIDO2_MAX_VERSION_EXCLUSIVE):
        raise AuthUnavailableError(
            "python-fido2 %s is outside the supported range (%s). The "
            "yubikey-fido2-hmac-secret provider is written against the 2.x "
            "client API (DefaultClientDataCollector, WindowsClient, "
            "HmacSecretExtension) and refuses to run against another "
            "generation rather than fail halfway through an enrollment."
            % (version, FIDO2_SUPPORTED_RANGE))
    return parsed


def _process_is_elevated():
    """True/False on Windows, None where the question does not apply."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def _hid_visibility_note():
    """Why a Windows host may enumerate zero authenticators."""
    if _process_is_elevated() is False:
        return (" This process is not elevated, and Windows 10 1903+ hides "
                "FIDO HID devices from medium-integrity processes, so a "
                "connected key looks exactly like an absent one here.")
    return ""


class Capabilities(object):
    """What this host can actually do, as facts rather than hopes."""

    __slots__ = ("provider", "available", "hmac_secret", "authenticators",
                 "detail", "transport", "user_verification", "library_version")

    def __init__(self, provider, available=False, hmac_secret=False,
                 authenticators=0, detail="", transport=TRANSPORT_UNAVAILABLE,
                 user_verification=False, library_version=""):
        self.provider = provider
        self.available = available
        self.hmac_secret = hmac_secret
        self.authenticators = authenticators
        self.detail = detail
        # One of TRANSPORTS: the path that would really be used, not the one
        # the provider would prefer in the abstract.
        self.transport = transport
        self.user_verification = user_verification
        self.library_version = library_version

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


class SecureAuthProvider(object):
    """Interface every authentication provider implements."""

    name = "abstract"
    provides_key_material = True
    #: One of :data:`TRANSPORTS`. Updated when an operation resolves a path.
    transport = TRANSPORT_UNAVAILABLE
    #: Profile policy: one of :data:`USER_VERIFICATION_VALUES`.
    user_verification = UV_DISCOURAGED

    def capabilities(self):
        raise NotImplementedError

    def create_credential(self, credential_profile, require_hmac_secret=True):
        """Return a dict with at least ``credential_id`` (bytes).

        Refuses with :class:`AuthCapabilityError` when the profile requires
        user verification and the authenticator did not perform it.
        """
        raise NotImplementedError

    def get_key_material(self, credential_ids, salt):
        """Return ``(credential_id_bytes, SecretBuffer)`` for one enrolled id.

        *credential_ids* is the allow-list; a key that answers with anything
        outside it raises :class:`AuthWrongCredentialError`. When the profile
        requires user verification, an assertion whose authenticator data
        does not carry the UV bit is refused -- a key that only proves
        presence must not open a vault that asked for more.
        """
        raise NotImplementedError


# --------------------------------------------------------------------------
# yubikey-fido2-hmac-secret
# --------------------------------------------------------------------------
class Fido2HmacSecretProvider(SecureAuthProvider):
    """WebAuthn/CTAP2 hmac-secret via python-fido2 2.x.

    Two transports, one chosen per host and reported as
    :data:`Capabilities.transport`; see the module docstring for why the
    choice is never silently downgraded.
    """

    name = "yubikey-fido2-hmac-secret"

    def __init__(self, rp_id=RP_ID, user_name="saituls", timeout=60,
                 pin_callback=None, user_verification=UV_DISCOURAGED,
                 window_handle=None, helper=None):
        self.rp_id = rp_id
        self.user_name = user_name
        self.timeout = timeout
        # Called as pin_callback(rp_id) -> str|None. The return value is
        # passed straight to the library and kept nowhere.
        self.pin_callback = pin_callback
        if user_verification not in USER_VERIFICATION_VALUES:
            raise AuthError("unsupported user_verification %r"
                            % (user_verification,))
        self.user_verification = user_verification
        # Parent for the native Windows WebAuthn dialog. None means "the
        # foreground window", which is right for a console and wrong for a
        # GUI whose broker lives on a worker thread.
        self.window_handle = window_handle
        self.helper = helper
        self.transport = TRANSPORT_UNAVAILABLE
        self._modules_cache = None

    def _channel(self):
        if self.helper is None:
            return None
        if not getattr(self.helper, "running", False):
            self.helper.start()
        return self.helper

    # -- dependency plumbing ---------------------------------------------
    def _modules(self):
        if self._modules_cache is not None:
            return self._modules_cache
        try:
            from importlib import metadata
            from fido2.client import (
                ClientError, DefaultClientDataCollector, Fido2Client,
                PinRequiredError, UserInteraction)
            from fido2.ctap2 import Ctap2
            from fido2.ctap2.extensions import HmacSecretExtension
            from fido2.hid import CtapHidDevice
            from fido2.webauthn import (
                AuthenticatorData, AuthenticatorSelectionCriteria,
                PublicKeyCredentialCreationOptions,
                PublicKeyCredentialDescriptor,
                PublicKeyCredentialParameters,
                PublicKeyCredentialRequestOptions,
                PublicKeyCredentialRpEntity, PublicKeyCredentialType,
                PublicKeyCredentialUserEntity, ResidentKeyRequirement,
                UserVerificationRequirement)
        except ImportError as exc:
            raise AuthUnavailableError(
                "FIDO2 support needs the 'fido2' Python package "
                "(pip install %s). Provider %s cannot run without it: %s"
                % (FIDO2_SUPPORTED_RANGE, self.name, exc))
        try:
            version = metadata.version("fido2")
        except Exception:
            version = "0"
        require_supported_fido2(version)
        mod = {
            "version": version,
            "ClientError": ClientError,
            "ClientDataCollector": DefaultClientDataCollector,
            "Fido2Client": Fido2Client,
            "PinRequiredError": PinRequiredError,
            "UserInteraction": UserInteraction,
            "Ctap2": Ctap2,
            "HmacSecretExtension": HmacSecretExtension,
            "CtapHidDevice": CtapHidDevice,
            "AuthenticatorData": AuthenticatorData,
            "Selection": AuthenticatorSelectionCriteria,
            "CreationOptions": PublicKeyCredentialCreationOptions,
            "Descriptor": PublicKeyCredentialDescriptor,
            "CredParams": PublicKeyCredentialParameters,
            "RequestOptions": PublicKeyCredentialRequestOptions,
            "RpEntity": PublicKeyCredentialRpEntity,
            "CredType": PublicKeyCredentialType,
            "UserEntity": PublicKeyCredentialUserEntity,
            "ResidentKey": ResidentKeyRequirement,
            "UV": UserVerificationRequirement,
        }
        # The platform client is optional: absent off Windows, and present
        # but too old to carry an hmac-secret salt on older Windows 10.
        mod["WindowsClient"] = None
        mod["WEBAUTHN_API_VERSION"] = 0
        try:
            from fido2.client.windows import WindowsClient
            try:
                from fido2.client.win_api import WEBAUTHN_API_VERSION
            except ImportError:                     # pragma: no cover
                from fido2.client.windows import WEBAUTHN_API_VERSION
            mod["WindowsClient"] = WindowsClient
            mod["WEBAUTHN_API_VERSION"] = int(WEBAUTHN_API_VERSION)
        except Exception:
            pass
        self._modules_cache = mod
        return mod

    def _collector(self, mod):
        return mod["ClientDataCollector"]("https://" + self.rp_id)

    def _interaction(self, mod):
        """UserInteraction that forwards a PIN request and stores nothing."""
        pin_callback = self.pin_callback

        class _Interaction(mod["UserInteraction"]):
            def __init__(self):
                self.pin_requested = False
                self.uv_requested = False

            def prompt_up(self):
                pass

            def request_pin(self, permissions, rp_id):
                self.pin_requested = True
                if pin_callback is None:
                    return None         # the library turns this into a refusal
                value = pin_callback(rp_id)
                return value or None

            def request_uv(self, permissions, rp_id):
                self.uv_requested = True
                return True

        return _Interaction()

    # -- transport selection ---------------------------------------------
    def _devices(self, mod):
        try:
            return list(mod["CtapHidDevice"].list_devices())
        except Exception as exc:
            raise AuthUnavailableError("could not enumerate FIDO2 devices: %s"
                                       % type(exc).__name__)

    @staticmethod
    def _close_all(devices):
        """``list_devices()`` OPENS every authenticator it enumerates.

        A loop that returns or breaks on the first usable one therefore
        leaves the rest held open, and a held-open FIDO HID handle is how the
        next operation gets an access-denied it cannot explain.
        """
        for device in devices:
            try:
                device.close()
            except Exception:
                pass

    def _windows_client(self, mod):
        """The platform client, or an explanation of why there is none."""
        client_cls = mod["WindowsClient"]
        if client_cls is None or not client_cls.is_available():
            return None, "the Windows WebAuthn API is not available"
        api = mod["WEBAUTHN_API_VERSION"]
        if api < WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION:
            return None, (
                "Windows WebAuthn API version %d cannot carry an hmac-secret "
                "salt (version %d is the first that can), so the platform "
                "path can create a credential here but never read key "
                "material from it"
                % (api, WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION))
        return client_cls(self._collector(mod), handle=self.window_handle,
                          allow_hmac_secret=True), (
            "Windows WebAuthn API version %d" % api)

    def _direct_client(self, mod, device):
        """Fido2Client on one HID device, hmac-secret explicitly allowed."""
        return mod["Fido2Client"](
            device,
            client_data_collector=self._collector(mod),
            user_interaction=self._interaction(mod),
            extensions=[mod["HmacSecretExtension"](allow_hmac_secret=True)])

    def _probe_device(self, mod, device):
        """``(hmac_secret, user_verification)`` straight from getInfo."""
        info = mod["Ctap2"](device).info
        extensions = list(info.extensions or [])
        options = dict(info.options or {})
        uv = options.get("clientPin") is True or options.get("uv") is True
        return ("hmac-secret" in extensions), uv

    def resolve_transport(self):
        """``(transport, detail, authenticators, hmac_secret, uv)``.

        The one place that decides which path this host really has. Callers
        get the answer instead of a guess, and a Windows failure is never
        retried over HID.
        """
        mod = self._modules()
        client, windows_detail = self._windows_client(mod)
        if client is not None:
            self.transport = TRANSPORT_WINDOWS_WEBAUTHN
            # The platform API exposes no getInfo: hmac-secret and UV are
            # properties of the API version plus whatever key the user
            # presents, and are proven at enrollment, not here.
            return (TRANSPORT_WINDOWS_WEBAUTHN, windows_detail, 1, True, True)

        # On Windows, when WebAuthn is unavailable or cannot carry an hmac-secret
        # salt (e.g. Win10 API version < 6), prefer the elevated CTAP helper:
        if os.name == "nt" and self.helper is not None:
            try:
                channel = self._channel()
                caps = channel.fido_capabilities()
                count = int(caps.get("authenticators") or 0)
                hmac_secret = bool(caps.get("hmac_secret"))
                uv = bool(caps.get("user_verification"))
                detail = str(caps.get("detail") or "direct CTAP over HID (elevated helper)")
                self.transport = TRANSPORT_ELEVATED_CTAP_HELPER
                return (TRANSPORT_ELEVATED_CTAP_HELPER, detail, count, hmac_secret, uv)
            except Exception as exc:
                self.transport = TRANSPORT_UNAVAILABLE
                return (TRANSPORT_UNAVAILABLE,
                        "%s, and elevated CTAP helper unavailable: %s" % (windows_detail, exc),
                        0, False, False)

        if os.name == "nt" and not _process_is_elevated():
            self.transport = TRANSPORT_UNAVAILABLE
            return (TRANSPORT_UNAVAILABLE,
                    "%s, and no FIDO2 authenticator is visible over HID.%s"
                    % (windows_detail, _hid_visibility_note()), 0, False, False)

        devices = self._devices(mod)
        if not devices:
            self.transport = TRANSPORT_UNAVAILABLE
            return (TRANSPORT_UNAVAILABLE,
                    "%s, and no FIDO2 authenticator is visible over HID.%s"
                    % (windows_detail, _hid_visibility_note()), 0, False, False)
        hmac_secret = False
        user_verification = False
        try:
            for device in devices:
                try:
                    device_hmac, device_uv = self._probe_device(mod, device)
                except Exception:
                    continue
                if device_hmac:
                    hmac_secret = True
                    user_verification = device_uv
                    break
        finally:
            self._close_all(devices)
        self.transport = TRANSPORT_DIRECT_CTAP
        if not hmac_secret:
            detail = ("direct CTAP over HID: the connected authenticator does "
                      "not advertise the CTAP2 hmac-secret extension")
        else:
            detail = "direct CTAP over HID (%s)" % windows_detail
        return (TRANSPORT_DIRECT_CTAP, detail, len(devices), hmac_secret,
                user_verification)

    # -- interface --------------------------------------------------------
    def capabilities(self):
        try:
            mod = self._modules()
        except AuthUnavailableError as exc:
            return Capabilities(self.name, available=False, detail=str(exc),
                                transport=TRANSPORT_UNAVAILABLE)
        version = mod["version"]
        try:
            transport, detail, count, hmac_secret, uv = self.resolve_transport()
        except AuthUnavailableError as exc:
            return Capabilities(self.name, available=False, detail=str(exc),
                                transport=TRANSPORT_UNAVAILABLE,
                                library_version=version)
        available = transport != TRANSPORT_UNAVAILABLE
        if available and self.user_verification == UV_REQUIRED and not uv:
            detail = (detail + "; this profile requires user verification but "
                      "the authenticator has no FIDO2 PIN set and no built-in "
                      "verification")
        return Capabilities(self.name, available=available,
                            hmac_secret=bool(hmac_secret),
                            authenticators=count, detail=detail,
                            transport=transport, user_verification=bool(uv),
                            library_version=version)

    # -- options ----------------------------------------------------------
    def _creation_options(self, mod, credential_profile, user_id, uv):
        return mod["CreationOptions"](
            rp=mod["RpEntity"](id=self.rp_id, name=RP_NAME),
            user=mod["UserEntity"](id=user_id, name=self.user_name,
                                   display_name="SAITULS " + credential_profile),
            challenge=os.urandom(32),
            pub_key_cred_params=[
                mod["CredParams"](type=mod["CredType"].PUBLIC_KEY, alg=-7),
                mod["CredParams"](type=mod["CredType"].PUBLIC_KEY, alg=-257),
            ],
            authenticator_selection=mod["Selection"](
                resident_key=mod["ResidentKey"].DISCOURAGED,
                user_verification=mod["UV"](uv)),
            timeout=self.timeout * 1000,
            extensions={"hmacCreateSecret": True},
        )

    def _request_options(self, mod, allowed, salt, uv):
        return mod["RequestOptions"](
            challenge=os.urandom(32),
            rp_id=self.rp_id,
            allow_credentials=[
                mod["Descriptor"](type=mod["CredType"].PUBLIC_KEY, id=c)
                for c in allowed],
            user_verification=mod["UV"](uv),
            timeout=self.timeout * 1000,
            extensions={"hmacGetSecret": {"salt1": bytes(salt)}},
        )

    @staticmethod
    def _uv_performed(mod, authenticator_data):
        return bool(authenticator_data.flags
                    & mod["AuthenticatorData"].FLAG.UV)

    def _require_uv(self, mod, authenticator_data, where):
        if self.user_verification != UV_REQUIRED:
            return self._uv_performed(mod, authenticator_data)
        performed = self._uv_performed(mod, authenticator_data)
        if not performed:
            raise AuthCapabilityError(
                "this profile requires user verification but the authenticator "
                "completed %s without it. Set a FIDO2 PIN on the key (or use a "
                "key with built-in verification) and try again -- possession of "
                "the key alone must not open this vault." % where)
        return True

    # -- credential creation ---------------------------------------------
    def _finish_creation(self, mod, response, require_hmac_secret, user_id,
                         transport):
        results = response.client_extension_results
        created = getattr(results, "hmac_create_secret", None)
        if require_hmac_secret and created is not True:
            raise AuthCapabilityError(
                "the authenticator accepted the credential but did not enable "
                "hmac-secret on it, so it cannot protect an encrypted vault; "
                "enrollment refuses to downgrade to a presence-only check")
        auth_data = response.response.attestation_object.auth_data
        credential_data = auth_data.credential_data
        if credential_data is None:
            raise AuthError("the attestation object carried no credential data")
        uv_performed = self._require_uv(mod, auth_data, "enrollment")
        return {
            "credential_id": bytes(credential_data.credential_id),
            "rp_id": self.rp_id,
            "user_id": user_id,
            "provider": self.name,
            "transport": transport,
            "user_verification": bool(uv_performed),
            "hmac_transport_semantics": DEFAULT_HMAC_TRANSPORT_SEMANTICS,
        }

    @staticmethod
    def _require_connected(transport, count, detail):
        """No authenticator behind the elevated helper: refuse before the PIN.

        The helper answers capabilities even with nothing plugged in, so the
        transport alone does not mean a key is there. Asking for a PIN first
        would collect it for nothing.
        """
        if transport == TRANSPORT_ELEVATED_CTAP_HELPER and not count:
            raise AuthUnavailableError(
                detail or "no FIDO2 authenticator is connected (elevated helper)")

    def create_credential(self, credential_profile, require_hmac_secret=True):
        mod = self._modules()
        transport, detail, count, _hmac, uv_available = self.resolve_transport()
        if transport == TRANSPORT_UNAVAILABLE:
            raise AuthUnavailableError(detail)
        self._require_connected(transport, count, detail)
        if self.user_verification == UV_REQUIRED and not uv_available:
            raise AuthCapabilityError(
                "this profile requires user verification but the connected "
                "authenticator reports none configured (no FIDO2 PIN, no "
                "built-in verification). %s" % detail)
        user_id = os.urandom(16)
        uv = self.user_verification
        if transport == TRANSPORT_WINDOWS_WEBAUTHN:
            client, _detail = self._windows_client(mod)
            options = self._creation_options(mod, credential_profile, user_id, uv)
            try:
                response = client.make_credential(options)
            except AuthError:
                raise
            except Exception as exc:
                # Explicitly NOT retried over HID: see the module docstring.
                raise self._translate(exc, TRANSPORT_WINDOWS_WEBAUTHN)
            return self._finish_creation(mod, response, require_hmac_secret,
                                         user_id, transport)
        if transport == TRANSPORT_ELEVATED_CTAP_HELPER:
            channel = self._channel()
            pin = self.pin_callback(self.rp_id) if self.pin_callback else None
            try:
                raw_res = channel.fido_create(
                    rp_id=self.rp_id,
                    credential_profile=credential_profile,
                    user_id_b64=b64e(user_id),
                    user_verification=uv,
                    require_hmac_secret=require_hmac_secret,
                    pin=pin,
                    timeout_seconds=self.timeout
                )
            except Exception as exc:
                raise self._translate_helper_error(exc, TRANSPORT_ELEVATED_CTAP_HELPER)
            finally:
                pin = None
            if require_hmac_secret and not raw_res.get("hmac_secret"):
                raise AuthCapabilityError(
                    "the authenticator accepted the credential but did not enable "
                    "hmac-secret on it, so it cannot protect an encrypted vault; "
                    "enrollment refuses to downgrade to a presence-only check")
            if uv == UV_REQUIRED and not raw_res.get("user_verification"):
                raise AuthCapabilityError(
                    "this profile requires user verification but the authenticator "
                    "completed enrollment without it. Set a FIDO2 PIN on the key "
                    "(or use a key with built-in verification) and try again -- "
                    "possession of the key alone must not open this vault.")
            return {
                "credential_id": b64d(raw_res["credential_id_b64"]),
                "rp_id": self.rp_id,
                "user_id": user_id,
                "provider": self.name,
                "transport": transport,
                "user_verification": bool(raw_res.get("user_verification")),
                "hmac_transport_semantics": DEFAULT_HMAC_TRANSPORT_SEMANTICS,
            }
        last_error = None
        devices = self._devices(mod)
        try:
            for device in devices:
                try:
                    client = self._direct_client(mod, device)
                    options = self._creation_options(mod, credential_profile,
                                                     user_id, uv)
                    response = client.make_credential(options)
                    return self._finish_creation(mod, response,
                                                 require_hmac_secret, user_id,
                                                 transport)
                except AuthError as exc:
                    last_error = exc
                except Exception as exc:
                    last_error = self._translate(exc, TRANSPORT_DIRECT_CTAP)
        finally:
            self._close_all(devices)
        raise last_error or AuthUnavailableError(
            "credential creation did not complete")

    # -- assertion --------------------------------------------------------
    def _finish_assertion(self, mod, selection, allowed):
        response = selection.get_response(0)
        results = response.client_extension_results
        output = getattr(results, "hmac_get_secret", None)
        secret = getattr(output, "output1", None) if output is not None else None
        if not secret:
            raise AuthCapabilityError(
                "the authenticator returned no hmac-secret output, so no key "
                "material was produced")
        used = bytes(response.raw_id)
        if used not in allowed:
            raise AuthWrongCredentialError(
                "the connected key answered with a credential that is not "
                "enrolled for this profile")
        self._require_uv(mod, response.response.authenticator_data,
                         "authentication")
        return used, sa_crypto.SecretBuffer(secret)

    def get_key_material(self, credential_ids, salt):
        if not credential_ids:
            raise AuthError("profile has no enrolled credential")
        mod = self._modules()
        transport, detail, count, _hmac, _uv = self.resolve_transport()
        if transport == TRANSPORT_UNAVAILABLE:
            raise AuthUnavailableError(detail)
        self._require_connected(transport, count, detail)
        allowed = [bytes(c) for c in credential_ids]
        uv = self.user_verification
        if transport == TRANSPORT_WINDOWS_WEBAUTHN:
            client, _detail = self._windows_client(mod)
            options = self._request_options(mod, allowed, salt, uv)
            try:
                selection = client.get_assertion(options)
            except AuthError:
                raise
            except Exception as exc:
                raise self._translate(exc, TRANSPORT_WINDOWS_WEBAUTHN)
            return self._finish_assertion(mod, selection, allowed)
        if transport == TRANSPORT_ELEVATED_CTAP_HELPER:
            channel = self._channel()
            pin = self.pin_callback(self.rp_id) if self.pin_callback else None
            try:
                raw_res = channel.fido_hmac(
                    rp_id=self.rp_id,
                    credential_ids_b64=[b64e(c) for c in allowed],
                    salt_b64=b64e(salt),
                    user_verification=uv,
                    pin=pin,
                    timeout_seconds=self.timeout
                )
            except Exception as exc:
                raise self._translate_helper_error(exc, TRANSPORT_ELEVATED_CTAP_HELPER)
            finally:
                pin = None
            used_id = b64d(raw_res["credential_id_b64"])
            if used_id not in allowed:
                raise AuthWrongCredentialError(
                    "the connected key answered with a credential that is not "
                    "enrolled for this profile")
            if uv == UV_REQUIRED and not raw_res.get("user_verification"):
                raise AuthCapabilityError(
                    "this profile requires user verification but the authenticator "
                    "completed authentication without it")
            secret = sa_crypto.SecretBuffer(b64d(raw_res["output_b64"]))
            return used_id, secret
        last_error = None
        devices = self._devices(mod)
        try:
            for device in devices:
                try:
                    client = self._direct_client(mod, device)
                    options = self._request_options(mod, allowed, salt, uv)
                    selection = client.get_assertion(options)
                    return self._finish_assertion(mod, selection, allowed)
                except AuthError as exc:
                    last_error = exc
                except Exception as exc:
                    last_error = self._translate(exc, TRANSPORT_DIRECT_CTAP)
        finally:
            self._close_all(devices)
        raise last_error or AuthUnavailableError(
            "authentication did not complete")

    @staticmethod
    def _translate_helper_error(exc, transport=TRANSPORT_UNAVAILABLE):
        cat = getattr(exc, "category", None)
        msg = str(exc)
        if cat == "auth_cancelled" or "cancel" in msg.lower() or "timeout" in msg.lower():
            return AuthCancelledError("the FIDO2 operation was cancelled or timed out")
        if cat == "auth_capability" or "capability" in msg.lower():
            return AuthCapabilityError(msg)
        if cat == "auth_wrong_credential" or "not hold" in msg.lower() or "wrong" in msg.lower():
            return AuthWrongCredentialError("the connected key does not hold this profile's credential")
        if cat == "auth_unavailable" or "not visible" in msg.lower() or "no fido2" in msg.lower():
            return AuthUnavailableError(msg)
        return AuthError("FIDO2 operation failed on the %s path: %s" % (transport, msg))

    # -- error translation -------------------------------------------------
    @staticmethod
    def _translate(exc, transport=TRANSPORT_UNAVAILABLE):
        """Library exception -> one of this module's audited categories."""
        code = getattr(getattr(exc, "cause", None), "code", None)
        if code is None:
            code = getattr(exc, "code", None)
        if isinstance(code, int):
            if code == _CTAP_NO_CREDENTIALS:
                return AuthWrongCredentialError(
                    "the connected key does not hold this profile's credential")
            if code in _CTAP_CANCELLED:
                return AuthCancelledError(
                    "the FIDO2 operation was cancelled or timed out")
            if code == _CTAP_PIN_NOT_SET:
                return AuthCapabilityError(
                    "the authenticator has no FIDO2 PIN set, so it cannot "
                    "perform the user verification this profile requires")
            if code in _CTAP_PIN_FAILED:
                return AuthError(
                    "the authenticator refused the PIN or its retry budget is "
                    "exhausted (%s path)" % transport)
            if code in _CTAP_UV_REQUIRED:
                return AuthCapabilityError(
                    "the authenticator demanded user verification that could "
                    "not be completed")
        text = ("%s %s" % (type(exc).__name__, exc)).lower()
        if "pinrequired" in type(exc).__name__.lower():
            return AuthError("the authenticator requires a PIN and none was "
                             "supplied")
        if "no credentials" in text or "no_credentials" in text:
            return AuthWrongCredentialError(
                "the connected key does not hold this profile's credential")
        if "cancel" in text or "timeout" in text or "timed out" in text:
            return AuthCancelledError(
                "the FIDO2 operation was cancelled or timed out")
        if "pin" in text:
            return AuthError("the authenticator requires a PIN interaction that "
                             "could not be completed")
        return AuthError("FIDO2 operation failed on the %s path (%s)"
                         % (transport, type(exc).__name__))


# --------------------------------------------------------------------------
# external-helper-fido2-hmac-secret
# --------------------------------------------------------------------------
class ExternalHelperProvider(SecureAuthProvider):
    """Delegates the same contract to a separate helper executable.

    CLI contract (stable, documented in ``Scripts/secure_apps/README.md``).
    Every invocation writes exactly one JSON object to stdout and exits 0 on
    success, nonzero on failure. No key material is ever passed on a command
    line or through the environment -- outputs travel on stdout only.

    ``<helper> capabilities``
        -> ``{"ok":true,"hmac_secret":true,"authenticators":1,"detail":"..."}``

    ``<helper> create --rp-id <id> --user-name <name> --profile <p>
      --require-hmac-secret``
        -> ``{"ok":true,"credential_id":"<base64url>","user_id":"<base64url>"}``

    ``<helper> hmac --rp-id <id> --salt <base64url>
      --credential-id <base64url> [--credential-id <base64url> ...]``
        -> ``{"ok":true,"credential_id":"<base64url>","output":"<base64url>"}``

    Failure shape for all three:
        ``{"ok":false,"error":"<category>","message":"<text>"}`` where category
        is one of ``capability``, ``cancelled``, ``wrong_credential``,
        ``unavailable``, ``failed``.

    User verification is part of the contract, not an afterthought:
    ``capabilities`` reports ``"user_verification": true|false``, ``create``
    and ``hmac`` take ``--user-verification <required|preferred|discouraged>``
    and must answer with ``"user_verification": true`` when the authenticator
    really performed it. A helper that omits the field while the profile
    requires UV is refused -- an unproven claim is not a proof.
    """

    name = "external-helper-fido2-hmac-secret"

    ERROR_MAP = {
        "capability": AuthCapabilityError,
        "cancelled": AuthCancelledError,
        "wrong_credential": AuthWrongCredentialError,
        "unavailable": AuthUnavailableError,
        "failed": AuthError,
    }

    def __init__(self, helper_executable, rp_id=RP_ID, timeout=120, runner=None,
                 user_verification=UV_DISCOURAGED):
        self.helper = helper_executable
        self.rp_id = rp_id
        self.timeout = timeout
        if user_verification not in USER_VERIFICATION_VALUES:
            raise AuthError("unsupported user_verification %r"
                            % (user_verification,))
        self.user_verification = user_verification
        self._runner = runner or self._spawn

    def _check_uv(self, payload, where):
        """Refuse a helper that will not prove the UV the profile demands."""
        if self.user_verification != UV_REQUIRED:
            return bool(payload.get("user_verification"))
        if payload.get("user_verification") is not True:
            raise AuthCapabilityError(
                "this profile requires user verification but the FIDO2 helper "
                "did not report that the authenticator performed it during %s"
                % where)
        return True

    def _spawn(self, argv, timeout):
        return subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)

    def _call(self, args):
        if not self.helper or not os.path.isfile(self.helper):
            raise AuthUnavailableError(
                "FIDO2 helper executable not found: %s" % (self.helper,))
        argv = [self.helper] + list(args)
        try:
            completed = self._runner(argv, self.timeout)
        except subprocess.TimeoutExpired:
            raise AuthCancelledError("the FIDO2 helper timed out")
        except OSError as exc:
            raise AuthUnavailableError("could not start the FIDO2 helper: %s"
                                       % type(exc).__name__)
        text = (completed.stdout or "").strip()
        start = text.find("{")
        if start < 0:
            raise AuthError("the FIDO2 helper produced no JSON result")
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError:
            raise AuthError("the FIDO2 helper produced malformed JSON")
        if not isinstance(payload, dict):
            raise AuthError("the FIDO2 helper produced a non-object result")
        if not payload.get("ok"):
            category = str(payload.get("error") or "failed")
            factory = self.ERROR_MAP.get(category, AuthError)
            raise factory(str(payload.get("message") or "helper reported failure"))
        return payload

    def capabilities(self):
        try:
            payload = self._call(["capabilities"])
        except AuthError as exc:
            return Capabilities(self.name, available=False, detail=str(exc))
        uv = bool(payload.get("user_verification"))
        detail = str(payload.get("detail") or "")
        if self.user_verification == UV_REQUIRED and not uv:
            detail = (detail + "; this profile requires user verification but "
                      "the helper reports none available").strip("; ")
        return Capabilities(self.name, available=True,
                            hmac_secret=bool(payload.get("hmac_secret")),
                            authenticators=int(payload.get("authenticators") or 0),
                            detail=detail,
                            transport=str(payload.get("transport")
                                          or TRANSPORT_DIRECT_CTAP),
                            user_verification=uv)

    def create_credential(self, credential_profile, require_hmac_secret=True):
        args = ["create", "--rp-id", self.rp_id, "--user-name", "saituls",
                "--profile", credential_profile,
                "--user-verification", self.user_verification]
        if require_hmac_secret:
            args.append("--require-hmac-secret")
        payload = self._call(args)
        cred = payload.get("credential_id")
        if not cred:
            raise AuthError("the FIDO2 helper returned no credential id")
        uv_performed = self._check_uv(payload, "enrollment")
        return {
            "credential_id": b64d(cred),
            "rp_id": self.rp_id,
            "user_id": b64d(payload["user_id"]) if payload.get("user_id") else b"",
            "provider": self.name,
            "transport": str(payload.get("transport") or TRANSPORT_DIRECT_CTAP),
            "user_verification": bool(uv_performed),
        }

    def get_key_material(self, credential_ids, salt):
        if not credential_ids:
            raise AuthError("profile has no enrolled credential")
        args = ["hmac", "--rp-id", self.rp_id, "--salt", b64e(salt),
                "--user-verification", self.user_verification]
        allowed = [bytes(c) for c in credential_ids]
        for cred in allowed:
            args += ["--credential-id", b64e(cred)]
        payload = self._call(args)
        output = payload.get("output")
        if not output:
            raise AuthCapabilityError("the FIDO2 helper returned no hmac-secret output")
        used = b64d(payload.get("credential_id") or b64e(allowed[0]))
        if used not in allowed:
            raise AuthWrongCredentialError(
                "the helper answered with a credential that is not enrolled")
        self._check_uv(payload, "authentication")
        return used, sa_crypto.SecretBuffer(b64d(output))


# --------------------------------------------------------------------------
# fake-auth (deterministic, tests only)
# --------------------------------------------------------------------------
class FakeAuthProvider(SecureAuthProvider):
    """Deterministic provider for lifecycle tests. Never a production choice.

    :mod:`sa_config` refuses ``fake-auth`` unless the caller passed
    ``allow_test_providers=True``, so a registry that names it cannot be
    loaded by the shipped launcher.
    """

    name = "fake-auth"

    def __init__(self, seed=b"fake-authenticator-seed", hmac_secret=True,
                 present=True, credential_id=b"fake-credential-1",
                 user_verification=UV_DISCOURAGED,
                 user_verification_available=True,
                 performs_user_verification=True):
        self.seed = seed
        self.hmac_secret = hmac_secret
        self.present = present
        # Policy the profile asked for, and what this fake key can/will do
        # about it. Separating the two is what makes the "UV required but the
        # key cannot" refusal testable without hardware.
        if user_verification not in USER_VERIFICATION_VALUES:
            raise AuthError("unsupported user_verification %r"
                            % (user_verification,))
        self.user_verification = user_verification
        self.user_verification_available = user_verification_available
        self.performs_user_verification = performs_user_verification
        self.transport = TRANSPORT_DIRECT_CTAP
        # The credential the NEXT create_credential() hands out.
        self.credential_id = credential_id
        # Every credential this authenticator can currently assert. A real
        # second key answers for its own credential and not for the first
        # one, which is what makes multi-key enrollment worth testing.
        self.available_credentials = {bytes(credential_id)}
        self.cancel_next = False
        self.wrong_credential = False
        self.calls = []

    def use_only(self, credential_id):
        """Model a different physical key being plugged in."""
        self.credential_id = bytes(credential_id)
        self.available_credentials = {bytes(credential_id)}

    def capabilities(self):
        return Capabilities(self.name, available=self.present,
                            hmac_secret=self.hmac_secret,
                            authenticators=1 if self.present else 0,
                            detail="fake provider",
                            transport=(TRANSPORT_DIRECT_CTAP if self.present
                                       else TRANSPORT_UNAVAILABLE),
                            user_verification=self.user_verification_available,
                            library_version="fake")

    def _uv_or_refuse(self, where):
        """Mirror the real provider: UV required means UV proven."""
        if self.user_verification != UV_REQUIRED:
            return bool(self.performs_user_verification
                        and self.user_verification_available)
        if not self.user_verification_available:
            raise AuthCapabilityError(
                "this profile requires user verification but the fake "
                "authenticator reports none configured")
        if not self.performs_user_verification:
            raise AuthCapabilityError(
                "this profile requires user verification but the fake "
                "authenticator completed %s without it" % where)
        return True

    def create_credential(self, credential_profile, require_hmac_secret=True):
        self.calls.append(("create", credential_profile))
        if not self.present:
            raise AuthUnavailableError("no fake authenticator is connected")
        if require_hmac_secret and not self.hmac_secret:
            raise AuthCapabilityError(
                "this authenticator does not support the CTAP2 hmac-secret "
                "extension, so it cannot protect an encrypted vault")
        uv_performed = self._uv_or_refuse("enrollment")
        self.available_credentials.add(bytes(self.credential_id))
        return {
            "credential_id": self.credential_id,
            "rp_id": RP_ID,
            "user_id": b"fake-user",
            "provider": self.name,
            "transport": self.transport,
            "user_verification": bool(uv_performed),
        }

    def get_key_material(self, credential_ids, salt):
        self.calls.append(("hmac", len(credential_ids)))
        if not self.present:
            raise AuthUnavailableError("no fake authenticator is connected")
        if self.cancel_next:
            self.cancel_next = False
            raise AuthCancelledError("the fake FIDO2 operation was cancelled")
        if not self.hmac_secret:
            raise AuthCapabilityError("fake authenticator has no hmac-secret")
        allowed = [bytes(c) for c in credential_ids]
        match = None
        if not self.wrong_credential:
            for candidate in allowed:
                if candidate in self.available_credentials:
                    match = candidate
                    break
        if match is None:
            raise AuthWrongCredentialError(
                "the connected key does not hold this profile's credential")
        self._uv_or_refuse("authentication")
        import hashlib
        # Per-credential material, like a real authenticator: two keys
        # enrolled for one vault derive two different KEKs and therefore two
        # independent wrapped copies of the same volume secret.
        material = hashlib.sha256(self.seed + b"|" + match + b"|" + bytes(salt)).digest()
        return match, sa_crypto.SecretBuffer(material)


def _profile_user_verification(profile):
    return getattr(profile, "user_verification", UV_DISCOURAGED)


PROVIDER_FACTORIES = {
    Fido2HmacSecretProvider.name: lambda profile, helper=None: Fido2HmacSecretProvider(
        user_verification=_profile_user_verification(profile),
        helper=helper),
    ExternalHelperProvider.name: lambda profile, helper=None: ExternalHelperProvider(
        profile.helper_executable,
        user_verification=_profile_user_verification(profile)),
}


def build_provider(profile, test_providers=None, helper=None):
    """Instantiate the provider a profile declares.

    *test_providers* maps a provider name to an instance and is supplied only
    by tests and by the ``--fake`` developer path.
    """
    if test_providers and profile.provider in test_providers:
        injected = test_providers[profile.provider]
        # A registry's user-verification policy governs even an injected test
        # double, so a test that declares "required" really exercises it.
        injected.user_verification = _profile_user_verification(profile)
        if helper is not None and hasattr(injected, "helper") and getattr(injected, "helper", None) is None:
            injected.helper = helper
        return injected
    factory = PROVIDER_FACTORIES.get(profile.provider)
    if factory is None:
        raise AuthUnavailableError("no implementation for provider %r"
                                   % (profile.provider,))
    try:
        return factory(profile, helper=helper)
    except TypeError:
        return factory(profile)


# --------------------------------------------------------------------------
# enrollment store
# --------------------------------------------------------------------------
class Enrollment(object):
    """One wrapped copy of a volume secret, bound to one credential."""

    __slots__ = ("credential_profile", "provider", "credential_id", "rp_id",
                 "hmac_salt", "kdf_salt", "nonce", "ciphertext",
                 "aad_schema_version", "created", "hmac_transport_semantics")

    def __init__(self, credential_profile, provider, credential_id, rp_id,
                 hmac_salt, kdf_salt, nonce, ciphertext,
                 aad_schema_version=sa_crypto.SCHEMA_VERSION, created=None,
                 hmac_transport_semantics=DEFAULT_HMAC_TRANSPORT_SEMANTICS):
        self.credential_profile = credential_profile
        self.provider = provider
        self.credential_id = bytes(credential_id)
        self.rp_id = rp_id
        self.hmac_salt = bytes(hmac_salt)
        self.kdf_salt = bytes(kdf_salt)
        self.nonce = bytes(nonce)
        self.ciphertext = bytes(ciphertext)
        self.aad_schema_version = int(aad_schema_version)
        self.created = created or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.hmac_transport_semantics = str(hmac_transport_semantics or DEFAULT_HMAC_TRANSPORT_SEMANTICS)

    def to_dict(self):
        return {
            "credential_profile": self.credential_profile,
            "provider": self.provider,
            "credential_id": b64e(self.credential_id),
            "rp_id": self.rp_id,
            "hmac_salt": b64e(self.hmac_salt),
            "kdf_salt": b64e(self.kdf_salt),
            "nonce": b64e(self.nonce),
            "ciphertext": b64e(self.ciphertext),
            "aad_schema_version": self.aad_schema_version,
            "created": self.created,
            "hmac_transport_semantics": self.hmac_transport_semantics,
        }

    @classmethod
    def from_dict(cls, raw):
        return cls(
            credential_profile=raw["credential_profile"],
            provider=raw["provider"],
            credential_id=b64d(raw["credential_id"]),
            rp_id=raw.get("rp_id", RP_ID),
            hmac_salt=b64d(raw["hmac_salt"]),
            kdf_salt=b64d(raw["kdf_salt"]),
            nonce=b64d(raw["nonce"]),
            ciphertext=b64d(raw["ciphertext"]),
            aad_schema_version=int(raw.get("aad_schema_version",
                                           sa_crypto.SCHEMA_VERSION)),
            created=raw.get("created"),
            hmac_transport_semantics=raw.get("hmac_transport_semantics",
                                            DEFAULT_HMAC_TRANSPORT_SEMANTICS),
        )


class NotEnrolledError(AuthError):
    category = "not_enrolled"


class CredentialStore(object):
    """Ciphertext and public metadata only. Never a plaintext secret."""

    def __init__(self, path):
        self.path = path
        self._document = {"schema": CREDENTIALS_SCHEMA,
                          "schema_version": CREDENTIALS_SCHEMA_VERSION,
                          "profiles": {}}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError):
            raise AuthError("the credential store is unreadable or malformed: %s"
                            % self.path)
        if not isinstance(document, dict) or document.get("schema") != CREDENTIALS_SCHEMA:
            raise AuthError("unsupported credential store schema in %s" % self.path)
        if document.get("schema_version") != CREDENTIALS_SCHEMA_VERSION:
            raise AuthError("unsupported credential store schema_version in %s"
                            % self.path)
        self._document = document
        self._document.setdefault("profiles", {})

    def save(self):
        self._write(self._document)

    def _commit(self, candidate):
        """Persist *candidate*, and only then make it the in-memory truth.

        Every mutation is copy -> validate -> persist -> commit (SRC-027
        W2-007): a refusal or a failed write leaves memory and disk as they
        were, so the process never believes something the file does not say.
        """
        self._write(candidate)
        self._document = candidate

    def _candidate(self):
        return copy.deepcopy(self._document)

    def _write(self, document):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def entry(self, profile_id):
        return self._document["profiles"].get(profile_id)

    def enrollments(self, profile_id):
        entry = self.entry(profile_id)
        if not entry:
            return []
        return [Enrollment.from_dict(e) for e in entry.get("enrollments", [])]

    def container_id(self, profile_id):
        entry = self.entry(profile_id)
        return entry.get("container_id") if entry else None

    def is_enrolled(self, profile_id):
        return bool(self.enrollments(profile_id))

    def add(self, profile_id, container_id, enrollment):
        candidate = self._candidate()
        entry = candidate["profiles"].setdefault(
            profile_id, {"container_id": container_id, "enrollments": []})
        if entry.get("container_id") != container_id:
            raise AuthError(
                "profile %r is already enrolled against container %r; enrolling "
                "against %r would orphan the existing wrapped keys"
                % (profile_id, entry.get("container_id"), container_id))
        for existing in entry["enrollments"]:
            if existing["credential_id"] == b64e(enrollment.credential_id):
                raise AuthError("this credential is already enrolled for profile %r"
                                % profile_id)
        entry["enrollments"].append(enrollment.to_dict())
        self._commit(candidate)
        return enrollment

    def remove(self, profile_id, credential_id):
        entry = self.entry(profile_id)
        if not entry:
            raise NotEnrolledError("profile %r has no enrollments" % profile_id)
        target = b64e(credential_id)
        remaining = [e for e in entry["enrollments"]
                     if e["credential_id"] != target]
        if len(remaining) == len(entry["enrollments"]):
            raise NotEnrolledError("credential is not enrolled for profile %r"
                                   % profile_id)
        if not remaining:
            raise AuthError(
                "refusing to remove the last enrolled credential for profile %r: "
                "the vault would become unopenable except through its BitLocker "
                "recovery material" % profile_id)
        candidate = self._candidate()
        candidate["profiles"][profile_id]["enrollments"] = copy.deepcopy(remaining)
        self._commit(candidate)


def unwrap_volume_secret(profile, provider, store, audit=None):
    """Authenticate, derive the KEK, unwrap the volume secret.

    Returns ``(credential_id_bytes, SecretBuffer)``. Every intermediate secret
    is zeroized before returning, whatever the outcome.
    """
    enrollments = store.enrollments(profile.id)
    if not enrollments:
        raise NotEnrolledError(
            "profile %r has no enrolled FIDO2 credential; run 'Manage key' first"
            % profile.id)
    container_id = store.container_id(profile.id) or profile.container_id
    last_error = None
    # Enrollments are grouped by hmac-secret salt so that every credential
    # sharing one salt is offered to the authenticator in a single assertion
    # -- one allowList, one touch. New enrollments reuse the profile's
    # existing salt, so in practice there is exactly one group.
    by_salt = {}
    for enrollment in enrollments:
        by_salt.setdefault(enrollment.hmac_salt, []).append(enrollment)
    for hmac_salt, group in by_salt.items():
        credential_ids = [e.credential_id for e in group]
        try:
            used_id, material = provider.get_key_material(credential_ids, hmac_salt)
        except (AuthCancelledError, AuthUnavailableError):
            # The user said no, or there is no key at all. Trying the next
            # salt group would just prompt again for the same refusal.
            raise
        except AuthError as exc:
            # This group's credentials are not on the connected key. Another
            # enrolled key may still be, so keep looking before giving up.
            last_error = exc
            continue
        try:
            chosen = None
            for enrollment in group:
                if enrollment.credential_id == bytes(used_id):
                    chosen = enrollment
                    break
            if chosen is None:
                last_error = AuthWrongCredentialError(
                    "the connected key does not hold this profile's credential")
                continue
            if getattr(chosen, "hmac_transport_semantics", None) == "direct-ctap-v1" and provider.transport == TRANSPORT_WINDOWS_WEBAUTHN:
                raise AuthCapabilityError(
                    "credential was enrolled with direct CTAP semantics ('%s'); "
                    "refusing cross-transport unlock via %s until cross-transport equivalence has been proven"
                    % (chosen.hmac_transport_semantics, provider.transport))
            kek = sa_crypto.derive_kek(material, chosen.kdf_salt, profile.id,
                                       container_id)
            try:
                aad = sa_crypto.build_aad(profile.id, container_id,
                                          b64e(chosen.credential_id),
                                          chosen.aad_schema_version)
                secret = sa_crypto.unwrap_secret(kek, chosen.nonce,
                                                 chosen.ciphertext, aad)
            finally:
                kek.zeroize()
            return chosen.credential_id, secret
        except sa_crypto.UnwrapError as exc:
            last_error = exc
        finally:
            material.zeroize()
    raise last_error or AuthError("no enrolled credential could unwrap the vault key")


def wrap_volume_secret_for(profile, provider, credential, volume_secret,
                           container_id=None, hmac_salt=None):
    """Produce one :class:`Enrollment` binding *credential* to *volume_secret*."""
    container = container_id or profile.container_id
    hmac_salt = hmac_salt or os.urandom(HMAC_SECRET_SALT_BYTES)
    kdf_salt = sa_crypto.new_salt()
    credential_id = bytes(credential["credential_id"])
    used_id, material = provider.get_key_material([credential_id], hmac_salt)
    try:
        if bytes(used_id) != credential_id:
            raise AuthWrongCredentialError(
                "the authenticator answered with a different credential than "
                "the one just created")
        kek = sa_crypto.derive_kek(material, kdf_salt, profile.id, container)
        try:
            aad = sa_crypto.build_aad(profile.id, container, b64e(credential_id))
            nonce, ciphertext = sa_crypto.wrap_secret(kek, volume_secret, aad)
        finally:
            kek.zeroize()
    finally:
        material.zeroize()
    semantics = credential.get("hmac_transport_semantics", DEFAULT_HMAC_TRANSPORT_SEMANTICS)
    return Enrollment(
        credential_profile=profile.credential_profile,
        provider=provider.name,
        credential_id=credential_id,
        rp_id=credential.get("rp_id", RP_ID),
        hmac_salt=hmac_salt,
        kdf_salt=kdf_salt,
        nonce=nonce,
        ciphertext=ciphertext,
        hmac_transport_semantics=semantics,
    )
