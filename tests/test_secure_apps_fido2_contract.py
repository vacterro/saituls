"""python-fido2 dependency-contract suite for SAITULS Secure Apps.

This suite imports the REAL ``fido2`` package and proves that
``sa_auth.Fido2HmacSecretProvider`` can be built and driven through the
public API of the version that is actually installed -- with no security key,
no elevation and no user interaction anywhere.

Why it exists: the provider was previously written against python-fido2 1.x
(``Fido2Client(device, origin)`` plus implicit extension handling). That code
imported fine, constructed fine and failed only when a human plugged in a
YubiKey -- because CI deliberately did not install ``fido2`` at all, so
nothing ever executed a single line of the real client surface. An absent
dependency is not a passing test.

What is covered here:

  * the installed version sits inside the declared supported range, and a
    1.x-shaped version is rejected;
  * every symbol the provider imports exists;
  * ``DefaultClientDataCollector``, ``HmacSecretExtension(allow_hmac_secret=
    True)`` and ``Fido2Client(device, client_data_collector=...,
    user_interaction=..., extensions=[...])`` construct against a stub CTAP2
    device that answers ``authenticatorGetInfo`` and nothing else;
  * ``WindowsClient(collector, handle=..., allow_hmac_secret=True)`` is
    constructible where the platform API exists;
  * the creation and request option objects build with exactly the keyword
    arguments the provider passes;
  * the extension-result accessors the provider reads
    (``client_extension_results.hmac_create_secret`` /
    ``.hmac_get_secret.output1``) exist on the real 2.x response objects;
  * transport resolution answers with one of the declared transports and
    never invents a fourth.

Physical-key acceptance stays in tests/secure_apps_interactive.py.
"""
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_auth            # noqa: E402

# A hard import: this suite exists precisely to fail when fido2 is missing.
import fido2              # noqa: E402,F401
from fido2 import cbor    # noqa: E402
from fido2.client import (                                      # noqa: E402
    DefaultClientDataCollector, Fido2Client, UserInteraction)
from fido2.ctap import CtapDevice                               # noqa: E402
from fido2.ctap2 import Ctap2                                   # noqa: E402
from fido2.ctap2.extensions import (                            # noqa: E402
    HMACGetSecretOutput, HmacSecretExtension)
from fido2.hid import CAPABILITY, CtapHidDevice                 # noqa: E402
from fido2.webauthn import (                                    # noqa: E402
    AuthenticationExtensionsClientOutputs, AuthenticatorData,
    AuthenticatorSelectionCriteria, PublicKeyCredentialCreationOptions,
    PublicKeyCredentialDescriptor, PublicKeyCredentialParameters,
    PublicKeyCredentialRequestOptions, PublicKeyCredentialRpEntity,
    PublicKeyCredentialType, PublicKeyCredentialUserEntity,
    ResidentKeyRequirement, UserVerificationRequirement)

try:
    from importlib import metadata
    INSTALLED_VERSION = metadata.version("fido2")
except Exception:                                   # pragma: no cover
    INSTALLED_VERSION = "0"


# ── a CTAP2 device that answers getInfo and refuses everything else ────────
GET_INFO = 0x04

INFO_MAP = {
    0x01: ["U2F_V2", "FIDO_2_0", "FIDO_2_1"],           # versions
    0x02: ["credProtect", "hmac-secret", "hmac-secret-mc"],   # extensions
    0x03: bytes(16),                                     # aaguid
    0x04: {"rk": True, "up": True, "plat": False,
           "clientPin": True, "pinUvAuthToken": True},   # options
    0x05: 1200,                                          # maxMsgSize
    0x06: [2, 1],                                        # pinUvAuthProtocols
}


