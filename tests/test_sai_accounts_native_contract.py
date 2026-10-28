#!/usr/bin/env python3
"""Native spawn contract for the SAI Accounts managed-context broker.

Run:  python tests/test_sai_accounts_native_contract.py
Exit: 0 = all PASS, 1 = failures.

These are the checks that unit tests CAN prove about the T-232 spawn path, plus
one they cannot. What is asserted here is the interop surface itself: the exact
CreateProcessWithLogonW ABI, the logon-flag namespace, the fact that the logon
happens inside the operator's session rather than in the service, the survival of
the broker's fail-closed SID check, and that no credential reaches a command line,
an environment block, the log, or a long-lived channel.

LIMITATION, stated on purpose: none of this proves a broker actually starts under
a second identity on a live desktop. Only the live gates do (see T-232 Gate A-F).
A static contract test that passes while the machine is broken is worse than no
test, so the live check is not simulated here and not marked covered.
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.sep + '..')
HELPER = os.path.join(ROOT, 'Scripts', 'accounts', 'SaiAccountsHelper.cs')
ENGINE = os.path.join(ROOT, 'Scripts', 'accounts', 'SaiAccounts.cs')
GUI = os.path.join(ROOT, 'SAITULS.cs')
DEPLOY = os.path.join(ROOT, 'Scripts', 'accounts', 'build.ps1')

FAILURES = []
PASSES = []


def check(name, ok, detail=''):
    if ok:
        PASSES.append(name)
        print('PASS  ' + name)
    else:
        FAILURES.append(name + (' :: ' + detail if detail else ''))
        print('FAIL  ' + name + (' :: ' + detail if detail else ''))


def read(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def strip_comments(src):
    src = re.sub(r'/\*.*?\*/', ' ', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


def after(src, marker, occurrence=0):
    """Everything from the Nth occurrence of marker onward."""
    idx = -1
    for _ in range(occurrence + 1):
        idx = src.find(marker, idx + 1)
        if idx < 0:
            return ''
    return src[idx:]


def test_cpwlw_abi():
    """P0-1: eleven parameters, in the Win32 order, with no phantom hToken."""
    src = read(HELPER)
    m = re.search(r'extern\s+bool\s+CreateProcessWithLogonW\s*\(([^;]*?)\);', src, re.S)
    check('cpwlw declared', m is not None)
    if not m:
        return
    params = [p.strip() for p in re.sub(r'\s+', ' ', m.group(1)).split(',') if p.strip()]
    check('cpwlw takes 11 parameters', len(params) == 11,
          'got %d: %s' % (len(params), params))
    # "string lpUsername" -> "lpUsername": the name is the token after the type.
    names = [p.split(' ')[-1] for p in params]
    expected = ['lpUsername', 'lpDomain', 'lpPassword', 'dwLogonFlags',
                'lpApplicationName', 'lpCommandLine', 'dwCreationFlags',
                'lpEnvironment', 'lpCurrentDirectory', 'lpStartupInfo',
                'lpProcessInformation']
    check('cpwlw parameter order matches Win32', names == expected,
          'got %s' % names)
    # The phantom hToken was the whole bug: an extra IntPtr between the flag and
    # the application name shifts every later argument.
    check('cpwlw has no phantom hToken', not any(n == 'hToken' for n in names), str(names))


def test_logon_flags():
    """P0-2: LOGON_WITH_PROFILE, and never LOGON_NETCREDENTIALS_ONLY."""
    src = strip_comments(read(HELPER))
    # Skip the declaration; only the CALL is a logon site. after() returns text
    # beginning AT the marker, so the argument list starts with '('.
    call = after(src, 'CreateProcessWithLogonW(', 1)
    # The call sits inside an if(...), so it ends '))' rather than ');'.
    m = re.match(r'CreateProcessWithLogonW\s*\((.*?)\)\s*\)', call, re.S)
    check('cpwlw call site found', m is not None)
    check('cpwlw is called exactly once', src.count('CreateProcessWithLogonW(') == 2,
          '%d occurrences (1 declaration + 1 call expected)' % src.count('CreateProcessWithLogonW('))
    if not m:
        return
    args = re.sub(r'\s+', ' ', m.group(1))
    check('logon flag is LOGON_WITH_PROFILE', 'LOGON_WITH_PROFILE' in args, args[:200])
    check('netcredentials-only is never passed', 'LOGON_NETCREDENTIALS_ONLY' not in args)
    check('LOGON32_LOGON_INTERACTIVE is gone', 'LOGON32_LOGON_INTERACTIVE' not in src)
    # LOGON_NETCREDENTIALS_ONLY leaves the child on the caller's token, which for a
    # LocalSystem helper means a SYSTEM broker that passes as the managed account.
    uses = [m2 for m2 in re.finditer(r'LOGON_NETCREDENTIALS_ONLY\s*[,;)]', src)]
    check('LOGON_NETCREDENTIALS_ONLY is declared but never used', not uses,
          '%d use(s)' % len(uses))


def test_session_bridge():
    """P0-3: the logon is performed in the operator's session, not in session 0."""
    src = read(HELPER)
    for api in ('WTSEnumerateSessionsW', 'WTSQueryUserToken', 'DuplicateTokenEx',
                'CreateProcessAsUser', 'CreateEnvironmentBlock'):
        check('session bridge declares ' + api, api in src)
    check('operator session is discovered, not hardcoded',
          'WTSGetActiveConsoleSessionId' not in strip_comments(src))
    check('launcher mode is reachable', '--in-session-spawn' in src)
    # The method body, not the Main dispatch that mentions the name last.
    marker = 'public static int RunInSessionSpawn('
    check('in-session launcher method exists', marker in src)
    launcher = after(src, marker)
    service_side = src[:src.find(marker)]
    check('the logon lives in the in-session launcher', 'CreateProcessWithLogonW' in launcher)
    check('the service does not perform the logon itself',
          'LogonUser' not in strip_comments(service_side))
    # lpDesktop must stay null: naming winsta0\default from session 0 is fatal.
    check('launcher leaves lpDesktop null', re.search(
        r'lpDesktop\s*=\s*null', launcher) is not None)
    # A Unicode environment block without CREATE_UNICODE_ENVIRONMENT fails with
    # ERROR_INVALID_PARAMETER; this exact mistake cost a probe run.
    check('CREATE_UNICODE_ENVIRONMENT is passed to CreateProcessAsUser',
          re.search(r'CreateProcessAsUser\s*\([^;]*?CREATE_UNICODE_ENVIRONMENT', src, re.S) is not None)


