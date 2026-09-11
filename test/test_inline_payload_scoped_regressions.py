"""Scoped inline mint checks over inert strings; no fixture program is executed."""

from __future__ import annotations

import ast
import base64
import inspect
import re

import pytest

from kiro_crew import security
from kiro_crew.security import argv_floor, inline_payload

_MINT = "credential-exfil-kirocrew-token"
_ENCODED_MINT = "a2lyb2NyZXcgdG9rZW4="


def _rule_of(cmd: str) -> str | None:
    reason = security.is_denied(cmd)
    if reason is None:
        return None
    m = re.search(r"rule=(\S+)", reason)
    return m.group(1) if m else reason.splitlines()[0]


class TestAnEncodedLiteralIsDataUntilSomethingDecodesIt:
    @pytest.mark.parametrize(
        "payload",
        [
            f'print(len("{_ENCODED_MINT}"))',
            f'print("{_ENCODED_MINT}" in open("test/test_argv_floor.py").read())',
            f'fixtures = ["{_ENCODED_MINT}"]; print(len(fixtures))',
            f'import base64; print(base64.b64decode("aGVsbG8=")); print(len("{_ENCODED_MINT}"))',
            f'print("b64decode"); print(len("{_ENCODED_MINT}"))',
            f"print(\"b64decode('{_ENCODED_MINT}')\")",
        ],
    )
    def test_a_carried_literal_is_not_the_programs_own_text(self, payload):
        # shlex.quote is shell quoting only, never execution of the fixture.
        import shlex

        cmd = "python -c " + shlex.quote(payload)
        assert not argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) is None, cmd

    @pytest.mark.parametrize(
        "decoder", ["b64decode", "standard_b64decode", "urlsafe_b64decode", "decodebytes"]
    )
    def test_a_decoded_literal_is_still_read_when_a_decoder_runs(self, decoder):
        cmd = f"python -c 'import os,base64; os.system(base64.{decoder}(b\"{_ENCODED_MINT}\").decode())'"
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) == _MINT, cmd

    def test_only_decoder_arguments_are_decoded(self):
        payload = f'print(base64.b64decode("aGVsbG8=")); print("{_ENCODED_MINT}")'
        assert inline_payload._decoded_b64_literals(payload) == ("hello",)
        assert inline_payload._decoded_b64_literals(f'x = "{_ENCODED_MINT}"') == ()
        assert (
            inline_payload._decoded_b64_literals(f"print(\"b64decode('{_ENCODED_MINT}')\")") == ()
        )

    def test_nested_decoding_preserves_case_until_resolved(self):
        inner = base64.b64encode(b"import kiro_crew.cli").decode()
        outer = base64.b64encode(inner.encode()).decode()
        cmd = f"python -c 'exec(base64.b64decode(base64.b64decode(\"{outer}\")))'"
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd)
        assert _rule_of(cmd) == _MINT
        assert inline_payload._decoded_b64_literals(cmd) == (inner.lower(), "import kiro_crew.cli")

    def test_many_data_literals_do_not_spend_decode_work(self, monkeypatch):
        calls = 0
        real = inline_payload.base64.b64decode

        def count(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(inline_payload.base64, "b64decode", count)
        data = ", ".join(repr(_ENCODED_MINT) for _ in range(200))
        payload = f'x = [{data}]; print(base64.b64decode("aGVsbG8="))'
        assert inline_payload._decoded_b64_literals(payload) == ("hello",)
        assert calls == 1


class TestTheConsoleScriptIsAWholePathComponent:
    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -c \"import runpy; runpy.run_path('/w/kirocrew-scratch/kirocrew.py')\"",
            "py -3 -c \"import runpy; runpy.run_path(r'C:\\\\w\\\\kirocrew-scratch\\\\kirocrew.py')\"",
            "python3 -c \"exec(open('/w/kirocrew-scratch/kirocrew.py').read())\"",
            "python3 -c \"import runpy; runpy.run_path('/w/scratch/kirocrew.pyc')\"",
            "python3 -c \"exec(open('/w/scratch/kiro-crew.py').read())\"",
        ],
    )
    def test_a_user_file_named_for_the_product_is_not_the_program(self, cmd):
        assert not argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -c \"exec(open(shutil.which('kirocrew')).read())\"",
            "python3 -c \"exec(open('/opt/venv/bin/kirocrew').read())\"",
            "python3 -c \"exec(open(r'C:\\\\venv\\\\Scripts\\\\kirocrew.exe').read())\"",
            "python3 -c \"import runpy,shutil; runpy.run_path(shutil.which('kiro-crew'))\"",
        ],
    )
    def test_the_installed_entry_point_is_still_the_program(self, cmd):
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) == _MINT, cmd

    def test_the_boundary_is_the_component_not_a_prefix(self):
        search = inline_payload._CONSOLE_SCRIPT_LITERAL_RE.search
        for program in ("'kirocrew'", "'/venv/bin/kirocrew'", "kirocrew.exe'"):
            assert search(program), program
        for other in (
            "'kirocrew.py'",
            "'kirocrew.pyc'",
            "'/w/kirocrew/x.py'",
            "kirocrew_ws",
            "x.kirocrew",
        ):
            assert not search(other), other