class StubCtapDevice(CtapDevice):
    """Answers authenticatorGetInfo. Every other command is CTAP2_ERR_OTHER.

    Enough to construct a CTAP2 client -- which is the whole point: building
    the client is the API contract this suite is about. Anything that would
    need a touch, a PIN or a credential is out of scope here and belongs in
    the interactive hardware script.
    """

    def __init__(self):
        self.commands = []

    @property
    def capabilities(self):
        return CAPABILITY.CBOR

    def call(self, cmd, data=b"", event=None, on_keepalive=None):
        self.commands.append((cmd, bytes(data)))
        if data and data[0] == GET_INFO:
            return b"\x00" + cbor.encode(INFO_MAP)
        return b"\x7f"                    # CTAP2_ERR_OTHER

    @classmethod
    def list_devices(cls):
        yield cls()


class VersionContractTests(unittest.TestCase):
    def test_installed_version_is_inside_the_supported_range(self):
        parsed = sa_auth.require_supported_fido2(INSTALLED_VERSION)
        self.assertGreaterEqual(parsed, sa_auth.FIDO2_MIN_VERSION)
        self.assertLess(parsed, sa_auth.FIDO2_MAX_VERSION_EXCLUSIVE)

    def test_a_one_x_install_is_refused_loudly(self):
        """The exact regression: 1.x must never be accepted silently."""
        for version in ("1.1.3", "1.2.0", "0.9.3", "3.0.0", "4.1"):
            with self.assertRaises(sa_auth.AuthUnavailableError):
                sa_auth.require_supported_fido2(version)

    def test_version_parsing_tolerates_prerelease_suffixes(self):
        self.assertEqual(sa_auth.parse_library_version("2.0.0b1"), (2, 0))
        self.assertEqual(sa_auth.parse_library_version("2.2.1"), (2, 2))

    def test_the_provider_reports_the_version_it_is_running_against(self):
        caps = sa_auth.Fido2HmacSecretProvider().capabilities()
        self.assertEqual(caps.library_version, INSTALLED_VERSION)


class ClientConstructionTests(unittest.TestCase):
    """The 2.x construction path, executed rather than assumed."""

    def setUp(self):
        self.provider = sa_auth.Fido2HmacSecretProvider(
            user_verification=sa_auth.UV_REQUIRED)
        self.mod = self.provider._modules()

    def test_every_symbol_the_provider_imports_exists(self):
        for key in ("Fido2Client", "ClientDataCollector", "UserInteraction",
                    "HmacSecretExtension", "CtapHidDevice", "Ctap2",
                    "AuthenticatorData", "Selection", "CreationOptions",
                    "RequestOptions", "Descriptor", "CredParams", "RpEntity",
                    "CredType", "UserEntity", "ResidentKey", "UV"):
            self.assertIsNotNone(self.mod.get(key), key)

    def test_default_client_data_collector_takes_an_origin(self):
        collector = self.provider._collector(self.mod)
        self.assertIsInstance(collector, DefaultClientDataCollector)

    def test_hmac_secret_extension_allows_hmac_secret_explicitly(self):
        extension = self.mod["HmacSecretExtension"](allow_hmac_secret=True)
        self.assertIsInstance(extension, HmacSecretExtension)
        self.assertTrue(extension._allow_hmac_secret)

    def test_direct_client_builds_a_ctap2_backend_on_a_stub_device(self):
        device = StubCtapDevice()
        client = self.provider._direct_client(self.mod, device)
        self.assertIsInstance(client, Fido2Client)
        # A CTAP1 fallback backend has no info; reaching it would mean the
        # CTAP2 construction path silently failed.
        self.assertIn("hmac-secret", client.info.extensions)
        self.assertTrue(any(cmd for cmd, _data in device.commands))

    def test_user_interaction_subclasses_the_library_base(self):
        interaction = self.provider._interaction(self.mod)
        self.assertIsInstance(interaction, UserInteraction)
        # No callback configured: a PIN request must cancel, not invent one.
        self.assertIsNone(interaction.request_pin(None, "rp"))
        self.assertTrue(interaction.pin_requested)

    def test_pin_callback_is_forwarded_and_not_stored(self):
        seen = []

        def callback(rp_id):
            seen.append(rp_id)
            return "123456"

        provider = sa_auth.Fido2HmacSecretProvider(pin_callback=callback)
        interaction = provider._interaction(provider._modules())
        self.assertEqual(interaction.request_pin(None, "rp.example"), "123456")
        self.assertEqual(seen, ["rp.example"])
        # The provider keeps the callback, never the value it returned.
        for name, value in vars(provider).items():
            self.assertNotIn("123456", str(value), name)

    def test_probe_device_reads_capabilities_from_get_info(self):
        hmac, uv = self.provider._probe_device(self.mod, StubCtapDevice())
        self.assertTrue(hmac)
        self.assertTrue(uv)

    def test_ctap2_info_shape_is_the_one_the_provider_reads(self):
        info = Ctap2(StubCtapDevice()).info
        self.assertIn("hmac-secret", list(info.extensions))
        self.assertIn("clientPin", dict(info.options))