def test_engine_spawns_are_unicode_aware():
    """Every engine CreateProcess that hands over a block must say it is UTF-16.

    A UTF-16 block without CREATE_UNICODE_ENVIRONMENT is read as ANSI, and the
    call fails with 87 (ERROR_INVALID_PARAMETER). That is not theoretical: it
    made every federated Claude/Codex console launch answer
    `launch_failed (CreateProcess failed: 87)` while the identical command with
    no block at all started fine. This is the same rule the helper already
    follows for CreateProcessAsUser, checked here at BOTH engine sites -- the
    usage capture and the new-console launch.
    """
    src = strip_comments(read(ENGINE))
    # The declaration is the only `extern bool CreateProcess(`, and it is not a
    # spawn site: skip its whole parameter list.
    decl = re.search(r'extern\s+bool\s+CreateProcess\s*\(', src)
    decl_span = (decl.start(), decl.end() + 500) if decl else (-1, -1)
    calls = [m.start() for m in re.finditer(r'\bCreateProcess\s*\(', src)
             if not (decl_span[0] <= m.start() < decl_span[1])]
    check('engine CreateProcess call sites found', len(calls) == 2,
          '%d call(s)' % len(calls))
    blind = []
    for at in calls:
        # The flag may be OR-ed into a local (the capture path builds
        # `createFlags` two lines earlier) or written inline, so read a window
        # on both sides of the call rather than the call alone.
        window = src[max(0, at - 900):at + 900]
        if 'CREATE_UNICODE_ENVIRONMENT' not in window:
            blind.append(src[at:at + 90].split('\n')[0].strip()[:70])
    check('every engine environment block is declared as Unicode', not blind, str(blind))