class TestACallArgumentHasOneQuoteAwareBoundary:
    @pytest.mark.parametrize(
        "payload",
        [
            "exec(open([')', shutil.which('kirocrew')][1]).read())",
            "runpy.run_path(run_name=str(')'), path_name=shutil.which('kirocrew'))",
            "exec(open( # ) a comment\n shutil.which('kirocrew')).read())",
            "exec(open(['''single ' and )''', shutil.which('kirocrew')][1]).read())",
            "exec(open(['a\\')', shutil.which('kirocrew')][1]).read())",
        ],
    )
    def test_a_quoted_paren_cannot_hide_the_program(self, payload):
        assert inline_payload._inline_payload_reaches_cli(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            "exec('print(\"(\")'); print('kirocrew done')",
            "exec(open('a(b').read()); x = 'kirocrew'",
            "exec('''print(\"(\")'''); print('kirocrew done')",
            "exec(open('patch.py').read()); print('from kiro_crew.acp import x, secret hint')",
        ],
    )
    def test_later_prose_is_not_a_loader_argument(self, payload):
        assert not inline_payload._inline_payload_reaches_cli(payload)

    def test_an_unterminated_call_is_still_judged_on_everything_left(self):
        for payload in ("exec(" + "f(" * 200 + "'kirocrew'", "exec(" + "x" * 20000 + "'kirocrew'"):
            assert inline_payload._inline_payload_reaches_cli(payload), payload[:32]