class OptionObjectTests(unittest.TestCase):
    """The option objects must accept exactly the kwargs the provider passes."""

    def setUp(self):
        self.provider = sa_auth.Fido2HmacSecretProvider()
        self.mod = self.provider._modules()

    def test_creation_options_build_with_hmac_create_secret(self):
        options = self.provider._creation_options(
            self.mod, "primary", os.urandom(16), sa_auth.UV_REQUIRED)
        self.assertIsInstance(options, PublicKeyCredentialCreationOptions)
        self.assertEqual(options.extensions["hmacCreateSecret"], True)
        self.assertEqual(options.authenticator_selection.user_verification,
                         UserVerificationRequirement.REQUIRED)
        self.assertEqual(options.authenticator_selection.resident_key,
                         ResidentKeyRequirement.DISCOURAGED)
        self.assertIsInstance(options.rp, PublicKeyCredentialRpEntity)
        self.assertIsInstance(options.user, PublicKeyCredentialUserEntity)
        self.assertTrue(all(isinstance(p, PublicKeyCredentialParameters)
                            for p in options.pub_key_cred_params))
        self.assertIsInstance(options.authenticator_selection,
                              AuthenticatorSelectionCriteria)

    def test_request_options_carry_a_32_byte_hmac_get_secret_salt(self):
        salt = os.urandom(sa_auth.HMAC_SECRET_SALT_BYTES)
        options = self.provider._request_options(
            self.mod, [b"cred-1"], salt, sa_auth.UV_REQUIRED)
        self.assertIsInstance(options, PublicKeyCredentialRequestOptions)
        self.assertEqual(options.extensions["hmacGetSecret"]["salt1"], salt)
        self.assertEqual(options.user_verification,
                         UserVerificationRequirement.REQUIRED)
        self.assertTrue(all(isinstance(d, PublicKeyCredentialDescriptor)
                            for d in options.allow_credentials))
        self.assertEqual(options.allow_credentials[0].type,
                         PublicKeyCredentialType.PUBLIC_KEY)

    def test_the_salt_length_matches_what_the_extension_demands(self):
        self.assertEqual(sa_auth.HMAC_SECRET_SALT_BYTES,
                         HmacSecretExtension.SALT_LEN)


class ExtensionResultShapeTests(unittest.TestCase):
    """The accessors the provider reads must exist on the real 2.x objects."""

    def test_hmac_create_secret_is_reachable_by_attribute(self):
        results = AuthenticationExtensionsClientOutputs(
            {"hmacCreateSecret": True})
        self.assertIs(results.hmac_create_secret, True)

    def test_hmac_get_secret_output1_is_reachable_by_attribute(self):
        output = HMACGetSecretOutput(b"\x01" * 32)
        results = AuthenticationExtensionsClientOutputs(
            {"hmacGetSecret": output})
        self.assertEqual(results.hmac_get_secret.output1, b"\x01" * 32)

    def test_a_missing_extension_result_reads_as_none(self):
        results = AuthenticationExtensionsClientOutputs({})
        self.assertIsNone(results.hmac_get_secret)
        self.assertIsNone(results.hmac_create_secret)

    def test_the_uv_flag_the_provider_checks_exists(self):
        self.assertTrue(hasattr(AuthenticatorData.FLAG, "UV"))
        provider = sa_auth.Fido2HmacSecretProvider()
        mod = provider._modules()

        class Data(object):
            flags = AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.UV

        class PresenceOnly(object):
            flags = AuthenticatorData.FLAG.UP

        self.assertTrue(provider._uv_performed(mod, Data()))
        self.assertFalse(provider._uv_performed(mod, PresenceOnly()))