def test_sid_fail_closed():
    """The broker's identity check must survive every spawn change."""
    src = strip_comments(read(ENGINE))
    marker = 'static void RunBrokerInner('
    check('broker entry point found', marker in src)
    body = after(src, marker)
    guard = body[:body.find('if (!isTest')] if 'if (!isTest' in body else ''
    mismatch = after(body, 'if (!isTest')[:400]
    check('broker captures its own SID', 'currentSid' in guard)
    check('broker compares its own SID to the expected SID',
          'context_mismatch' in mismatch and 'expectedSid' in mismatch)
    check('the SID mismatch path exits before serving', 'return;' in mismatch)
    check('only test mode bypasses the SID check',
          bool(re.search(r'if\s*\(!isTest\s*&&\s*!string\.Equals\(currentSid,\s*expectedSid', src)))
    check('doctor covers managed_windows_user brokers',
          'IsWindowsUserBackend(S(acc, "execution_backend"))' in src)


def test_secret_containment():
    """No credential in a command line, an environment block, or the log."""
    src = read(HELPER)
    code = strip_comments(src)
    # The spawn command line is built from fixed fields only.
    m = re.search(r'string\s+cmdLine\s*=\s*"(.*?)";', code, re.S)
    check('spawn command line built', m is not None)
    if m:
        line = re.sub(r'\s+', ' ', m.group(1))
        check('command line carries no password', 'password' not in line.lower(), line)
    check('password never becomes an environment variable',
          not re.search(r'EnvironmentVariables\s*\[\s*"[^"]*[Pp]ass', code))
    check('no savecred anywhere', 'savecred' not in code.lower())
    launcher = after(code, 'public static int RunInSessionSpawn(')
    check('launcher receives the secret only over the handoff pipe',
          'NamedPipeClientStream' in launcher)
    check('the secret is wiped after use', 'Array.Clear' in launcher)


def test_ui_failure_rendering():
    """P0-4: a handled failure renders as a short line, never as the envelope."""
    src = strip_comments(read(GUI))
    say = re.search(r'public\s+string\s+Say\s*\{(.*?)\n\s*\}', src, re.S)
    check('CliRun.Say found', say is not None)
    if say:
        body = say.group(1)
        check('Say parses the envelope on failure too',
              'AccountsLastLine' in body and not re.search(r'!\s*Ok\s*\)\s*return\s+Last\b', body))
    check('the raw envelope is never returned as a fallback',
          not re.search(r'catch\s*\{\s*return\s+last;\s*\}', src))
    check('Connect reports progress before the wait',
          'connecting...' in src)
    connect = re.search(r'\bvoid\s+AccountsContext\(string\s+verb\).*?(?=\n        \w)', src, re.S)
    body = connect.group(0) if connect else ''
    check('Connect re-probes the account immediately',
          'LoadAccountsAsync()' in body and 'ProbeContextState(' in body,
          'AccountsContext body: ' + body[:200])


def test_deploy_restart():
    """P1: a deploy that does not restart SCM is not a deploy."""
    src = read(DEPLOY)
    check('deploy stops the service', 'Stop-Service' in src)
    check('deploy starts the service', 'Start-Service' in src)
    check('deploy elevates when it is not already elevated',
          'RunAs' in src and 'IsInRole' in src)
    check('deploy verifies the installed hash against the build hash',
          'Get-FileHash' in src and 'does not match the build hash' in src)
    check('deploy verifies the helper answers after restart',
          'helper status' in src)