class TestAResolvedCallableIsReadLikeTheNameWrittenAtTheCall:
    """``getattr`` names a runner or a loader in a string; the reach is unchanged."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # The runner reached immediately, and through an alias bound first.
            'python3 -c \'import runpy; getattr(runpy, "run_module")("kiro_crew")\'',
            'python3 -c \'import runpy; r = getattr(runpy, "run_module"); r("kiro_crew")\'',
            'python3 -c \'import importlib; getattr(importlib, "import_module")("kiro_crew")\'',
            # The attribute spelled in pieces, which the fold joins before matching.
            'python3 -c \'import runpy; getattr(runpy, "run_" "module")("kiro_crew")\'',
            # A loader reached the same way, handed the installed console script.
            'python3 -c \'import builtins,shutil; getattr(builtins, "exec")(open(shutil.which("kirocrew")).read())\'',
            'python3 -c \'import builtins; g = getattr(builtins, "exec"); g("from kiro_crew.cli import main")\'',
        ],
    )
    def test_an_indirected_runner_or_loader_is_still_the_program(self, cmd):
        assert argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) == _MINT, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            # The resolved callable is judged by what it is HANDED, as the written
            # name is: another file, or a package path, is not the package.
            'python3 -c \'import runpy; getattr(runpy, "run_path")("patch.py")\'',
            'python3 -c \'import runpy; getattr(runpy, "run_path")("src/kiro_crew/x.py")\'',
            # `getattr` on anything else stays generic code, product path or not.
            "python3 -c 'import json; print(getattr(json, \"dumps\")({}))'",
            'python3 -c \'import os; print(getattr(os, "getcwd")(), "src/kiro_crew")\'',
        ],
    )
    def test_an_indirection_to_anything_else_is_not_a_mint(self, cmd):
        assert not argv_floor._is_credential_mint(cmd.lower(), raw_text=cmd), cmd
        assert _rule_of(cmd) is None, cmd

    def test_the_attribute_comes_from_getattrs_own_second_argument(self):
        # A runner name in the DEFAULT slot is not what the call resolves to.
        assert not inline_payload._inline_payload_reaches_cli(
            'getattr(obj, "dumps", "run_module")("kiro_crew")'
        )
        # ... and a one-argument call resolves to nothing at all.
        assert not inline_payload._inline_payload_reaches_cli('getattr(runpy)("kiro_crew")')


class TestTheInspectionIsOnePassAndNotRecursive:
    def test_names_the_mint_does_not_call_itself(self):
        tree = ast.parse(inspect.getsource(inline_payload._names_the_mint))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_names_the_mint" not in called

    def test_nesting_is_still_read_without_the_rescan(self):
        assert inline_payload._inline_payload_reaches_cli(
            "exec(compile('import kiro_crew.config.loader as l; l.read_local_secret(1)', 'x', 'exec'))"
        )
        assert not inline_payload._inline_payload_reaches_cli(
            "exec(compile('from kiro_crew.acp import client', 'x', 'exec'))"
        )

    def test_nested_loaders_do_not_copy_or_scan_each_tail(self, monkeypatch):
        scanned = 0
        real = inline_payload._call_spans

        def count(view):
            nonlocal scanned
            scanned += len(view)
            return real(view)

        monkeypatch.setattr(inline_payload, "_call_spans", count)
        payload = "exec(" * 1200 + "'p.py'" + ")" * 1200
        assert not inline_payload._inline_payload_reaches_cli(payload)
        assert scanned <= 3 * len(payload)
        args = inline_payload._code_loader_arguments(payload)
        assert len(args) == 1
        assert sum(map(len, args)) <= len(payload)


class TestTheGenuineProtectionsSurvive:
    @pytest.mark.parametrize(
        "cmd",
        [
            "python -c 'from kiro_crew.cli import main; main()'",
            "python -c 'from kiro_crew.cli_server import _token; _token(None)'",
            "python -c 'from kiro_crew.config.loader import read_local_secret; print(read_local_secret(5476))'",
            "python -c 'from kiro_crew.instances import run_marker; print(run_marker.read_secret(5476))'",
            "python3 -c \"import runpy; runpy.run_module(mod_name='kiro_crew', run_name='__main__')\"",
            "python -c \"exec('from kiro_crew.config.loader import read_local_secret; f()')\"",
            "python3 - <<'PY'\nfrom kiro_crew.dashboard.token_auth import generate_token\nPY",
        ],
    )
    def test_the_mint_surface_is_still_denied(self, cmd):
        assert _rule_of(cmd) == _MINT, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 -c \"import ast; ast.parse(open('src/kiro_crew/security.py').read())\"",
            "python3 -c 'from kiro_crew.acp import client; print(client)'",
            "python3 -c \"exec(open('patch.py').read())\"",
            "python3 -c \"print('kiro_crew docs mention the token verb')\"",
        ],
    )
    def test_an_ordinary_mention_is_still_allowed(self, cmd):
        assert _rule_of(cmd) is None, cmd
