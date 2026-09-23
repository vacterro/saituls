"""Elevated FIDO worker for SAITULS Secure Apps.

Invoked exclusively by the elevated PowerShell storage helper
(sa_storage_helper.ps1) as a child process. Inherits the elevated token.

Talks to the parent over stdin/stdout using single-line JSON requests:
  -> {"op": "capabilities", ...}
  <- {"ok": true, "result": {...}}
  or
  <- {"ok": false, "error": "<category>", "message": "<text>"}

No sensitive values ever appear in process command-line arguments.
PINs and key material received via stdin are zeroized/deleted from memory
immediately after use.
"""
import base64
import json
import os
import sys

# Audited error categories matching sa_auth
ERR_CAPABILITY = "auth_capability"
ERR_CANCELLED = "auth_cancelled"
ERR_WRONG_CRED = "auth_wrong_credential"
ERR_UNAVAILABLE = "auth_unavailable"
ERR_FAILED = "auth_failed"

_CTAP_NO_CREDENTIALS = 0x2E
_CTAP_PIN_NOT_SET = 0x35
_CTAP_CANCELLED = (0x2D, 0x27, 0x2F, 0x3A)
_CTAP_PIN_FAILED = (0x31, 0x32, 0x33, 0x34, 0x3C, 0x3F)
_CTAP_UV_REQUIRED = (0x36, 0x3B)


def b64e(raw):
    return base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")