def test_status_auth_state_present():
    """Every online route in status must carry auth_state -- local included.

    TWO defect sites, both verified live against the deployed engine on
    2026-10-04 by interleaved A/B (5 rounds, deployed then built within each
    round, so ordering alone cannot produce the result):

      managed  A managed account -- a Windows user other than the operator's
               own -- is answered over the named pipe. That reply can come
               back without a 'credential' object, and the code set
               context_state = ONLINE unconditionally while setting auth_state
               only inside `if (cred != null)`.

      local    The operator's OWN account answers from this process. The
               deployed build dropped auth_state here too, and that is the
               row the operator reads daily: the Account Manager draws its
               Re-authenticate button only when AuthState is AUTHENTICATED or
               AUTH_REQUIRED (SAITULS.cs), so an absent field silently
               removed the very button T-234's acceptance needs, while
               HumanAuth rendered "—" over a credential that was verified.

    Deployed (05:31) omitted it for AG1 in 5/5 rounds and answered
    AUTHENTICATED for AG2 in 5/5; the same-session build answered
    AUTHENTICATED for both, 5/5.

    An absent field and a field set to UNKNOWN are not the same bug -- UNKNOWN
    is a stated answer, absence is silence -- so the online path has to assign
    auth_state on every route out of it, in BOTH branches.
    """
    src = strip_comments(read(ENGINE))
    start = src.find('static int CmdStatus')
    check('CmdStatus found', start >= 0)
    if start < 0:
        return
    end = src.find('static int CmdUsage', start)
    body = src[start:end if end > start else len(src)]

    # --- local branch -----------------------------------------------------
    # CheckCredential() occurs exactly once in CmdStatus and only in the local
    # branch, so it is an unambiguous anchor -- unlike context_state = ONLINE,
    # which both branches write.
    local = body.find('CheckCredential()')
    pipe = body.find('SendPipeCommand')
    check('the local branch answers from this process', local >= 0 and pipe > local)
    if local < 0 or pipe <= local:
        return
    check('the local branch states auth_state',
          'Set(o, "auth_state"' in body[local:pipe],
          'the local row is the operator\'s own; without auth_state it loses its '
          'Re-authenticate button and renders "—" over a verified credential')

    # --- managed branch ---------------------------------------------------
    # Anchor on the pipe call, NOT on the first context_state = ONLINE, which
    # the local branch also writes.
    managed = body[pipe:]

    check('a null reply is answered UNKNOWN, not silence',
          re.search(r'reply\s*==\s*null[\s\S]{0,200}?auth_state[\s\S]{0,60}?"UNKNOWN"', managed) is not None,
          'a null pipe reply must state UNKNOWN explicitly')

    check('the managed branch reports ONLINE once it has a reply',
          re.search(r'Set\(o,\s*"context_state",\s*"ONLINE"\)', managed) is not None)

    # Both sub-routes of a successful reply must state an auth_state: the
    # credential-bearing one, and the one where the reply carried no
    # credential object at all.
    guard = re.search(r'if\s*\(\s*cred\s*!=\s*null\s*\)\s*\{(.*?)\}\s*(else)?', managed, re.S)
    check('the credential-bearing route was located', guard is not None,
          'managed branch: ' + managed[:220])
    if not guard:
        return
    check('a credential-bearing reply states auth_state',
          'Set(o, "auth_state"' in guard.group(1),
          'setting credential without auth_state is the defect this test exists for')
    check('a reply with no credential still states auth_state',
          guard.group(2) is not None and 'Set(o, "auth_state"' in managed[guard.end():guard.end() + 160],
          'a credential-less reply must say UNKNOWN rather than omit the field')


def test_signin_button_is_conditional():
    """SIGN IN is drawn only for AUTH_REQUIRED; Re-authenticate is not.

    Derived live 2026-10-04 from the real render guards against the real
    engine envelope, for both rows:

      AG1  ctx=ONLINE auth=''            -> Open console only.
                                           context_secondary=False (vac34 is us),
                                           and NO Re-authenticate, because the
                                           deployed engine omits auth_state.
      AG2  ctx=ONLINE auth=AUTHENTICATED -> Open console, Disconnect,
                                           Re-authenticate. NO SIGN IN.

    AG2 is signed in, so the button an operator would go looking for is not
    merely a no-op -- it is not drawn. That is the documented behaviour and it
    is what Wiki/ACCOUNTS-STATUS.md 7.2.1 now says; this check exists so the
    doc cannot drift back into "click SIGN IN" as if the button were always
    there, which is the mistake this ticket's own acceptance instructions made.
    """
    src = strip_comments(read(GUI))

    # The Authentication row draws SIGN IN under exactly one condition.
    m = re.search(r'if\s*\(\s*sel\.AuthState\s*==\s*"AUTH_REQUIRED"\s*\)', src)
    check('SIGN IN is gated on AUTH_REQUIRED', m is not None,
          'an ungated SIGN IN button would claim an account needs signing in '
          'when it does not')
    if m:
        window = src[m.start():m.start() + 600]
        check('the gated SIGN IN routes to the sign-in flow',
              'SignInSelectedAccount' in window,
              'the button must invoke the same flow the primary CTA uses')

    # Re-authenticate covers BOTH states, which is why a healthy account is not
    # left without a way back in.
    r = re.search(r'if\s*\(\s*sel\.AuthState\s*==\s*"AUTH_REQUIRED"\s*\|\|\s*sel\.AuthState\s*==\s*"AUTHENTICATED"\s*\)', src)
    check('Re-authenticate is offered for AUTHENTICATED too, not only when it is required', r is not None,
          'otherwise a signed-in account cannot be re-signed-in')

    # HumanState must not invent SIGN IN from a healthy online account.
    hs = re.search(r'static string HumanState\(AccountRow row\)(.*?)\n        \}', src, re.S)
    check('HumanState located', hs is not None)
    if hs:
        check('HumanState maps AUTH_REQUIRED to SIGN IN',
              re.search(r'AuthState\s*==\s*"AUTH_REQUIRED"\)\s*return\s*"SIGN IN"', hs.group(1)) is not None)
        check('HumanState does not map AUTHENTICATED to SIGN IN',
              'AuthState == "AUTHENTICATED"' not in hs.group(1).replace('\n', ' ').replace('  ', ' ')
              or 'return "SIGN IN"' not in hs.group(1).split('AuthState == "AUTHENTICATED"')[1][:120],
              'a signed-in account that reports SIGN IN is the defect this guards')

    # OPEN CONSOLE is the primary action for a healthy account, which is the
    # other half of "why the three buttons look alike".
    check('OPEN CONSOLE is the primary action for READY',
          re.search(r'case\s+"READY":\s*\n\s*case\s+"OFFLINE":\s*return AccountsLoc\.T\("OpenConsole"\)', src) is not None)


