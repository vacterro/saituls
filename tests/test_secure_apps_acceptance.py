"""Hardware-acceptance gate regression for SAITULS Secure Apps.

Hermetic: no security key, no elevation, no disk image. What is under test is
the gate itself and the bookkeeping that decides whether it may open:

* ``sa_acceptance`` -- the durable record is an allowlist, MIGRATION_READY is
  derived and never passed in, and every materially relevant change (schema,
  host, provider, user-verification policy, backend, transport, hmac-secret
  semantics, python-fido2 major version, security source, the acceptance
  runner that produced the verdict, and any security-relevant field of the
  profile itself) makes an existing record stale;
* the medium-integrity architecture -- an elevated broker can neither produce
  a record that grants readiness nor consume one, which is stricter than the
  two integrity levels merely agreeing;
* the disposable acceptance run's hardware-free building blocks -- every
  required property has a check, a skipped or failed check can never add up
  to MIGRATION_READY, the PIN is remembered only as salted digests, the audit
  scanner finds secrets without printing them, and the disposable registry is
  a valid production profile that mirrors the real one;
* the chain end to end -- a record written by a green run is recognised by
  the production ``sa_cli.py migrate --check-acceptance``, and a failed run
  revokes what an earlier green run left behind.

The hardware modes themselves live in tests/secure_apps_interactive.py and
never run here.
"""
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
TESTS = Path(__file__).resolve().parent
for entry in (SUBSYSTEM, TESTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import sa_acceptance      # noqa: E402
import sa_audit           # noqa: E402
import sa_auth            # noqa: E402
import sa_cli             # noqa: E402
import sa_config          # noqa: E402
import sa_privhelper      # noqa: E402
import sa_privtask        # noqa: E402
import secure_apps_interactive as interactive   # noqa: E402

from test_secure_apps import ACCEPTED_ENVIRONMENT, Harness, all_gates   # noqa: E402

CANARY_PIN = "CANARY-PIN-736152"
CANARY_RECOVERY = "123456-234567-345678-456789-567890-678901-789012-890123"
CANARY_SECRET = "c2FpdHVscy1jYW5hcnktdm9sdW1lLXNlY3JldC1ieXRlcy0wMTIzNDU2Nzg5"


class FakePolicy(object):
    """The shipped obsidian policy, with one field at a time moved."""

    def __init__(self, **changes):
        self.mode = "default"
        self.idle_timeout_minutes = 360
        self.lock_on_windows_lock = True
        self.lock_on_suspend = True
        self.lock_on_logoff = True
        self.lock_on_shutdown = True
        self.lock_on_broker_shutdown = True
        self.unmount_when_app_closes = True
        self.graceful_close_timeout_seconds = 30
        self.force_terminate_after_timeout = True
        self.process_exit_confirm_timeout_seconds = 20
        self.unmount_timeout_seconds = 60
        self.conditions = [
            sa_config.Condition("require_auth_after_windows_lock", {}),
            sa_config.Condition("require_auth_after_session_expiry", {}),
            sa_config.Condition("unmount_storage_on_app_exit", {}),
            sa_config.Condition("require_auth_provider",
                                {"provider": "yubikey-fido2-hmac-secret"}),
        ]
        for name, value in changes.items():
            if not hasattr(self, name):
                raise AttributeError("no policy field %r" % (name,))
            setattr(self, name, value)


class FakeProfile(object):
    """Every field the security fingerprint reads, cosmetic ones included."""

    def __init__(self, provider="yubikey-fido2-hmac-secret", uv="required",
                 backend="bitlocker-vhdx", policy=None, **changes):
        self.id = "vault"
        self.label = "Vault"
        self.notes = None
        self.enabled = True
        self.provider = provider
        self.credential_profile = "primary"
        self.helper_executable = None
        self.user_verification = uv
        self.backend = backend
        self.container = r"C:\managed\vaults\vault\vault.vhdx"
        self.container_id = "vault-1"
        self.mount_path = r"V:\vault"
        self.executable = r"C:\managed\apps\vault\app\App.exe"
        self.working_directory = r"C:\managed\apps\vault\app"
        self.arguments = []
        self.vault_argument_style = "path"
        self.allow_unmanaged_executable = False
        self.size_gb = 16
        self.filesystem_label = "SAITULS-VAULT"
        self.policy = policy if policy is not None else FakePolicy()
        for name, value in changes.items():
            if not hasattr(self, name):
                raise AttributeError("no profile field %r" % (name,))
            setattr(self, name, value)


def profile_facts(provider="yubikey-fido2-hmac-secret", uv="required",
                  backend="bitlocker-vhdx"):
    """What a record says about the profile -- fingerprint included.

    Built through the production helper on purpose: a record made here and a
    verdict computed from the matching :class:`FakeProfile` agree by
    construction, so a test that blocks does so for the reason it names.
    """
    return sa_acceptance.profile_facts(FakeProfile(provider, uv, backend))


class TempDirCase(unittest.TestCase):
    def tempdir(self):
        path = Path(tempfile.mkdtemp(prefix="saituls-acceptance-"))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path


# ══════════════════════════════════════════════════════════ the record
class AcceptanceRecordTests(TempDirCase):
    def test_the_record_carries_only_allowlisted_fields(self):
        environment = dict(ACCEPTED_ENVIRONMENT, pin=CANARY_PIN,
                           recovery_password=CANARY_RECOVERY,
                           hmac_secret_output=CANARY_SECRET)
        facts = dict(profile_facts(), wrapped_key=CANARY_SECRET)
        gates = dict(all_gates(), credential_secret=CANARY_SECRET)
        record = sa_acceptance.build_record(environment, facts, gates)
        self.assertEqual(tuple(record), sa_acceptance.RECORD_FIELDS)
        text = json.dumps(record)
        for canary in (CANARY_PIN, CANARY_RECOVERY, CANARY_SECRET):
            self.assertNotIn(canary, text)
        path = sa_acceptance.save_record(str(self.tempdir()), record)
        on_disk = Path(path).read_text(encoding="utf-8")
        for canary in (CANARY_PIN, CANARY_RECOVERY, CANARY_SECRET):
            self.assertNotIn(canary, on_disk)
        self.assertEqual(tuple(json.loads(on_disk)), sa_acceptance.RECORD_FIELDS)

    def test_the_directive_facts_are_all_recorded(self):
        for name in ("schema_version", "timestamp", "host_fingerprint",
                     "windows_build", "webauthn_api_version",
                     "python_fido2_version", "provider", "transport",
                     "acceptance_producer_fingerprint",
                     "profile_security_fingerprint",
                     "user_verification_policy", "fido2_hardware_accepted",
                     "storage_accepted", "default_mode_accepted",
                     "aggressive_mode_accepted", "workstation_lock_accepted",
                     "helper_failure_accepted", "audit_hygiene_accepted",
                     "migration_ready"):
            self.assertIn(name, sa_acceptance.RECORD_FIELDS)

    def test_migration_ready_is_derived_never_passed_in(self):
        gates = all_gates()
        gates["migration_ready"] = True
        gates["workstation_lock_accepted"] = False
        record = sa_acceptance.build_record(ACCEPTED_ENVIRONMENT, profile_facts(), gates)
        self.assertFalse(record["migration_ready"])
        self.assertTrue(sa_acceptance.build_record(
            ACCEPTED_ENVIRONMENT, profile_facts(), all_gates())["migration_ready"])

    def test_an_incomplete_run_is_never_ready(self):
        record = sa_acceptance.build_record(ACCEPTED_ENVIRONMENT, profile_facts(),
                                            all_gates(), complete=False)
        self.assertFalse(record["migration_ready"])

    def test_only_a_literal_true_counts_as_accepted(self):
        for truthy in ("PASS", "true", 1, [True]):
            gates = dict(all_gates(), audit_hygiene_accepted=truthy)
            record = sa_acceptance.build_record(ACCEPTED_ENVIRONMENT, profile_facts(),
                                                gates)
            self.assertIs(record["audit_hygiene_accepted"], False, truthy)
            self.assertFalse(record["migration_ready"], truthy)

    def test_save_refuses_a_field_outside_the_allowlist(self):
        record = sa_acceptance.build_record(ACCEPTED_ENVIRONMENT, profile_facts(),
                                            all_gates())
        record["pin"] = CANARY_PIN
        state = self.tempdir()
        with self.assertRaises(sa_acceptance.AcceptanceError) as caught:
            sa_acceptance.save_record(str(state), record)
        self.assertNotIn(CANARY_PIN, str(caught.exception))
        self.assertFalse(Path(sa_acceptance.record_path(str(state))).exists())

    def test_revoke_removes_the_record(self):
        state = str(self.tempdir())
        sa_acceptance.save_record(state, sa_acceptance.build_record(
            ACCEPTED_ENVIRONMENT, profile_facts(), all_gates()))
        self.assertTrue(sa_acceptance.revoke(state))
        self.assertFalse(os.path.exists(sa_acceptance.record_path(state)))
        self.assertTrue(sa_acceptance.revoke(state))


# ══════════════════════════════════════════════════════════ the gate
class AcceptanceGateTests(TempDirCase):
    def _state(self, environment=None, facts=None, gates=None, mutate=None):
        state = str(self.tempdir())
        record = sa_acceptance.build_record(environment or ACCEPTED_ENVIRONMENT,
                                            facts or profile_facts(),
                                            all_gates() if gates is None else gates)
        if mutate:
            mutate(record)
        path = sa_acceptance.record_path(state)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        return state

    def _evaluate(self, state, profile=None, environment=None):
        return sa_acceptance.evaluate(state, profile or FakeProfile(),
                                      dict(environment or ACCEPTED_ENVIRONMENT))

    def assertBlocked(self, verdict, fragment):
        self.assertFalse(verdict.ok)
        self.assertTrue(any(fragment in reason for reason in verdict.reasons),
                        "%r not in %r" % (fragment, verdict.reasons))
        self.assertEqual(verdict.to_dict()["status"], sa_acceptance.MIGRATION_BLOCKED)

    def test_a_matching_record_is_accepted(self):
        verdict = self._evaluate(self._state())
        self.assertTrue(verdict.ok, verdict.reasons)
        self.assertEqual(verdict.to_dict()["status"], list(sa_acceptance.FINAL_TOKENS))

    def test_no_record_blocks(self):
        self.assertBlocked(self._evaluate(str(self.tempdir())),
                           "no hardware acceptance record")

    def test_a_malformed_record_blocks(self):
        state = str(self.tempdir())
        Path(sa_acceptance.record_path(state)).write_text("{not json", encoding="utf-8")
        self.assertBlocked(self._evaluate(state), "unreadable or malformed")

    def test_a_changed_provider_blocks(self):
        self.assertBlocked(self._evaluate(self._state(), FakeProfile(
            provider="external-helper-fido2-hmac-secret")), "authentication provider")

    def test_a_changed_user_verification_policy_blocks(self):
        self.assertBlocked(self._evaluate(self._state(), FakeProfile(uv="preferred")),
                           "user verification policy")

    def test_a_changed_storage_backend_blocks(self):
        self.assertBlocked(self._evaluate(self._state(), FakeProfile(backend="other")),
                           "storage backend")

    def test_a_changed_transport_blocks(self):
        newer = dict(ACCEPTED_ENVIRONMENT, webauthn_api_version=6,
                     transport=sa_auth.TRANSPORT_WINDOWS_WEBAUTHN)
        self.assertBlocked(self._evaluate(self._state(), environment=newer), "transport")

    def test_changed_hmac_transport_semantics_block(self):
        other = dict(ACCEPTED_ENVIRONMENT, hmac_transport_semantics="webauthn-v1")
        self.assertBlocked(self._evaluate(self._state(), environment=other),
                           "hmac-secret transport semantics")

    def test_a_new_python_fido2_major_blocks(self):
        three = dict(ACCEPTED_ENVIRONMENT, python_fido2_version="3.0.0",
                     python_fido2_major=3)
        self.assertBlocked(self._evaluate(self._state(), environment=three),
                           "python-fido2 major version")

    def test_a_python_fido2_minor_update_does_not_block(self):
        minor = dict(ACCEPTED_ENVIRONMENT, python_fido2_version="2.3.0")
        self.assertTrue(self._evaluate(self._state(), environment=minor).ok)

    def test_a_missing_python_fido2_blocks(self):
        gone = dict(ACCEPTED_ENVIRONMENT, python_fido2_version=None,
                    python_fido2_major=None)
        self.assertBlocked(self._evaluate(self._state(), environment=gone),
                           "python-fido2 major version cannot be determined")

    def test_a_changed_schema_blocks(self):
        self.assertBlocked(self._evaluate(self._state(mutate=lambda r: r.update(
            schema_version=sa_acceptance.SCHEMA_VERSION + 1))), "acceptance schema")
        self.assertBlocked(self._evaluate(self._state(
            mutate=lambda r: r.update(schema="saituls.other/1"))), "acceptance schema")

    def test_another_host_blocks(self):
        elsewhere = dict(ACCEPTED_ENVIRONMENT, host_fingerprint="d" * 64)
        self.assertBlocked(self._evaluate(self._state(), environment=elsewhere),
                           "host identity changed")

    def test_an_unmeasurable_host_blocks(self):
        unknown = dict(ACCEPTED_ENVIRONMENT, host_fingerprint=None)
        self.assertBlocked(self._evaluate(self._state(), environment=unknown),
                           "host identity cannot be determined")

    def test_a_changed_broker_integrity_blocks(self):
        elevated = dict(ACCEPTED_ENVIRONMENT, broker_elevated=True)
        self.assertBlocked(self._evaluate(self._state(), environment=elevated),
                           "broker integrity")

    def test_a_changed_implementation_blocks(self):
        edited = dict(ACCEPTED_ENVIRONMENT, implementation_fingerprint="e" * 64)
        self.assertBlocked(self._evaluate(self._state(), environment=edited),
                           "Secure Apps implementation changed")

    # -- the acceptance producer ------------------------------------------
    def test_a_changed_acceptance_producer_blocks(self):
        """The runner that decides PASS is as binding as the code it runs."""
        edited = dict(ACCEPTED_ENVIRONMENT,
                      acceptance_producer_fingerprint="9" * 64)
        self.assertBlocked(self._evaluate(self._state(), environment=edited),
                           "acceptance producer changed")

    def test_an_unmeasurable_acceptance_producer_blocks(self):
        gone = dict(ACCEPTED_ENVIRONMENT, acceptance_producer_fingerprint=None)
        self.assertBlocked(self._evaluate(self._state(), environment=gone),
                           "acceptance producer cannot be determined")

    def test_a_record_that_never_bound_the_producer_blocks(self):
        """An older record is not grandfathered in by the field's absence."""
        def strip(record):
            record.pop("acceptance_producer_fingerprint")
        verdict = self._evaluate(self._state(mutate=strip))
        self.assertBlocked(verdict, "acceptance producer changed")

    # -- the profile's security configuration ------------------------------
    def assertProfileChangeBlocks(self, changed, *extra):
        verdict = self._evaluate(self._state(), changed)
        self.assertBlocked(verdict, "security-relevant profile configuration "
                                    "changed")
        for fragment in extra:
            self.assertBlocked(verdict, fragment)

    def test_a_changed_lock_on_windows_lock_blocks(self):
        self.assertProfileChangeBlocks(
            FakeProfile(policy=FakePolicy(lock_on_windows_lock=False)))

    def test_a_changed_idle_timeout_blocks(self):
        self.assertProfileChangeBlocks(
            FakeProfile(policy=FakePolicy(idle_timeout_minutes=10080)))

    def test_a_changed_policy_mode_blocks(self):
        self.assertProfileChangeBlocks(
            FakeProfile(policy=FakePolicy(mode="aggressive")))

    def test_a_removed_require_auth_after_windows_lock_condition_blocks(self):
        policy = FakePolicy()
        policy.conditions = [c for c in policy.conditions
                             if c.type != "require_auth_after_windows_lock"]
        self.assertProfileChangeBlocks(FakeProfile(policy=policy))

    def test_a_changed_condition_parameter_blocks(self):
        policy = FakePolicy()
        policy.conditions = [c for c in policy.conditions
                             if c.type != "require_auth_provider"]
        policy.conditions.append(sa_config.Condition(
            "require_auth_provider", {"provider": "external-helper-fido2-hmac-secret"}))
        self.assertProfileChangeBlocks(FakeProfile(policy=policy))

    def test_a_changed_mount_path_blocks(self):
        self.assertProfileChangeBlocks(FakeProfile(mount_path=r"V:\elsewhere"))

    def test_a_changed_container_blocks(self):
        self.assertProfileChangeBlocks(
            FakeProfile(container=r"D:\other\vault.vhdx"))

    def test_a_changed_protected_application_blocks(self):
        self.assertProfileChangeBlocks(FakeProfile(executable=r"C:\other\App.exe"))
        self.assertProfileChangeBlocks(FakeProfile(allow_unmanaged_executable=True))

    def test_a_changed_user_verification_blocks_by_name_and_by_fingerprint(self):
        self.assertProfileChangeBlocks(FakeProfile(uv="preferred"),
                                       "user verification policy")

    def test_a_changed_storage_backend_blocks_by_name_and_by_fingerprint(self):
        self.assertProfileChangeBlocks(FakeProfile(backend="other"),
                                       "storage backend")

    def test_a_changed_provider_blocks_by_name_and_by_fingerprint(self):
        self.assertProfileChangeBlocks(
            FakeProfile(provider="external-helper-fido2-hmac-secret"),
            "authentication provider")

    def test_an_unmeasurable_profile_blocks(self):
        class Unreadable(object):
            id = "vault"
            provider = "yubikey-fido2-hmac-secret"
            user_verification = "required"
            backend = "bitlocker-vhdx"
            policy = None
        self.assertBlocked(self._evaluate(self._state(), Unreadable()),
                           "security-relevant profile configuration cannot be "
                           "determined")

    def test_cosmetic_profile_changes_do_not_block(self):
        """A rename must not cost a run with the physical key."""
        for field, value in (("label", "Renamed vault"),
                             ("notes", "a paragraph of prose"),
                             ("size_gb", 64),
                             ("filesystem_label", "SAITULS-OTHER")):
            verdict = self._evaluate(self._state(), FakeProfile(**{field: value}))
            self.assertTrue(verdict.ok, "%s: %s" % (field, verdict.reasons))

    def test_reordering_the_same_conditions_does_not_block(self):
        policy = FakePolicy()
        policy.conditions = list(reversed(policy.conditions))
        self.assertTrue(self._evaluate(self._state(),
                                       FakeProfile(policy=policy)).ok)

    # -- the medium-integrity architecture ---------------------------------
    def test_an_elevated_migration_is_refused_even_when_the_record_agrees(self):
        """Equality between two elevated values is not the architecture."""
        elevated = dict(ACCEPTED_ENVIRONMENT, broker_elevated=True)
        state = self._state(environment=elevated,
                            mutate=lambda r: r.update(migration_ready=True))
        verdict = self._evaluate(state, environment=elevated)
        self.assertBlocked(verdict,
                           sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY)
        named = [r for r in verdict.reasons
                 if sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY in r]
        self.assertEqual(len(named), 2, verdict.reasons)

    def test_an_elevated_record_is_refused_by_a_medium_broker(self):
        state = self._state(environment=dict(ACCEPTED_ENVIRONMENT,
                                             broker_elevated=True),
                            mutate=lambda r: r.update(migration_ready=True))
        self.assertBlocked(self._evaluate(state),
                           sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY)

    def test_an_elevated_broker_is_refused_by_a_medium_record(self):
        elevated = dict(ACCEPTED_ENVIRONMENT, broker_elevated=True)
        self.assertBlocked(self._evaluate(self._state(), environment=elevated),
                           sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY)

    def test_an_unknown_integrity_level_is_refused(self):
        unknown = dict(ACCEPTED_ENVIRONMENT, broker_elevated=None)
        self.assertBlocked(self._evaluate(self._state(), environment=unknown),
                           sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY)

    def test_a_medium_broker_beside_an_elevated_helper_is_accepted(self):
        """Only the broker's integrity is bound; the helper stays elevated."""
        verdict = self._evaluate(self._state())
        self.assertTrue(verdict.ok, verdict.reasons)
        self.assertIs(verdict.record["broker_elevated"], False)
        # Nothing in the record asks the privileged helper to be medium, and
        # sa_privhelper still refuses a peer that is not elevated + high.
        self.assertNotIn("helper_elevated", sa_acceptance.RECORD_FIELDS)
        self.assertTrue(callable(sa_privhelper.process_is_elevated))

    def test_each_unaccepted_gate_blocks(self):
        for flag in sa_acceptance.GATE_FLAGS:
            gates = dict(all_gates(), **{flag: False})
            verdict = self._evaluate(self._state(gates=gates))
            self.assertBlocked(verdict, "%s was not accepted" % flag)
            self.assertBlocked(verdict, "did not grant migration readiness")

    def test_a_hand_edited_ready_flag_does_not_open_the_gate(self):
        def forge(record):
            record["helper_failure_accepted"] = False
            record["migration_ready"] = True
        self.assertBlocked(self._evaluate(self._state(mutate=forge)),
                           "helper_failure_accepted was not accepted")

    def test_string_flags_do_not_open_the_gate(self):
        def stringify(record):
            for flag in sa_acceptance.GATE_FLAGS + ("migration_ready",):
                record[flag] = "true"
        self.assertBlocked(self._evaluate(self._state(mutate=stringify)),
                           "was not accepted")

    def test_a_record_with_foreign_fields_blocks(self):
        self.assertBlocked(self._evaluate(self._state(
            mutate=lambda r: r.update(pin=CANARY_PIN))), "outside the acceptance allowlist")

    def test_the_refusal_names_the_token_only_as_a_refusal(self):
        verdict = self._evaluate(self._state(gates=dict(all_gates(),
                                                        storage_accepted=False)))
        for reason in verdict.reasons:
            for token in sa_acceptance.FINAL_TOKENS:
                self.assertNotIn(token, reason)


# ══════════════════════════════════════════════════════════ the environment
class EnvironmentTests(TempDirCase):
    def test_expected_transport(self):
        expect = sa_acceptance.expected_transport
        self.assertEqual(expect(True, 2, windows=True),
                         sa_auth.TRANSPORT_ELEVATED_CTAP_HELPER)
        self.assertEqual(expect(False, 0, windows=True),
                         sa_auth.TRANSPORT_ELEVATED_CTAP_HELPER)
        self.assertEqual(expect(True, 6, windows=True),
                         sa_auth.TRANSPORT_WINDOWS_WEBAUTHN)
        self.assertEqual(expect(False, 7, windows=True),
                         sa_auth.TRANSPORT_ELEVATED_CTAP_HELPER)
        self.assertEqual(expect(False, 0, windows=False), sa_auth.TRANSPORT_DIRECT_CTAP)

    def test_expected_transport_agrees_with_the_provider(self):
        class Helper(object):
            running = True

            def fido_capabilities(self, timeout=30.0):
                return {"available": True, "hmac_secret": True, "authenticators": 1,
                        "user_verification": True}

        class Windows(object):
            def __init__(self, *args, **kwargs):
                pass

            @staticmethod
            def is_available():
                return True

        for api in (2, 5, 6, 7):
            provider = sa_auth.Fido2HmacSecretProvider(helper=Helper())
            try:
                mod = dict(provider._modules())
            except sa_auth.AuthUnavailableError:
                self.skipTest("python-fido2 is not installed")
            mod["WindowsClient"] = Windows
            mod["WEBAUTHN_API_VERSION"] = api
            provider._modules_cache = mod
            with mock.patch.object(sa_auth.os, "name", "nt"):
                transport = provider.resolve_transport()[0]
            self.assertEqual(transport,
                             sa_acceptance.expected_transport(True, api, windows=True),
                             "API version %d" % api)

    def test_the_implementation_fingerprint_follows_the_security_sources(self):
        copy = self.tempdir()
        for name in sa_acceptance.SECURITY_SOURCES:
            shutil.copyfile(SUBSYSTEM / name, copy / name)
        original = sa_acceptance.implementation_fingerprint(str(copy))
        self.assertRegex(original, r"^[0-9a-f]{64}$")
        self.assertEqual(original, sa_acceptance.implementation_fingerprint())

        # line endings alone are not a change
        target = copy / "sa_auth.py"
        data = target.read_bytes().replace(b"\r\n", b"\n")
        target.write_bytes(data.replace(b"\n", b"\r\n"))
        self.assertEqual(sa_acceptance.implementation_fingerprint(str(copy)), original)
        target.write_bytes(data)
        self.assertEqual(sa_acceptance.implementation_fingerprint(str(copy)), original)

        for name in ("sa_auth.py", "sa_storage_helper.ps1", "sa_fido_worker.py"):
            path = copy / name
            saved = path.read_bytes()
            path.write_bytes(saved + b"\n# edited\n")
            self.assertNotEqual(sa_acceptance.implementation_fingerprint(str(copy)),
                                original, name)
            path.write_bytes(saved)
        (copy / "sa_broker.py").unlink()
        self.assertIsNone(sa_acceptance.implementation_fingerprint(str(copy)))

    def test_every_module_the_migration_entry_point_loads_is_fingerprinted(self):
        pattern = re.compile(r"^\s*(?:import|from)\s+(sa_\w+)", re.MULTILINE)
        seen, queue = set(), ["sa_cli"]
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            text = (SUBSYSTEM / (name + ".py")).read_text(encoding="utf-8")
            queue.extend(pattern.findall(text))
        loaded = {name + ".py" for name in seen}
        # launched rather than imported, by the elevated helper
        loaded |= {"sa_storage_helper.ps1", "sa_fido_worker.py"}
        missing = sorted(loaded - set(sa_acceptance.SECURITY_SOURCES))
        self.assertEqual(missing, [], "security sources not fingerprinted: %s" % missing)
        for dead in ("sa_ctap.py", "sa_hid.py", "sa_cbor.py"):
            self.assertNotIn(dead, loaded, "%s is imported by production again" % dead)
            self.assertNotIn(dead, sa_acceptance.SECURITY_SOURCES)

    @unittest.skipUnless(os.name == "nt", "Windows host facts")
    def test_the_real_environment_is_measurable(self):
        env = sa_acceptance.probe_environment()
        self.assertEqual(set(env), set(sa_acceptance.ENVIRONMENT_FIELDS))
        self.assertRegex(env["host_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(env["host_fingerprint"], sa_acceptance.host_fingerprint())
        self.assertRegex(env["windows_build"], r"^\d+\.\d+\.\d+(\.\d+)?$")
        self.assertIn(env["broker_elevated"], (True, False))
        self.assertIn(env["transport"], sa_auth.TRANSPORTS)
        self.assertEqual(env["implementation_fingerprint"],
                         sa_acceptance.implementation_fingerprint())
        self.assertEqual(env["hmac_transport_semantics"],
                         sa_auth.DEFAULT_HMAC_TRANSPORT_SEMANTICS)

    def test_the_acceptance_command_is_exact(self):
        command = sa_acceptance.acceptance_command(
            profile_id="obsidian", registry_path=r"C:\reg dir\secure_apps.json",
            managed_root=r"D:\managed root", python=r"C:\Py\python.exe")
        self.assertTrue(os.path.isfile(sa_acceptance.ACCEPTANCE_SCRIPT))
        self.assertEqual(command, '"C:\\Py\\python.exe" "%s" --disposable-acceptance '
                         '--profile obsidian --registry "C:\\reg dir\\secure_apps.json" '
                         '--managed-root "D:\\managed root"'
                         % sa_acceptance.ACCEPTANCE_SCRIPT)


# ══════════════════════════════════════════════ disposable run building blocks
class AcceptanceChecksTests(unittest.TestCase):
    def test_every_gate_has_checks_and_every_check_feeds_a_gate(self):
        ids = [check_id for check_id, _flag, _text in interactive.CHECKS]
        self.assertEqual(len(ids), len(set(ids)))
        flags = {flag for _id, flag, _text in interactive.CHECKS}
        self.assertEqual(flags, set(sa_acceptance.GATE_FLAGS))

    def test_no_gate_flag_may_exist_without_evidence(self):
        """The defect this closes: ``all([])`` is True.

        Two gates -- privileged_task_security_accepted and
        privileged_runtime_acl_accepted -- were declared in GATE_FLAGS with no
        CHECK mapped to them, so their conjunction was over an EMPTY list and
        both were recorded as accepted on every run, forever, without a single
        check ever having been run. This test fails the moment a future gate
        is added without evidence behind it, and the companion test below
        proves the aggregation itself no longer accepts an empty conjunction.
        """
        for flag in sa_acceptance.GATE_FLAGS:
            mapped = [check_id for check_id, mapped_flag, _text
                      in interactive.CHECKS if mapped_flag == flag]
            self.assertTrue(mapped,
                            "gate %r has no CHECK mapped to it: it would be "
                            "accepted on an empty conjunction" % flag)

    def test_a_gate_with_no_mapped_check_is_never_accepted(self):
        """The aggregation, not the data: an unmapped gate must be False."""
        run = interactive.AcceptanceRun(echo=None)
        for check_id, _flag, _text in interactive.CHECKS:
            run.check(check_id, True)
        with mock.patch.object(sa_acceptance, "GATE_FLAGS",
                               tuple(sa_acceptance.GATE_FLAGS)
                               + ("a_gate_nothing_proves",)):
            gates = run.gates()
        self.assertFalse(gates["a_gate_nothing_proves"],
                         "a gate with zero mapped checks was accepted")
        self.assertTrue(gates["storage_accepted"])

    def test_the_privilege_boundary_gates_are_proven_by_real_operations(self):
        """P05/P06/P07 must attempt the operation, not parse a descriptor."""
        text = {check_id: desc for check_id, _flag, desc in interactive.CHECKS}
        self.assertIn("REALLY denies", text["P05"])
        self.assertIn("REALLY lets", text["P06"])
        self.assertIn("owned by Administrators/SYSTEM", text["P07"])
        mapped = {flag: [check_id for check_id, mapped_flag, _t
                         in interactive.CHECKS if mapped_flag == flag]
                  for flag in ("privileged_runtime_acl_accepted",
                               "privileged_task_security_accepted")}
        self.assertEqual(mapped["privileged_runtime_acl_accepted"], ["P05", "P07"])
        self.assertEqual(mapped["privileged_task_security_accepted"], ["P06"])
        source = (REPO / "tests" / "secure_apps_interactive.py").read_text(
            encoding="utf-8")
        self.assertIn("probe.run_medium", source)
        self.assertIn("phase_privilege_boundary", interactive.DisposableAcceptance.PHASES)

    def test_the_required_sequence_is_covered(self):
        text = {check_id: desc for check_id, _flag, desc in interactive.CHECKS}
        required = {
            "F01": "transport", "F02": "authenticator detected",
            "F03": "hmac-secret", "F04": "user verification",
            "F05": "credential creation", "F06": "correct PIN + touch",
            "F07": "unwrap", "F08": "cancelled PIN", "F09": "wrong PIN",
            "F10": "no key", "F11": "wrong enrolled credential",
            "S02": "write and read", "S04": "broker shutdown",
            "D01": "Default reopen", "A02": "Aggressive reopen",
            "W02": "workstation lock", "H02": "never reads as LOCKED",
            "X02": "no secret",
        }
        for check_id, fragment in required.items():
            self.assertIn(fragment, text[check_id], check_id)

    def _green_run(self):
        run = interactive.AcceptanceRun(echo=None)
        for check_id, _flag, _text in interactive.CHECKS:
            run.check(check_id, True)
        return run

    def test_a_fully_green_run_is_complete(self):
        run = self._green_run()
        self.assertTrue(run.complete())
        self.assertTrue(all(run.gates().values()))

    def test_a_skipped_check_is_never_green(self):
        run = interactive.AcceptanceRun(echo=None)
        for check_id, _flag, _text in interactive.CHECKS:
            if check_id == "F09":
                run.skip(check_id, "operator declined")
            else:
                run.check(check_id, True)
        self.assertFalse(run.complete())
        self.assertFalse(run.gates()["fido2_hardware_accepted"])
        self.assertTrue(run.gates()["storage_accepted"])
        record = sa_acceptance.build_record(ACCEPTED_ENVIRONMENT, profile_facts(),
                                            run.gates(), complete=run.complete())
        self.assertFalse(record["migration_ready"])

    def test_a_failure_is_not_repaired_by_a_later_pass(self):
        run = self._green_run()
        run.check("S03", False, "detach failed once")
        self.assertFalse(run.passed("S03"))
        self.assertFalse(run.complete())
        self.assertFalse(run.gates()["storage_accepted"])

    def test_a_missing_check_is_never_green(self):
        run = interactive.AcceptanceRun(echo=None)
        for check_id, _flag, _text in interactive.CHECKS:
            if check_id != "W01":
                run.check(check_id, True)
        self.assertEqual(run.missing(), ["W01"])
        self.assertFalse(run.complete())
        self.assertFalse(run.gates()["workstation_lock_accepted"])

    def test_an_aborted_run_is_never_complete(self):
        run = self._green_run()
        run.abort("cancelled by the operator")
        self.assertFalse(run.complete())

    def test_an_unknown_check_id_is_a_programming_error(self):
        with self.assertRaises(KeyError):
            interactive.AcceptanceRun(echo=None).check("Z99", True)


class PinPromptTests(unittest.TestCase):
    def test_the_prompt_keeps_no_pin(self):
        prompt = interactive.PinPrompt(reader=lambda text: CANARY_PIN)
        self.assertEqual(prompt(sa_auth.RP_ID), CANARY_PIN)
        self.assertEqual(prompt.prompts, 1)
        for value in vars(prompt).values():
            self.assertNotIn(CANARY_PIN, repr(value))

    def test_the_prompt_recognises_what_was_typed(self):
        prompt = interactive.PinPrompt(reader=lambda text: CANARY_PIN)
        prompt()
        self.assertTrue(prompt.found_in(CANARY_PIN))
        self.assertTrue(prompt.found_in("detail " + CANARY_PIN + " tail"))
        self.assertFalse(prompt.found_in("detail " + CANARY_PIN + " tail",
                                         substrings=False))
        self.assertFalse(prompt.found_in("CANARY-PIN-736153"))

    def test_short_pins_are_matched_whole_only(self):
        prompt = interactive.PinPrompt(reader=lambda text: "2026")
        prompt()
        self.assertTrue(prompt.found_in("2026"))
        self.assertFalse(prompt.found_in("2026-09-16T12:00:00Z"))

    def test_a_cancelled_prompt_returns_none(self):
        for typed in ("", None):
            prompt = interactive.PinPrompt(reader=lambda text, typed=typed: typed)
            self.assertIsNone(prompt("rp"))

    def test_the_console_callbacks_take_the_relying_party_id(self):
        with mock.patch.object(interactive, "_getpass", lambda text: "1234"):
            self.assertEqual(interactive.console_pin(sa_auth.RP_ID), "1234")
            self.assertEqual(interactive.console_pin(), "1234")


class AuditScanTests(TempDirCase):
    def _lifecycle_audit(self):
        h = Harness()
        self.addCleanup(h.close)
        h.enroll()
        h.broker.open("vault")
        h.adapter.exit_tree(h.broker.status("vault")["app_pid"])
        h.broker.tick()
        h.broker.lock_now("vault")
        return h

    def test_a_real_lifecycle_audit_is_clean(self):
        h = self._lifecycle_audit()
        prompt = interactive.PinPrompt(reader=lambda text: CANARY_PIN)
        prompt()
        known = (h.profile.mount_path,
                 interactive.sa_paths.enrollment_mount_path(h.profile.container))
        records, allow, secret = interactive.scan_audit(
            h.registry.audit_path, pin_prompt=prompt,
            recovery_values=[CANARY_RECOVERY], known_mount_paths=known)
        self.assertGreater(records, 5)
        self.assertEqual(allow, [])
        self.assertEqual(secret, [])

    def test_secrets_are_found_and_never_repeated(self):
        h = self._lifecycle_audit()
        prompt = interactive.PinPrompt(reader=lambda text: CANARY_PIN)
        prompt()
        log = sa_audit.AuditLog(h.registry.audit_path)
        # through the real logger: allowlisted fields carrying what must not be there
        log.write("leak_pin", profile_id="vault", detail_code="pin=" + CANARY_PIN)
        log.write("leak_recovery", profile_id="vault", event=CANARY_RECOVERY)
        log.write("leak_blob", profile_id="vault", container_id=CANARY_SECRET)
        log.write("leak_hash", profile_id="vault", credential_id_hash="not-a-hash")
        # and past the logger entirely, as a broken writer would
        with open(h.registry.audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": "x", "transition": "raw", "result": "ok",
                                     "error_category": "none", "pin": "x"}) + "\n")
        records, allow, secret = interactive.scan_audit(
            h.registry.audit_path, pin_prompt=prompt,
            recovery_values=[CANARY_RECOVERY],
            known_mount_paths=(h.profile.mount_path,
                               interactive.sa_paths.enrollment_mount_path(
                                   h.profile.container)))
        joined = "\n".join(allow + secret)
        self.assertIn("detail_code carries an entered PIN", joined)
        self.assertIn("event carries the BitLocker recovery password", joined)
        self.assertIn("container_id carries a key-material-shaped value", joined)
        self.assertIn("credential_id_hash is not a 32-hex hash", joined)
        self.assertIn("non-allowlisted field(s) ['pin']", joined)
        for canary in (CANARY_PIN, CANARY_RECOVERY, CANARY_SECRET):
            self.assertNotIn(canary, joined)


class DisposableRegistryTests(TempDirCase):
    def _template(self, root):
        registry = sa_config.load_registry(sa_config.default_registry_path(),
                                           managed_root=str(root))
        return registry.get("obsidian")

    def test_the_disposable_profile_is_a_valid_production_profile(self):
        root = self.tempdir()
        template = self._template(root / "prod")
        document = interactive.disposable_registry_document(
            template, root / "acceptance", root / "mount", root / "acceptance" / "stop")
        # production validation: no test providers, no test backends
        registry = sa_config.parse_registry(document,
                                            managed_root=str(root / "acceptance"))
        profile = registry.get(interactive.DISPOSABLE_PROFILE)
        self.assertEqual(profile.provider, template.provider)
        self.assertEqual(profile.user_verification, template.user_verification)
        self.assertEqual(profile.user_verification, "required")
        self.assertEqual(profile.backend, template.backend)
        self.assertEqual([c.to_dict() for c in profile.policy.conditions],
                         [c.to_dict() for c in template.policy.conditions])
        self.assertEqual(profile.policy.mode, "default")
        self.assertEqual(profile.size_gb, 1)
        self.assertEqual(profile.vault_argument_style, "none")
        self.assertEqual(profile.executable, sa_config.sa_paths.canonical(sys.executable))
        self.assertNotEqual(profile.mount_path.lower(), template.mount_path.lower())
        self.assertNotEqual(profile.container.lower(), template.container.lower())


@unittest.skipUnless(os.name == "nt", "Windows session notifications")
@unittest.skipIf(os.environ.get("CI"), "hosted runners have no interactive session")
class WorkstationLockListenerTests(unittest.TestCase):
    def test_the_listener_registers_for_session_notifications_and_stops(self):
        listener = interactive.WorkstationLockListener()
        try:
            self.assertTrue(listener.start(), listener.failed)
            self.assertFalse(listener.locked.is_set())
        finally:
            listener.stop()
        self.assertFalse(listener._thread.is_alive())


# ═════════════════════════════════════════════════ the acceptance producer
class AcceptanceProducerTests(TempDirCase):
    """The program that decides PASS is bound as tightly as the code it runs."""

    def _repo(self, edit=None):
        """A repo root holding the producer sources, optionally tampered with."""
        root = self.tempdir()
        for name in sa_acceptance.ACCEPTANCE_PRODUCER_SOURCES:
            if edit == "missing":
                continue
            target = root.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            data = REPO.joinpath(*name.split("/")).read_bytes()
            if edit == "modify":
                # what a weakened runner looks like: one check that cannot fail
                data += b"\n\ndef _always_pass(*a, **k):\n    return True\n"
            target.write_bytes(data)
        return root

    def test_the_producer_sources_exist_and_name_the_acceptance_runner(self):
        self.assertIn("tests/secure_apps_interactive.py",
                      sa_acceptance.ACCEPTANCE_PRODUCER_SOURCES)
        for name in sa_acceptance.ACCEPTANCE_PRODUCER_SOURCES:
            self.assertTrue(REPO.joinpath(*name.split("/")).is_file(), name)
        self.assertEqual(Path(sa_acceptance.ACCEPTANCE_SCRIPT).resolve(),
                         REPO.joinpath("tests", "secure_apps_interactive.py").resolve())

    def test_the_fingerprint_measures_the_real_runner(self):
        current = sa_acceptance.acceptance_producer_fingerprint()
        self.assertRegex(current, r"^[0-9a-f]{64}$")
        self.assertEqual(sa_acceptance.acceptance_producer_fingerprint(
            str(self._repo())), current)
        self.assertNotEqual(current, sa_acceptance.implementation_fingerprint(),
                            "the producer and the implementation must not share "
                            "a digest; a swap between them would go unnoticed")

    def test_a_modified_runner_changes_the_fingerprint(self):
        self.assertNotEqual(
            sa_acceptance.acceptance_producer_fingerprint(str(self._repo("modify"))),
            sa_acceptance.acceptance_producer_fingerprint())

    def test_a_missing_runner_cannot_be_measured(self):
        self.assertIsNone(
            sa_acceptance.acceptance_producer_fingerprint(str(self._repo("missing"))))

    def test_line_endings_alone_are_not_a_change(self):
        root = self._repo()
        target = root.joinpath(*sa_acceptance.ACCEPTANCE_PRODUCER_SOURCES[0].split("/"))
        data = target.read_bytes().replace(b"\r\n", b"\n")
        target.write_bytes(data.replace(b"\n", b"\r\n"))
        self.assertEqual(sa_acceptance.acceptance_producer_fingerprint(str(root)),
                         sa_acceptance.acceptance_producer_fingerprint())

    def test_every_module_that_decides_a_verdict_is_fingerprinted(self):
        """If the checks or the aggregation move, the tuple must follow them."""
        covered = {REPO.joinpath(*name.split("/")).resolve()
                   for name in sa_acceptance.ACCEPTANCE_PRODUCER_SOURCES}
        deciders = (interactive.AcceptanceRun,          # PASS/FAIL/SKIP -> gates
                    interactive.AcceptanceRun.gates,
                    interactive.AcceptanceRun.complete,
                    interactive.DisposableAcceptance,   # runs the checks
                    interactive.DisposableAcceptance.finalize,
                    interactive.scan_audit,
                    interactive.disposable_registry_document)
        for decider in deciders:
            path = Path(inspect.getsourcefile(decider)).resolve()
            self.assertIn(path, covered,
                          "%s decides part of the verdict but is not "
                          "fingerprinted" % getattr(decider, "__qualname__", decider))
        # CHECKS is data rather than code: bind the module that defines it
        self.assertIn(Path(interactive.__file__).resolve(), covered)

    def test_the_probe_reports_the_producer_and_the_integrity_level(self):
        self.assertIn("acceptance_producer_fingerprint", interactive.EVIDENCE_FIELDS)
        self.assertIn("profile_security_fingerprint", interactive.EVIDENCE_FIELDS)
        self.assertIn("broker_medium_integrity", interactive.EVIDENCE_FIELDS)


# ═══════════════════════════════════ the profile's security configuration
class ProfileSecurityFingerprintTests(TempDirCase):
    """Against the real shipped profile, not a stand-in."""

    #: Everything the directive requires the fingerprint to cover, as the
    #: path it takes through the canonical document.
    REQUIRED = (
        ("authentication", "provider"),
        ("authentication", "user_verification"),
        ("storage", "backend"),
        ("storage", "mount_path"),
        ("policy", "mode"),
        ("policy", "idle_timeout_minutes"),
        ("policy", "lock_on_windows_lock"),
        ("policy", "lock_on_suspend"),
        ("policy", "lock_on_logoff"),
        ("policy", "lock_on_shutdown"),
        ("policy", "unmount_when_app_closes"),
        ("policy", "force_terminate_after_timeout"),
        ("policy", "graceful_close_timeout_seconds"),
        ("policy", "process_exit_confirm_timeout_seconds"),
        ("policy", "conditions"),
    )

    def setUp(self):
        self.managed = self.tempdir()
        registry = sa_config.load_registry(sa_config.default_registry_path(),
                                           managed_root=str(self.managed))
        self.profile = registry.get("obsidian")
        self.base = sa_acceptance.profile_security_fingerprint(self.profile)

    def _policy(self, policy=None, conditions=None, **changes):
        clone = sa_config.Policy()
        for name in sa_config.Policy.__slots__:
            setattr(clone, name, getattr(policy or self.profile.policy, name))
        clone.conditions = list(conditions if conditions is not None
                                else clone.conditions)
        for name, value in changes.items():
            setattr(clone, name, value)
        return clone

    def _profile(self, **changes):
        clone = sa_config.Profile()
        for name in sa_config.Profile.__slots__:
            setattr(clone, name, getattr(self.profile, name))
        for name, value in changes.items():
            setattr(clone, name, value)
        return clone

    def assertMoved(self, changed, label):
        self.assertNotEqual(sa_acceptance.profile_security_fingerprint(changed),
                            self.base, label)

    def assertUnmoved(self, changed, label):
        self.assertEqual(sa_acceptance.profile_security_fingerprint(changed),
                         self.base, label)

    # -- shape --------------------------------------------------------------
    def test_the_fingerprint_is_a_sha256_of_a_canonical_document(self):
        document = sa_acceptance.profile_security_document(self.profile)
        payload = sa_acceptance.canonical_json(document)
        self.assertRegex(self.base, r"^[0-9a-f]{64}$")
        self.assertEqual(self.base, hashlib.sha256(
            payload.encode("utf-8")).hexdigest())
        self.assertNotIn(", ", payload)
        self.assertNotIn(": ", payload)
        self.assertEqual(json.loads(payload), document)
        # the same facts in another key order are the same bytes
        shuffled = dict(reversed(list(document.items())))
        self.assertEqual(sa_acceptance.canonical_json(shuffled), payload)
        # UTF-8, not escaped ASCII: one byte sequence per value
        self.assertEqual(sa_acceptance.canonical_json({"b": 1, "a": "ÕÄÖÜ"}),
                         '{"a":"ÕÄÖÜ","b":1}')

    def test_the_document_carries_every_required_security_field(self):
        document = sa_acceptance.profile_security_document(self.profile)
        for section, field in self.REQUIRED:
            self.assertIn(field, document[section], "%s.%s" % (section, field))

    def test_the_document_carries_nothing_cosmetic(self):
        payload = sa_acceptance.canonical_json(
            sa_acceptance.profile_security_document(self.profile))
        for field in sa_acceptance.PROFILE_COSMETIC_FIELDS:
            self.assertNotIn('"%s"' % field, payload)
        # the shipped profile's prose, which must not cost an acceptance run
        self.assertTrue(self.profile.notes, "the fixture lost its notes")
        self.assertNotIn(self.profile.notes[:60], payload)

    def test_only_the_digest_is_persisted(self):
        record = sa_acceptance.build_record(ACCEPTED_ENVIRONMENT, self.profile,
                                            all_gates())
        self.assertEqual(record["profile_security_fingerprint"], self.base)
        text = json.dumps(record)
        self.assertNotIn(self.profile.mount_path, text)
        self.assertNotIn(self.profile.container, text)
        self.assertNotIn(self.profile.executable, text)

    # -- every security field moves it --------------------------------------
    def test_every_security_relevant_profile_field_moves_the_fingerprint(self):
        for name, value in (
                ("id", "other"),
                ("enabled", False),
                ("provider", "external-helper-fido2-hmac-secret"),
                ("credential_profile", "secondary"),
                ("helper_executable", r"C:\helpers\fido.exe"),
                ("user_verification", "preferred"),
                ("backend", "other-backend"),
                ("container", r"D:\elsewhere\vault.vhdx"),
                ("container_id", "obsidian-vault-2"),
                ("mount_path", r"V:\test-vaults\elsewhere"),
                ("executable", r"C:\Program Files\Other\Other.exe"),
                ("working_directory", r"C:\Program Files\Other"),
                ("arguments", ["--enable-everything"]),
                ("vault_argument_style", "none"),
                ("allow_unmanaged_executable", True)):
            self.assertMoved(self._profile(**{name: value}), name)

    def test_every_security_relevant_policy_field_moves_the_fingerprint(self):
        for name, value in (
                ("mode", "aggressive"),
                ("idle_timeout_minutes", 30),
                ("lock_on_windows_lock", False),
                ("lock_on_suspend", False),
                ("lock_on_logoff", False),
                ("lock_on_shutdown", False),
                ("lock_on_broker_shutdown", False),
                ("unmount_when_app_closes", False),
                ("graceful_close_timeout_seconds", 5),
                ("force_terminate_after_timeout", False),
                ("process_exit_confirm_timeout_seconds", 5),
                ("unmount_timeout_seconds", 5)):
            self.assertMoved(self._profile(policy=self._policy(**{name: value})), name)

    def test_dropping_a_typed_condition_moves_the_fingerprint(self):
        for dropped in [c.type for c in self.profile.policy.conditions]:
            kept = [c for c in self.profile.policy.conditions if c.type != dropped]
            self.assertMoved(self._profile(policy=self._policy(conditions=kept)),
                             dropped)

    def test_changing_a_condition_parameter_moves_the_fingerprint(self):
        conditions = [c for c in self.profile.policy.conditions
                      if c.type != "require_auth_provider"]
        conditions.append(sa_config.Condition(
            "require_auth_provider",
            {"provider": "external-helper-fido2-hmac-secret"}))
        self.assertMoved(self._profile(policy=self._policy(conditions=conditions)),
                         "require_auth_provider.provider")

    def test_adding_a_condition_moves_the_fingerprint(self):
        conditions = list(self.profile.policy.conditions)
        conditions.append(sa_config.Condition("require_auth_after_minutes",
                                              {"minutes": 5}))
        self.assertMoved(self._profile(policy=self._policy(conditions=conditions)),
                         "require_auth_after_minutes")

    # -- and nothing else does ----------------------------------------------
    def test_the_fingerprint_is_stable_across_calls_and_reloads(self):
        registry = sa_config.load_registry(sa_config.default_registry_path(),
                                           managed_root=str(self.managed))
        self.assertEqual(sa_acceptance.profile_security_fingerprint(
            registry.get("obsidian")), self.base)

    def test_reordering_conditions_does_not_move_the_fingerprint(self):
        self.assertUnmoved(self._profile(policy=self._policy(
            conditions=list(reversed(self.profile.policy.conditions)))),
            "reversed conditions")

    def test_cosmetic_changes_do_not_move_the_fingerprint(self):
        for name, value in (("label", "Renamed"),
                            ("notes", "a different paragraph of prose"),
                            ("size_gb", 64),
                            ("filesystem_label", "SAITULS-OTHER")):
            self.assertUnmoved(self._profile(**{name: value}), name)

    def test_an_unmeasurable_profile_has_no_fingerprint(self):
        self.assertIsNone(sa_acceptance.profile_security_fingerprint(None))
        self.assertIsNone(sa_acceptance.profile_security_fingerprint(object()))


# ══════════════════════════════════════ the medium-integrity architecture
class BrokerIntegrityRecordTests(TempDirCase):
    """An elevated run cannot record readiness, whatever its checks said."""

    def _record(self, broker_elevated, gates=None, complete=True):
        return sa_acceptance.build_record(
            dict(ACCEPTED_ENVIRONMENT, broker_elevated=broker_elevated),
            profile_facts(), all_gates() if gates is None else gates,
            complete=complete)

    def test_a_medium_run_with_every_gate_green_is_ready(self):
        record = self._record(False)
        self.assertIs(record["broker_elevated"], False)
        self.assertTrue(record["migration_ready"])

    def test_no_other_integrity_value_is_ever_ready(self):
        for value in (True, None, 1, 0, "false", "medium"):
            record = self._record(value)
            self.assertFalse(record["migration_ready"], repr(value))
            self.assertTrue(all(record[flag] for flag in sa_acceptance.GATE_FLAGS),
                            "the gates themselves stayed green: readiness was "
                            "withheld for the integrity level alone")

    def test_an_elevated_green_run_is_refused_by_the_gate_it_wrote(self):
        elevated = dict(ACCEPTED_ENVIRONMENT, broker_elevated=True)
        state = str(self.tempdir())
        sa_acceptance.save_record(state, sa_acceptance.build_record(
            elevated, profile_facts(), all_gates()))
        verdict = sa_acceptance.evaluate(state, FakeProfile(), dict(elevated))
        self.assertFalse(verdict.ok)
        self.assertTrue(any(sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY in r
                            for r in verdict.reasons), verdict.reasons)


@unittest.skipUnless(os.name == "nt", "Windows integrity levels")
class DisposableAcceptanceIntegrityTests(TempDirCase):
    """The run refuses an elevated process before anything exists to clean up."""

    def setUp(self):
        self.root = self.tempdir()
        self.managed = self.root / "managed"
        self.args = types.SimpleNamespace(
            registry=sa_config.default_registry_path(), managed_root=str(self.managed),
            profile="obsidian", mount_parent=str(self.root / "mounts"),
            evidence=str(self.root / "evidence.json"), size_gb=None, additional=False)

    def _run(self, elevated):
        lines = []
        with mock.patch.object(sa_auth, "_process_is_elevated", lambda: elevated):
            acceptance = interactive.DisposableAcceptance(
                self.args, reader=lambda text: "no", echo=lines.append)
            code = acceptance.execute()
        return code, lines

    def test_an_elevated_run_refuses_and_creates_nothing(self):
        code, lines = self._run(True)
        joined = "\n".join(lines)
        self.assertEqual(code, 2, joined)
        self.assertIn("FAIL: " + sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY,
                      joined)
        self.assertNotIn("Start the disposable hardware acceptance?", joined)
        for token in sa_acceptance.FINAL_TOKENS:
            self.assertNotIn(token, joined)
        self.assertFalse(self.managed.exists()
                         and any(self.managed.rglob("*.vhdx")))
        self.assertFalse(Path(sa_acceptance.record_path(
            str(self.managed / "state"))).exists())
        self.assertFalse((self.root / "mounts").exists())

    def test_a_uac_disabled_host_is_told_what_is_actually_wrong(self):
        """EnableLUA=0 leaves no medium shell to start from; say so."""
        with mock.patch.object(sa_acceptance, "uac_enabled", lambda: False):
            _code, lines = self._run(True)
        joined = "\n".join(lines)
        self.assertIn("FAIL: " + sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY,
                      joined)
        self.assertIn("EnableLUA=0", joined)
        self.assertIn("standard user", joined)

    def test_an_unreadable_integrity_level_refuses_too(self):
        code, lines = self._run(None)
        self.assertEqual(code, 2)
        self.assertIn("FAIL: " + sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY,
                      "\n".join(lines))

    def test_an_elevated_run_does_not_destroy_an_earlier_green_record(self):
        """An accidental elevated launch must not cost a key-in-hand run."""
        state = str(self.managed / "state")
        os.makedirs(state, exist_ok=True)
        with mock.patch.object(sa_auth, "_process_is_elevated", lambda: False):
            registry = sa_config.load_registry(self.args.registry,
                                               managed_root=self.args.managed_root)
            record = sa_acceptance.build_record(
                sa_acceptance.probe_environment(), registry.get("obsidian"),
                all_gates())
            sa_acceptance.save_record(state, record)
        self._run(True)
        kept = json.loads(Path(sa_acceptance.record_path(state)).read_text(
            encoding="utf-8"))
        self.assertEqual(kept, record)

    def test_a_medium_run_reaches_the_confirmation_prompt(self):
        code, lines = self._run(False)
        joined = "\n".join(lines)
        self.assertEqual(code, 1, joined)
        self.assertIn("broker integrity     medium", joined)
        self.assertIn("not started; nothing was created", joined)


# ══════════════════════════════════════════════ run -> record -> production gate
class AcceptanceToMigrationGateTests(TempDirCase):
    """What a finished run leaves behind is exactly what migrate accepts."""

    #: What a host with the privileged helper installed measures. These tests
    #: force every CHECK to pass, so they must also pretend the environment
    #: those checks would have run in -- a registered, fingerprinted task.
    #: That an ABSENT task really blocks migration has its own test below.
    INSTALLED_PRIVILEGED = {
        "privileged_launch_mode": sa_privtask.LAUNCH_MODE,
        "privileged_task_name": sa_privtask.TASK_PATH,
        "privileged_task_definition_fingerprint": "f" * 64,
        # The record binds the task SECURITY descriptor and the protected
        # RUNTIME BUNDLE as well as the definition. A host where either cannot
        # be measured can never match a record -- which is the correct,
        # fail-closed answer, and is why a fixture that pretends the helper is
        # installed has to pretend all three are measurable.
        "privileged_task_security_fingerprint": "e" * 64,
        "privileged_runtime_bundle_fingerprint": "d" * 64,
    }

    def setUp(self):
        # The suite must not depend on the integrity level of the shell it was
        # started from: the run's own refusal to accept an elevated broker has
        # its own tests below. Everything else here stays real.
        medium = mock.patch.object(sa_auth, "_process_is_elevated", lambda: False)
        medium.start()
        self.addCleanup(medium.stop)
        installed = mock.patch.object(sa_acceptance, "privileged_launch_facts",
                                      lambda: dict(self.INSTALLED_PRIVILEGED))
        installed.start()
        self.addCleanup(installed.stop)
        self.root = self.tempdir()
        self.managed = self.root / "managed"
        self.args = types.SimpleNamespace(
            registry=sa_config.default_registry_path(), managed_root=str(self.managed),
            profile="obsidian", mount_parent=str(self.root / "mounts"),
            evidence=str(self.root / "evidence.json"), size_gb=None, additional=False)

    def _acceptance(self, lines):
        acceptance = interactive.DisposableAcceptance(self.args, reader=lambda text: "no",
                                                      echo=lines.append)
        self.assertTrue(acceptance.prepare(), lines)
        return acceptance

    def _finish(self, acceptance, skip=None):
        for check_id, _flag, _text in interactive.CHECKS:
            if check_id == skip:
                acceptance.run.skip(check_id, "test")
            else:
                acceptance.run.check(check_id, True)
        with contextlib.redirect_stdout(io.StringIO()):
            return acceptance.finalize()

    def _check_acceptance(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = sa_cli.main(["--registry", self.args.registry,
                                "--managed-root", self.args.managed_root,
                                "migrate", "obsidian", "--check-acceptance"])
        return code, stdout.getvalue()

    def test_declining_to_start_creates_nothing_and_changes_no_record(self):
        lines = []
        acceptance = interactive.DisposableAcceptance(self.args, reader=lambda text: "no",
                                                      echo=lines.append)
        self.assertEqual(acceptance.execute(), 1)
        self.assertFalse(self.managed.exists() and any(self.managed.rglob("*.vhdx")))
        self.assertFalse(Path(sa_acceptance.record_path(
            str(self.managed / "state"))).exists())
        self.assertFalse((self.root / "mounts").exists())

    def test_a_green_run_is_recognised_by_the_production_migrate_gate(self):
        code, out = self._check_acceptance()
        self.assertEqual(code, sa_cli.EXIT_REFUSED)
        self.assertEqual(out.splitlines()[0], sa_acceptance.MIGRATION_BLOCKED)

        lines = []
        acceptance = self._acceptance(lines)
        self.assertEqual(self._finish(acceptance), 0, "\n".join(lines))
        for token in sa_acceptance.FINAL_TOKENS:
            self.assertIn(token, lines)

        code, out = self._check_acceptance()
        self.assertEqual(code, sa_cli.EXIT_OK, out)
        self.assertEqual(out.splitlines()[:4], ["HARDWARE_ACCEPTANCE_RECOGNIZED"]
                         + list(sa_acceptance.FINAL_TOKENS))

    def test_a_skipped_check_prints_no_token_and_revokes_an_earlier_green_record(self):
        first = self._acceptance([])
        self.assertEqual(self._finish(first), 0)
        self.assertEqual(self._check_acceptance()[0], sa_cli.EXIT_OK)

        lines = []
        second = self._acceptance(lines)
        self.assertEqual(self._finish(second, skip="F10"), 1)
        for line in lines:
            for token in sa_acceptance.FINAL_TOKENS:
                self.assertNotIn(token, line)
        record = json.loads(Path(sa_acceptance.record_path(
            str(self.managed / "state"))).read_text(encoding="utf-8"))
        self.assertFalse(record["migration_ready"])
        self.assertFalse(record["fido2_hardware_accepted"])
        code, out = self._check_acceptance()
        self.assertEqual(code, sa_cli.EXIT_REFUSED)
        self.assertIn("fido2_hardware_accepted was not accepted", out)

    def _producer_repo(self, edit):
        """The repo root the gate measures the acceptance producer from."""
        root = self.tempdir()
        for name in sa_acceptance.ACCEPTANCE_PRODUCER_SOURCES:
            if edit == "missing":
                continue
            target = root.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            data = REPO.joinpath(*name.split("/")).read_bytes()
            if edit == "modify":
                data += b"\n\ndef _always_pass(*a, **k):\n    return True\n"
            target.write_bytes(data)
        return str(root)

    def test_a_modified_acceptance_producer_invalidates_a_green_record(self):
        self.assertEqual(self._finish(self._acceptance([])), 0)
        self.assertEqual(self._check_acceptance()[0], sa_cli.EXIT_OK)
        with mock.patch.object(sa_acceptance, "REPO_ROOT",
                               self._producer_repo("modify")):
            code, out = self._check_acceptance()
        self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
        self.assertEqual(out.splitlines()[0], sa_acceptance.MIGRATION_BLOCKED)
        self.assertIn("acceptance producer changed", out)

    def test_a_missing_acceptance_producer_invalidates_a_green_record(self):
        self.assertEqual(self._finish(self._acceptance([])), 0)
        self.assertEqual(self._check_acceptance()[0], sa_cli.EXIT_OK)
        with mock.patch.object(sa_acceptance, "REPO_ROOT",
                               self._producer_repo("missing")):
            code, out = self._check_acceptance()
        self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
        self.assertEqual(out.splitlines()[0], sa_acceptance.MIGRATION_BLOCKED)
        self.assertIn("acceptance producer cannot be determined", out)

    def test_a_security_policy_edit_invalidates_a_green_record(self):
        """Each field the directive names, one at a time, end to end."""
        self.assertEqual(self._finish(self._acceptance([])), 0)
        self.assertEqual(self._check_acceptance()[0], sa_cli.EXIT_OK)
        registry = sa_config.load_registry(self.args.registry,
                                           managed_root=self.args.managed_root)
        profile = registry.get("obsidian")
        edits = (
            ("policy", "lock_on_windows_lock", False),
            ("policy", "idle_timeout_minutes", 15),
            ("policy", "mode", "aggressive"),
            ("profile", "mount_path", r"V:\somewhere\else"),
            ("profile", "user_verification", "preferred"),
            ("profile", "backend", "other-backend"),
        )
        for owner, field, value in edits:
            target = profile.policy if owner == "policy" else profile
            saved = getattr(target, field)
            setattr(target, field, value)
            try:
                with mock.patch.object(sa_cli, "load", lambda args: registry):
                    code, out = self._check_acceptance()
                self.assertEqual(code, sa_cli.EXIT_REFUSED, "%s: %s" % (field, out))
                self.assertIn("security-relevant profile configuration changed", out,
                              field)
            finally:
                setattr(target, field, saved)
        conditions = list(profile.policy.conditions)
        profile.policy.conditions = [c for c in conditions
                                     if c.type != "require_auth_after_windows_lock"]
        try:
            with mock.patch.object(sa_cli, "load", lambda args: registry):
                code, out = self._check_acceptance()
            self.assertEqual(code, sa_cli.EXIT_REFUSED, out)
            self.assertIn("security-relevant profile configuration changed", out)
        finally:
            profile.policy.conditions = conditions
        with mock.patch.object(sa_cli, "load", lambda args: registry):
            self.assertEqual(self._check_acceptance()[0], sa_cli.EXIT_OK,
                             "restoring every edit must restore acceptance")

    def test_an_aborted_run_is_not_ready_even_if_its_checks_passed(self):
        lines = []
        acceptance = self._acceptance(lines)
        acceptance.run.abort("cancelled by the operator")
        self.assertEqual(self._finish(acceptance), 1)
        self.assertEqual(self._check_acceptance()[0], sa_cli.EXIT_REFUSED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