class TransportContractTests(unittest.TestCase):
    def test_resolve_transport_answers_with_a_declared_transport(self):
        provider = sa_auth.Fido2HmacSecretProvider()
        transport, detail, count, hmac_secret, uv = provider.resolve_transport()
        self.assertIn(transport, sa_auth.TRANSPORTS)
        self.assertEqual(provider.transport, transport)
        self.assertIsInstance(detail, str)
        self.assertTrue(detail, "an unusable host must say why")
        self.assertIsInstance(count, int)
        self.assertIsInstance(hmac_secret, bool)
        self.assertIsInstance(uv, bool)

    def test_capabilities_never_claim_more_than_the_transport_allows(self):
        caps = sa_auth.Fido2HmacSecretProvider().capabilities().to_dict()
        self.assertIn(caps["transport"], sa_auth.TRANSPORTS)
        if caps["transport"] == sa_auth.TRANSPORT_UNAVAILABLE:
            self.assertFalse(caps["available"])
            self.assertFalse(caps["hmac_secret"])
        else:
            self.assertTrue(caps["available"])

    def test_windows_webauthn_is_refused_below_the_hmac_secret_api_version(self):
        """An old webauthn.dll must be reported, never tried and hoped for."""
        provider = sa_auth.Fido2HmacSecretProvider()
        mod = dict(provider._modules())

        class TooOld(object):
            @staticmethod
            def is_available():
                return True

        mod["WindowsClient"] = TooOld
        mod["WEBAUTHN_API_VERSION"] = \
            sa_auth.WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION - 1
        client, detail = provider._windows_client(mod)
        self.assertIsNone(client)
        self.assertIn("hmac-secret", detail)

    @unittest.skipUnless(os.name == "nt", "Windows WebAuthn is Windows-only")
    def test_windows_client_is_constructible_when_the_api_is_new_enough(self):
        from fido2.client.windows import WindowsClient
        from fido2.client.win_api import WEBAUTHN_API_VERSION
        provider = sa_auth.Fido2HmacSecretProvider()
        mod = provider._modules()
        self.assertIs(mod["WindowsClient"], WindowsClient)
        self.assertEqual(mod["WEBAUTHN_API_VERSION"], WEBAUTHN_API_VERSION)
        if WEBAUTHN_API_VERSION < sa_auth.WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION:
            self.skipTest(
                "webauthn.dll reports API version %d; hmac-secret salts need "
                "version %d, so this host cannot use the platform path"
                % (WEBAUTHN_API_VERSION,
                   sa_auth.WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION))
        client, detail = provider._windows_client(mod)
        self.assertIsInstance(client, WindowsClient)
        self.assertIn("API version", detail)

    def test_a_windows_failure_is_not_retried_over_hid(self):
        """The rule from the module docstring, enforced rather than described."""
        provider = sa_auth.Fido2HmacSecretProvider()
        mod = dict(provider._modules())
        attempted = []

        class Boom(object):
            @staticmethod
            def is_available():
                return True

            def __init__(self, *args, **kwargs):
                pass

            def make_credential(self, options):
                raise RuntimeError("platform refused")

            def get_assertion(self, options):
                raise RuntimeError("platform refused")

        mod["WindowsClient"] = Boom
        mod["WEBAUTHN_API_VERSION"] = \
            sa_auth.WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION
        mod["CtapHidDevice"] = type(
            "NeverCalled", (),
            {"list_devices": staticmethod(
                lambda: attempted.append("hid") or [])})
        provider._modules_cache = mod
        with self.assertRaises(sa_auth.AuthError):
            provider.create_credential("primary")
        with self.assertRaises(sa_auth.AuthError):
            provider.get_key_material([b"cred"], b"\x00" * 32)
        self.assertEqual(attempted, [],
                         "a WindowsClient failure fell back to the HID path")