def test_no_fabricated_quota_percentages():
    """T-266: a quota STATE is never drawn as a percentage.

    Until T-266 the Accounts detail pane turned quota_state into a number --
    READY -> 30%, LOW -> 75%, EXHAUSTED -> 100% -- and drew it as a "Session"
    usage bar. No vendor ever sent those numbers. The usage section now draws
    only real windows from the limits source (Limits / LimitWindow), and a
    window without a reading reads "--".

    This check fails on the pre-T-266 source and must keep failing on any
    future change that maps READY/LOW/EXHAUSTED onto a number again.
    """
    gui = strip_comments(read(GUI))

    # 1. No branch on a quota state may produce a numeric literal percentage.
    fabricated = re.search(
        r'QuotaState\s*==\s*"(READY|LOW|EXHAUSTED)"[^;{}]*\?\s*\d+(\.\d+)?\s*:', gui)
    check('no quota_state -> percentage mapping', fabricated is None,
          fabricated.group(0)[:160] if fabricated else '')
    placeholder = re.search(r'"Session"\s*,\s*pct', gui)
    check('no synthetic "Session" usage bar', placeholder is None,
          placeholder.group(0) if placeholder else '')
    check('the placeholder quota bar helper is gone', 'DrawQuotaBar' not in gui)

    # 2. The usage section reads real windows and nothing else.
    body_start = gui.find('int DrawUsageSection(')
    body = gui[body_start:gui.find('\n        }\n', body_start)] if body_start >= 0 else ''
    check('usage section exists', body_start >= 0)
    check('usage section draws limits windows', 'LimitsFor(sel)' in body and 'la.Windows' in body,
          body[:200])
    check('usage section never reads the quota state', 'QuotaState' not in body, body[:200])

    # 3. An unavailable window renders "--", never a number.
    shown = re.search(r'public static string Shown\(LimitWindow w, bool showUsed\)\s*\{(.*?)\n        \}', gui, re.S)
    check('Shown() exists', shown is not None)
    check('unavailable reads "--"', shown is not None
          and re.search(r'!w\.Available\)\s*return "--"', shown.group(1)) is not None,
          shown.group(1)[:200] if shown else '')


def main():
    if not os.path.exists(HELPER):
        print('FAIL  helper source missing: ' + HELPER)
        return 1
    test_cpwlw_abi()
    test_logon_flags()
    test_session_bridge()
    test_engine_spawns_are_unicode_aware()
    test_sid_fail_closed()
    test_secret_containment()
    test_ui_failure_rendering()
    test_status_auth_state_present()
    test_signin_button_is_conditional()
    test_no_fabricated_quota_percentages()
    test_deploy_restart()

    print()
    print('NOTE  these tests prove the interop CONTRACT only.')
    print('NOTE  they do NOT prove a live AG2 broker starts on the desktop;')
    print('NOTE  that is T-232 Gates A-F and is still outstanding.')
    print()
    print('PASS %d / FAIL %d' % (len(PASSES), len(FAILURES)))
    if FAILURES:
        for f in FAILURES:
            print('  FAILED: ' + f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