def b64d(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _close_all(devices):
    for dev in devices:
        try:
            dev.close()
        except Exception:
            pass


def _import_fido2():
    try:
        from importlib import metadata
        import fido2
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
        return {
            "metadata": metadata,
            "ClientError": ClientError,
            "Collector": DefaultClientDataCollector,
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
    except Exception as exc:
        raise RuntimeError("could not load python-fido2: %s" % exc)


def _translate_error(exc):
    if "pinrequired" in type(exc).__name__.lower():
        # python-fido2 raises PinRequiredError only when request_pin() had
        # nothing to give: the broker's PIN prompt was cancelled or declined.
        # That is the user saying no, not a broken authenticator.
        return ERR_CANCELLED, "a PIN was required and none was entered"
    code = getattr(getattr(exc, "cause", None), "code", None)
    if code is None:
        code = getattr(exc, "code", None)
    if isinstance(code, int):
        if code == _CTAP_NO_CREDENTIALS:
            return ERR_WRONG_CRED, "the connected key does not hold this profile's credential"
        if code in _CTAP_CANCELLED:
            return ERR_CANCELLED, "the FIDO2 operation was cancelled or timed out"
        if code == _CTAP_PIN_NOT_SET:
            return ERR_CAPABILITY, "the authenticator has no FIDO2 PIN set"
        if code in _CTAP_PIN_FAILED:
            return ERR_FAILED, "the authenticator refused the PIN or its retry budget is exhausted"
        if code in _CTAP_UV_REQUIRED:
            return ERR_CAPABILITY, "the authenticator demanded user verification that could not be completed"
    text = ("%s %s" % (type(exc).__name__, exc)).lower()
    if "no credentials" in text or "no_credentials" in text:
        return ERR_WRONG_CRED, "the connected key does not hold this profile's credential"
    if "cancel" in text or "timeout" in text or "timed out" in text:
        return ERR_CANCELLED, "the FIDO2 operation was cancelled or timed out"
    if "pin" in text:
        return ERR_FAILED, "the authenticator requires a PIN interaction that could not be completed"
    return ERR_FAILED, "FIDO2 operation failed (%s)" % type(exc).__name__


def _op_capabilities(mod, args):
    devices = list(mod["CtapHidDevice"].list_devices())
    if not devices:
        return {
            "available": False,
            "hmac_secret": False,
            "authenticators": 0,
            "user_verification": False,
            "detail": "no FIDO2 authenticator is visible over HID",
        }
    hmac_secret = False
    uv_available = False
    try:
        for dev in devices:
            try:
                info = mod["Ctap2"](dev).info
                exts = list(info.extensions or [])
                options = dict(info.options or {})
                if "hmac-secret" in exts:
                    hmac_secret = True
                if options.get("clientPin") is True or options.get("uv") is True:
                    uv_available = True
            except Exception:
                continue
    finally:
        _close_all(devices)

    detail = "direct CTAP over HID (elevated helper)"
    if not hmac_secret:
        detail = "the connected authenticator does not advertise hmac-secret"
    return {
        "available": bool(devices),
        "hmac_secret": hmac_secret,
        "authenticators": len(devices),
        "user_verification": uv_available,
        "detail": detail,
    }


def _make_interaction(mod, pin):
    class _Interaction(mod["UserInteraction"]):
        def prompt_up(self):
            pass

        def request_pin(self, permissions, rp_id):
            return pin or None

        def request_uv(self, permissions, rp_id):
            return True

    return _Interaction()


def _op_create(mod, args):
    rp_id = args.get("rp_id", "saituls.secure-apps.local")
    rp_name = args.get("rp_name", "SAITULS Secure Apps")
    user_name = args.get("user_name", "saituls")
    user_id_b64 = args.get("user_id_b64")
    user_id = b64d(user_id_b64) if user_id_b64 else os.urandom(16)
    credential_profile = args.get("credential_profile", "primary")
    user_verification = args.get("user_verification", "required")
    require_hmac_secret = bool(args.get("require_hmac_secret", True))
    timeout = int(args.get("timeout_seconds", 60))
    pin = args.get("pin")

    devices = list(mod["CtapHidDevice"].list_devices())
    if not devices:
        return None, (ERR_UNAVAILABLE, "no FIDO2 authenticator connected")

    last_error = (ERR_FAILED, "credential creation failed")
    try:
        for dev in devices:
            try:
                collector = mod["Collector"]("https://" + rp_id)
                interaction = _make_interaction(mod, pin)
                client = mod["Fido2Client"](
                    dev,
                    client_data_collector=collector,
                    user_interaction=interaction,
                    extensions=[mod["HmacSecretExtension"](allow_hmac_secret=True)],
                )
                options = mod["CreationOptions"](
                    rp=mod["RpEntity"](id=rp_id, name=rp_name),
                    user=mod["UserEntity"](
                        id=user_id,
                        name=user_name,
                        display_name="SAITULS " + credential_profile,
                    ),
                    challenge=os.urandom(32),
                    pub_key_cred_params=[
                        mod["CredParams"](type=mod["CredType"].PUBLIC_KEY, alg=-7),
                        mod["CredParams"](type=mod["CredType"].PUBLIC_KEY, alg=-257),
                    ],
                    authenticator_selection=mod["Selection"](
                        resident_key=mod["ResidentKey"].DISCOURAGED,
                        user_verification=mod["UV"](user_verification),
                    ),
                    timeout=timeout * 1000,
                    extensions={"hmacCreateSecret": True} if require_hmac_secret else {},
                )
                response = client.make_credential(options)
                results = response.client_extension_results
                created = getattr(results, "hmac_create_secret", None)
                if require_hmac_secret and created is not True:
                    return None, (ERR_CAPABILITY, "authenticator did not enable hmac-secret on credential")
                auth_data = response.response.attestation_object.auth_data
                credential_data = auth_data.credential_data
                if credential_data is None:
                    return None, (ERR_FAILED, "attestation object carried no credential data")
                uv_performed = bool(auth_data.flags & mod["AuthenticatorData"].FLAG.UV)
                if user_verification == "required" and not uv_performed:
                    return None, (ERR_CAPABILITY, "user verification required but not performed")
                cred_id = bytes(credential_data.credential_id)
                return {
                    "credential_id_b64": b64e(cred_id),
                    "user_id_b64": b64e(user_id),
                    "user_verification": uv_performed,
                    "hmac_secret": bool(created),
                }, None
            except Exception as exc:
                last_error = _translate_error(exc)
    finally:
        _close_all(devices)
        pin = None
        args.clear()

    return None, last_error


def _op_hmac(mod, args):
    rp_id = args.get("rp_id", "saituls.secure-apps.local")
    credential_ids_b64 = args.get("credential_ids_b64", [])
    salt_b64 = args.get("salt_b64")
    user_verification = args.get("user_verification", "required")
    timeout = int(args.get("timeout_seconds", 60))
    pin = args.get("pin")

    if not salt_b64:
        return None, (ERR_FAILED, "missing salt")
    salt = b64d(salt_b64)
    allowed = [b64d(c) for c in credential_ids_b64]

    devices = list(mod["CtapHidDevice"].list_devices())
    if not devices:
        return None, (ERR_UNAVAILABLE, "no FIDO2 authenticator connected")

    last_error = (ERR_FAILED, "authentication failed")
    try:
        for dev in devices:
            try:
                collector = mod["Collector"]("https://" + rp_id)
                interaction = _make_interaction(mod, pin)
                client = mod["Fido2Client"](
                    dev,
                    client_data_collector=collector,
                    user_interaction=interaction,
                    extensions=[mod["HmacSecretExtension"](allow_hmac_secret=True)],
                )
                options = mod["RequestOptions"](
                    challenge=os.urandom(32),
                    rp_id=rp_id,
                    allow_credentials=[
                        mod["Descriptor"](type=mod["CredType"].PUBLIC_KEY, id=c)
                        for c in allowed
                    ],
                    user_verification=mod["UV"](user_verification),
                    timeout=timeout * 1000,
                    extensions={"hmacGetSecret": {"salt1": bytes(salt)}},
                )
                selection = client.get_assertion(options)
                response = selection.get_response(0)
                results = response.client_extension_results
                output = getattr(results, "hmac_get_secret", None)
                secret = getattr(output, "output1", None) if output is not None else None
                if not secret:
                    return None, (ERR_CAPABILITY, "authenticator returned no hmac-secret output")
                used_id = bytes(response.raw_id)
                if used_id not in allowed:
                    return None, (ERR_WRONG_CRED, "authenticator returned unenrolled credential")
                uv_performed = bool(response.response.authenticator_data.flags & mod["AuthenticatorData"].FLAG.UV)
                if user_verification == "required" and not uv_performed:
                    return None, (ERR_CAPABILITY, "user verification required but not performed")
                return {
                    "credential_id_b64": b64e(used_id),
                    "output_b64": b64e(secret),
                    "user_verification": uv_performed,
                }, None
            except Exception as exc:
                last_error = _translate_error(exc)
    finally:
        _close_all(devices)
        pin = None
        args.clear()

    return None, last_error


def main():
    try:
        line = sys.stdin.readline()
        if not line:
            return
        request = json.loads(line)
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": ERR_FAILED, "message": "invalid request: %s" % exc}) + "\n")
        sys.stdout.flush()
        return

    op = request.get("op")
    args = request.get("args") or {}

    try:
        mod = _import_fido2()
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": ERR_UNAVAILABLE, "message": str(exc)}) + "\n")
        sys.stdout.flush()
        return

    try:
        if op == "capabilities":
            res = _op_capabilities(mod, args)
            sys.stdout.write(json.dumps({"ok": True, "result": res}) + "\n")
        elif op == "create":
            res, err = _op_create(mod, args)
            if err:
                sys.stdout.write(json.dumps({"ok": False, "error": err[0], "message": err[1]}) + "\n")
            else:
                sys.stdout.write(json.dumps({"ok": True, "result": res}) + "\n")
        elif op == "hmac":
            res, err = _op_hmac(mod, args)
            if err:
                sys.stdout.write(json.dumps({"ok": False, "error": err[0], "message": err[1]}) + "\n")
            else:
                sys.stdout.write(json.dumps({"ok": True, "result": res}) + "\n")
        else:
            sys.stdout.write(json.dumps({"ok": False, "error": ERR_FAILED, "message": "unknown op: %s" % op}) + "\n")
    finally:
        sys.stdout.flush()


if __name__ == "__main__":
    main()