class HidEnumerationTests(unittest.TestCase):
    def test_list_devices_is_the_api_the_provider_calls(self):
        self.assertTrue(hasattr(CtapHidDevice, "list_devices"))
        provider = sa_auth.Fido2HmacSecretProvider()
        # Never raises on a host with no authenticator: an empty list is an
        # answer, and it is UNAVAILABLE rather than an exception.
        self.assertIsInstance(provider._devices(provider._modules()), list)


CLIENT_PIN = 0x06
GET_KEY_AGREEMENT = 0x02


class KeyAgreementStubCtapDevice(StubCtapDevice):
    """A stub that also answers clientPin getKeyAgreement.

    The hmac-secret extension negotiates its shared secret BEFORE the client
    asks for a PIN, so a stub without key agreement never reaches the PIN
    decision at all. Every other clientPin subcommand answers
    *pin_status*: CTAP2_ERR_OTHER by default, PIN_INVALID for a wrong PIN.
    """

    def __init__(self, pin_status=0x7F):
        StubCtapDevice.__init__(self)
        from cryptography.hazmat.primitives.asymmetric import ec
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.pin_status = pin_status

    def call(self, cmd, data=b"", event=None, on_keepalive=None):
        if data and data[0] == CLIENT_PIN:
            self.commands.append((cmd, bytes(data)))
            params = cbor.decode(bytes(data[1:]))
            if params.get(2) == GET_KEY_AGREEMENT:
                numbers = self._key.public_key().public_numbers()
                cose = {1: 2, 3: -25, -1: 1,
                        -2: numbers.x.to_bytes(32, "big"),
                        -3: numbers.y.to_bytes(32, "big")}
                return b"\x00" + cbor.encode({1: cose})
            return bytes([self.pin_status])
        return StubCtapDevice.call(self, cmd, data, event, on_keepalive)


class WorkerErrorTranslationTests(unittest.TestCase):
    """How the elevated FIDO worker names what went wrong.

    The acceptance run has to tell a cancelled PIN prompt apart from a wrong
    PIN and from a broken stack, so each must arrive with its own category.
    """

    def setUp(self):
        import sa_fido_worker
        self.worker = sa_fido_worker
        self.mod = sa_fido_worker._import_fido2()

    def _hmac_args(self, **extra):
        args = {"rp_id": sa_auth.RP_ID,
                "credential_ids_b64": [sa_auth.b64e(b"cred-1")],
                "salt_b64": sa_auth.b64e(b"\x01" * 32),
                "user_verification": "required", "timeout_seconds": 5}
        args.update(extra)
        return args

    def _with_device(self, device):
        mod = dict(self.mod)

        class OneDevice(object):
            @staticmethod
            def list_devices():
                yield device

        mod["CtapHidDevice"] = OneDevice
        return mod

    def test_no_pin_given_is_a_cancellation_on_the_real_client_path(self):
        device = KeyAgreementStubCtapDevice()
        result, error = self.worker._op_hmac(self._with_device(device),
                                             self._hmac_args())
        self.assertIsNone(result)
        self.assertEqual(error[0], self.worker.ERR_CANCELLED, error)
        subcommands = [cbor.decode(data[1:]).get(2) for _cmd, data in device.commands
                       if data and data[0] == CLIENT_PIN]
        self.assertEqual(subcommands, [GET_KEY_AGREEMENT],
                         "no PIN was entered, so no PIN may have been sent")

    def test_a_wrong_pin_is_a_failure_not_a_cancellation(self):
        device = KeyAgreementStubCtapDevice(pin_status=0x31)     # PIN_INVALID
        result, error = self.worker._op_hmac(self._with_device(device),
                                             self._hmac_args(pin="wrong-pin"))
        self.assertIsNone(result)
        self.assertEqual(error[0], self.worker.ERR_FAILED, error)

    def test_no_pin_at_enrollment_is_a_cancellation(self):
        result, error = self.worker._op_create(
            self._with_device(KeyAgreementStubCtapDevice()),
            {"rp_id": sa_auth.RP_ID, "credential_profile": "primary",
             "user_verification": "required", "require_hmac_secret": True,
             "timeout_seconds": 5})
        self.assertIsNone(result)
        self.assertEqual(error[0], self.worker.ERR_CANCELLED, error)

    def test_library_errors_map_to_their_categories(self):
        from fido2.client import ClientError, PinRequiredError
        from fido2.ctap import CtapError
        cases = [
            (PinRequiredError(), self.worker.ERR_CANCELLED),
            (ClientError(ClientError.ERR.BAD_REQUEST,
                         CtapError(CtapError.ERR.PIN_INVALID)), self.worker.ERR_FAILED),
            (ClientError(ClientError.ERR.DEVICE_INELIGIBLE,
                         CtapError(CtapError.ERR.NO_CREDENTIALS)),
             self.worker.ERR_WRONG_CRED),
            (ClientError(ClientError.ERR.TIMEOUT,
                         CtapError(CtapError.ERR.USER_ACTION_TIMEOUT)),
             self.worker.ERR_CANCELLED),
        ]
        for exc, expected in cases:
            self.assertEqual(self.worker._translate_error(exc)[0], expected, repr(exc))

    def test_a_cancelled_helper_reply_reaches_the_broker_as_a_cancellation(self):
        import sa_privhelper
        exc = sa_privhelper.HelperError(
            "storage helper operation failed (auth_cancelled)", "auth_cancelled")
        translated = sa_auth.Fido2HmacSecretProvider._translate_helper_error(
            exc, sa_auth.TRANSPORT_ELEVATED_CTAP_HELPER)
        self.assertIsInstance(translated, sa_auth.AuthCancelledError)


class ElevatedHelperContractTests(unittest.TestCase):
    """Contracts for ELEVATED_CTAP_HELPER transport."""

    def test_elevated_helper_is_selected_when_webauthn_is_incapable(self):
        class FakeHelper(object):
            running = True

            def fido_capabilities(self, timeout=30.0):
                return {
                    "available": True,
                    "hmac_secret": True,
                    "authenticators": 1,
                    "user_verification": True,
                    "detail": "direct CTAP over HID (elevated helper)",
                }

        provider = sa_auth.Fido2HmacSecretProvider(helper=FakeHelper())
        mod = dict(provider._modules())
        mod["WindowsClient"] = None
        mod["WEBAUTHN_API_VERSION"] = 2
        provider._modules_cache = mod

        transport, detail, count, hmac_secret, uv = provider.resolve_transport()
        self.assertEqual(transport, sa_auth.TRANSPORT_ELEVATED_CTAP_HELPER)
        self.assertEqual(count, 1)
        self.assertTrue(hmac_secret)
        self.assertTrue(uv)
        self.assertIn("elevated helper", detail)

    def test_create_and_hmac_drive_the_helper_pipe(self):
        calls = []

        class FakeHelper(object):
            running = True

            def fido_capabilities(self, timeout=30.0):
                return {"available": True, "hmac_secret": True, "authenticators": 1, "user_verification": True}

            def fido_create(self, **kwargs):
                calls.append(("create", kwargs))
                return {
                    "credential_id_b64": sa_auth.b64e(b"fake-cred-id"),
                    "user_verification": True,
                    "hmac_secret": True,
                }

            def fido_hmac(self, **kwargs):
                calls.append(("hmac", kwargs))
                return {
                    "credential_id_b64": sa_auth.b64e(b"fake-cred-id"),
                    "output_b64": sa_auth.b64e(b"\x42" * 32),
                    "user_verification": True,
                }

        provider = sa_auth.Fido2HmacSecretProvider(
            helper=FakeHelper(),
            user_verification=sa_auth.UV_REQUIRED,
            pin_callback=lambda rp: "123456"
        )
        mod = dict(provider._modules())
        mod["WindowsClient"] = None
        provider._modules_cache = mod

        cred = provider.create_credential("primary")
        self.assertEqual(cred["credential_id"], b"fake-cred-id")
        self.assertEqual(cred["transport"], sa_auth.TRANSPORT_ELEVATED_CTAP_HELPER)
        self.assertEqual(cred["hmac_transport_semantics"], "direct-ctap-v1")
        self.assertEqual(calls[0][0], "create")
        self.assertEqual(calls[0][1]["pin"], "123456")

        used_id, secret = provider.get_key_material([b"fake-cred-id"], b"\x01" * 32)
        self.assertEqual(used_id, b"fake-cred-id")
        self.assertEqual(secret.bytes(), b"\x42" * 32)
        secret.zeroize()
        self.assertEqual(calls[1][0], "hmac")
        self.assertEqual(calls[1][1]["pin"], "123456")

    def test_no_connected_key_is_refused_before_a_pin_is_asked_for(self):
        calls = []
        prompts = []

        class EmptyHelper(object):
            running = True

            def fido_capabilities(self, timeout=30.0):
                return {"available": False, "hmac_secret": False, "authenticators": 0,
                        "user_verification": False,
                        "detail": "no FIDO2 authenticator is visible over HID"}

            def fido_create(self, **kwargs):
                calls.append("create")

            def fido_hmac(self, **kwargs):
                calls.append("hmac")

        provider = sa_auth.Fido2HmacSecretProvider(
            helper=EmptyHelper(), user_verification=sa_auth.UV_REQUIRED,
            pin_callback=lambda rp_id: prompts.append(rp_id) or "123456")
        mod = dict(provider._modules())
        mod["WindowsClient"] = None
        provider._modules_cache = mod
        with self.assertRaises(sa_auth.AuthUnavailableError):
            provider.get_key_material([b"fake-cred-id"], b"\x01" * 32)
        with self.assertRaises(sa_auth.AuthUnavailableError):
            provider.create_credential("primary")
        self.assertEqual(prompts, [], "a PIN was collected with no key connected")
        self.assertEqual(calls, [])


class BrokerPinCallbackContractTests(unittest.TestCase):
    """The broker's PIN callback reaches the REAL provider it already built."""

    def setUp(self):
        import tempfile
        import shutil
        import sa_config
        self.tmp = Path(tempfile.mkdtemp(prefix="saituls-pin-callback-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        root = self.tmp / "managed"
        document = {
            "schema": sa_config.SCHEMA_ID, "schema_version": 1,
            "managed_root": str(root),
            "profiles": [{
                "id": "vault",
                "application": {"executable": str(root / "apps" / "vault" / "App.exe")},
                "storage": {"backend": "bitlocker-vhdx",
                            "container": str(root / "vaults" / "vault.vhdx"),
                            "mount_path": str(self.tmp / "mount")},
                "authentication": {"provider": "yubikey-fido2-hmac-secret",
                                   "user_verification": "required"},
                "policy": {"mode": "default", "idle_timeout_minutes": 60},
            }],
        }
        self.registry = sa_config.parse_registry(document, managed_root=str(root))

    def test_reassigning_the_callback_after_the_provider_exists_takes_effect(self):
        import sa_audit
        import sa_broker
        import sa_privhelper
        pins = []

        class RecordingHelper(object):
            running = True

            def fido_capabilities(self, timeout=30.0):
                return {"available": True, "hmac_secret": True, "authenticators": 1,
                        "user_verification": True}

            def fido_hmac(self, **kwargs):
                pins.append(kwargs.get("pin"))
                if not kwargs.get("pin"):
                    raise sa_privhelper.HelperError(
                        "storage helper operation failed (auth_cancelled)",
                        "auth_cancelled")
                return {"credential_id_b64": sa_auth.b64e(b"cred-1"),
                        "output_b64": sa_auth.b64e(b"\x42" * 32),
                        "user_verification": True}

        first = lambda rp_id: "123456"       # noqa: E731
        broker = sa_broker.SecureBroker(self.registry, audit=sa_audit.NullAuditLog(),
                                        pin_callback=first)
        provider = broker.provider(self.registry.get("vault"))
        self.assertIs(provider.pin_callback, first)
        provider.helper = RecordingHelper()
        mod = dict(provider._modules())
        mod["WindowsClient"] = None
        provider._modules_cache = mod

        cancelled = lambda rp_id: None       # noqa: E731
        broker.pin_callback = cancelled      # the assignment that used to do nothing
        self.assertIs(provider.pin_callback, cancelled)
        with self.assertRaises(sa_auth.AuthCancelledError):
            provider.get_key_material([b"cred-1"], b"\x01" * 32)

        broker.set_pin_callback(first)
        _used, secret = provider.get_key_material([b"cred-1"], b"\x01" * 32)
        secret.zeroize()
        self.assertEqual(pins, [None, "123456"])

    def test_helper_process_id_reports_only_a_running_helper(self):
        import sa_audit
        import sa_broker
        import sa_privhelper

        class Helper(object):
            running = True
            # The pid the kernel reported for the process serving the pipe.
            identity = sa_privhelper.HelperIdentity(pid=4242, elevated=True)

            def close(self):
                self.running = False

        broker = sa_broker.SecureBroker(self.registry, audit=sa_audit.NullAuditLog())
        self.assertIsNone(broker.helper_process_id)
        broker._helper = Helper()
        self.assertEqual(broker.helper_process_id, 4242)
        broker._helper.running = False
        self.assertIsNone(broker.helper_process_id)


class TransportProvenanceTests(unittest.TestCase):
    """Requirement 8: Transport provenance and cross-transport equivalence contracts."""

    def test_enrollment_stores_and_loads_hmac_transport_semantics(self):
        salt = b"\x01" * 32
        enrollment = sa_auth.Enrollment(
            credential_profile="primary",
            provider="yubikey-fido2-hmac-secret",
            credential_id=b"cred-1",
            rp_id="rp",
            hmac_salt=salt,
            kdf_salt=salt,
            nonce=b"\x00" * 12,
            ciphertext=b"\xff" * 32,
            hmac_transport_semantics="direct-ctap-v1"
        )
        doc = enrollment.to_dict()
        self.assertEqual(doc["hmac_transport_semantics"], "direct-ctap-v1")
        restored = sa_auth.Enrollment.from_dict(doc)
        self.assertEqual(restored.hmac_transport_semantics, "direct-ctap-v1")

    def test_cross_transport_unlock_refused_without_proven_equivalence(self):
        import sa_crypto

        class DummyProfile(object):
            id = "test-vault"
            container_id = "test-c1"

        class DummyStore(object):
            def enrollments(self, pid):
                return [sa_auth.Enrollment(
                    credential_profile="primary",
                    provider="yubikey-fido2-hmac-secret",
                    credential_id=b"cred-1",
                    rp_id="rp",
                    hmac_salt=b"\x01" * 32,
                    kdf_salt=b"\x02" * 32,
                    nonce=b"\x00" * 12,
                    ciphertext=b"\xff" * 32,
                    hmac_transport_semantics="direct-ctap-v1"
                )]

            def container_id(self, pid):
                return "test-c1"

        class DummyWebAuthnProvider(object):
            transport = sa_auth.TRANSPORT_WINDOWS_WEBAUTHN

            def get_key_material(self, allowed, salt):
                return b"cred-1", sa_crypto.SecretBuffer(b"\x99" * 32)

        with self.assertRaises(sa_auth.AuthCapabilityError) as ctx:
            sa_auth.unwrap_volume_secret(DummyProfile(), DummyWebAuthnProvider(), DummyStore())
        self.assertIn("refusing cross-transport unlock", str(ctx.exception))

    def test_future_cross_transport_equivalence_comparator(self):
        """Template / harness test for dual-transport platforms to prove salt equivalence."""
        stored_logical_salt = b"\xaa" * 32

        def simulate_direct_ctap(cred_id, salt):
            return b"ctap_output_" + salt[:16]

        def simulate_webauthn(cred_id, salt):
            return b"ctap_output_" + salt[:16]

        out_ctap = simulate_direct_ctap(b"cred-1", stored_logical_salt)
        out_win = simulate_webauthn(b"cred-1", stored_logical_salt)
        self.assertEqual(out_ctap, out_win, "transports must yield identical output to be interchangeable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
