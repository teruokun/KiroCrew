"""Static mint-name checks for inline Python; payloads are never executed.

The argv floor chooses the payload. This module folds literal concatenation,
reads call boundaries with Python's tokenizer, and matches explicit mint names.
It is a heuristic, not an interpreter or an OS credential boundary. In particular,
.local_secret is readable inside the sandbox; its explicit readers stay protected.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import token
import tokenize

from .shell_normalizer import (
    _NESTED_SHELL_PROGRAMS,
    _PYTHON_PROGRAM_RE,
    _program_basename,
    _shell_tokens,
)
from .vocabulary import _SELF_NAME_RE

# A cheap trigger, not a verdict. Include stdlib base64 aliases before the floor
# short-circuits, without treating arbitrary eval/getattr calls as minting.
_INLINE_DYNAMIC_EXEC_RE = re.compile(
    r"\b__import__\s*\(|\bimportlib\b|\bimport_module\b|\brunpy\b|\brun_module\b|"
    r"\brun_path\b|\bexec\s*\(|\beval\s*\(|\bcompile\s*\(|"
    r"\b(?:(?:standard_|urlsafe_)?b64decode|decodebytes)\b|\bmarshal\b|\bgetattr\s*\("
)
# Keep explicit dispatch and token-producer paths, including stdin file carriers.
# Secret is not a path word: handlers/secrets.py is ordinary patch-script data.
_MINT_SURFACE_RE = re.compile(
    r"kiro_crew[./](?:cli|cli_server|__main__|_bootstrap)(?![a-z0-9_])"
    r"|kiro_crew[\w./]*token(?!iz)"
    r"|from\s+kiro_crew\s+import\b[^;]{0,120}?(?<![a-z0-9_.-])(?:cli|cli_server|__main__|_bootstrap)(?![a-z0-9_])"
)
_PRODUCT_IMPORT_RE = re.compile(
    r"(?:^|[;\n])\s*(?:from\s+kiro_crew(?:\.[\w.]*)?\s+import\b"
    r"|import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*kiro_crew\b)"
)
# Only consulted INSIDE a loader argument, never beside an unrelated loader.
# Nested compile string arguments need this anchor even without recursive scans.
_LOADED_IMPORT_RE = re.compile(
    r"""["']\s*(?:from\s+kiro_crew(?:\.[\w.]*)?\s+import\b"""
    r"|import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*kiro_crew\b)"
)
_DYNAMIC_RUNNER_RE = re.compile(
    r"(?<![a-z0-9_])(?:run_module|run_path|import_module|__import__)\s*\("
)
#: The same callables as the two call regexes, as bare names -- what a ``getattr``
#: argument spells when the payload declines to write the name at the call.
_DYNAMIC_RUNNER_NAMES = frozenset({"run_module", "run_path", "import_module", "__import__"})
_INLINE_CODE_LOADER_NAMES = frozenset({"exec", "eval", "compile", "run_path", "execfile"})
_GETATTR_CALL_RE = re.compile(r"(?<![a-z0-9_])getattr\s*\(")
_ATTRIBUTE_NAME_LITERAL_RE = re.compile(r"""(["'])([A-Za-z_]\w*)\1""")
_ALIAS_BINDING_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*\Z")
#: Matched with an explicit position, so it carries no string-start anchor:
#: ``\\A`` means offset zero even when ``match`` is given one, which reads every
#: call after the first as nameless.
_CALL_NAME_RE = re.compile(r"([A-Za-z_]\w*)")
_PACKAGE_LITERAL_RE = re.compile(r"(?<![a-z0-9_./\\-])kiro_crew(?![a-z0-9_/\\-])")
_INLINE_CODE_LOADER_RE = re.compile(r"(?<![a-z0-9_])(?:exec|eval|compile|run_path|execfile)\s*\(")
_CONSOLE_SCRIPT_LITERAL_RE = re.compile(
    r"(?<![a-z0-9_.-])kiro[-.]?crew(?:\.exe)?(?![a-z0-9_./\\-])"
)
_LOADER_ARG_DELIMITERS_RE = re.compile(
    r"""^\s*(?:[rbfu]{1,2})?(?:\"\"\"|'''|[\"'])|(?:\"\"\"|'''|[\"'])\s*$"""
)
_MINT_VERB_RE = re.compile(r"(?<![a-z0-9])(?:token|secret)(?![a-z0-9])")
_PLAIN_LITERAL_RE = re.compile(r"""(["'])([^"'\\\n]*)\1""")
_INLINE_B64_LITERAL_RE = re.compile(r"""\s*(?:[bBrR])?(["'])([A-Za-z0-9+/=_-]{8,})\1\s*""")
_B64_DECODE_CALL_RE = re.compile(
    r"(?:standard_b64decode|urlsafe_b64decode|b64decode|decodebytes)\s*\("
)
_B64_INPUT_KEYWORD_RE = re.compile(r"\s*([a-zA-Z_]\w*)\s*=\s*(?!=)")
_B64_MODULE_QUALIFIER_RE = re.compile(r"(?:[a-zA-Z_]\w*\.)+")
_PY_LINE_CONTINUATION_RE = re.compile(r"\\\r?\n[ \t]*")
#: How many times a payload's own quotes can survive shell splitting: its own word,
#: and one nested shell command (``bash -c '… python -c …'``). Deeper nesting is a
#: residual, not a silent claim -- an unreachable carrier is noted, never covered.
_SHELL_QUOTE_LEVELS = 2


def _lex(view: str):
    """Yield Python tokens with absolute offsets, retaining a malformed prefix.

    Python's lexer owns escapes, triple quotes and comments; a shell quote walk
    cannot substitute for its different string rules. No AST or execution occurs.
    """
    offsets = [0]
    for line in view.split("\n"):
        offsets.append(offsets[-1] + len(line) + 1)
    try:
        for item in tokenize.generate_tokens(io.StringIO(view).readline):
            yield item, offsets[item.start[0] - 1] + item.start[1], offsets[
                item.end[0] - 1
            ] + item.end[1]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return


def _fold_inline_literals(payload: str) -> str:
    """Join plain adjacent literals without touching nested string delimiters.

    Only whitespace or a plus may join tokens. Joining each run in one operation
    avoids both repeated passes and quadratic concatenation of growing strings.
    """
    view = _PY_LINE_CONTINUATION_RE.sub(" ", payload)
    out: list[str] = []
    pieces: list[str] = []
    start = end = cursor = 0
    quote = "'"
    for item, left, right in _lex(view):
        literal = _PLAIN_LITERAL_RE.fullmatch(item.string) if item.type == token.STRING else None
        if literal is None:
            continue
        if pieces and view[end:left].strip() not in ("", "+"):
            out.extend((view[cursor:start], quote, "".join(pieces), quote))
            cursor = end
            pieces = []
        if not pieces:
            start = left
            quote = literal.group(1)
        pieces.append(literal.group(2))
        end = right
    if pieces:
        out.extend((view[cursor:start], quote, "".join(pieces), quote))
        cursor = end
    out.append(view[cursor:])
    return "".join(out)


def _call_spans(view: str) -> list[tuple[int, int, int]]:
    """(name start, argument start, argument end), in opening order.

    One lexer pass pairs parentheses without counting strings or comments.
    Unclosed calls end at EOF, never a truncated prefix. Store indices instead
    of nested suffix copies; no Python recursion or per-call boundary rescan.
    """
    calls: list[tuple[int, int, int]] = []
    stack: list[int | None] = []
    previous = None
    for item, start, end in _lex(view):
        if item.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
            continue
        if item.type == token.OP and item.string == "(":
            index = None
            if previous is not None and previous[0].type == token.NAME:
                index = len(calls)
                calls.append((previous[1], end, len(view)))
            stack.append(index)
        elif item.type == token.OP and item.string == ")" and stack:
            index = stack.pop()
            if index is not None:
                name, opening, _ = calls[index]
                calls[index] = (name, opening, start)
        previous = (item, start)
    return calls


def _call_arguments(
    view: str, call_re: re.Pattern[str], spans: list[tuple[int, int, int]] | None = None
) -> tuple[str, ...]:
    """Disjoint outermost matching arguments; nested text is inspected once.

    Keeping all nested suffixes costs quadratic space even with a linear lexer.
    Outermost intervals include their nested names without copying those tails.
    """
    args: list[str] = []
    covered = -1
    for name, start, end in _call_spans(view) if spans is None else spans:
        if name < covered:
            continue
        match = call_re.match(view, name)
        if match is not None and match.end() == start:
            args.append(view[start:end])
            covered = end
    return tuple(args)


def _code_loader_arguments(view: str) -> tuple[str, ...]:
    """Arguments belonging to code loaders, excluding later statements."""
    return _call_arguments(view, _INLINE_CODE_LOADER_RE)


def _dynamic_runner_handed_the_package(view: str) -> bool:
    return any(_PACKAGE_LITERAL_RE.search(arg) for arg in _call_arguments(view, _DYNAMIC_RUNNER_RE))


def _indirect_call_arguments(
    view: str,
    wanted: frozenset[str],
    spans: list[tuple[int, int, int]],
    brackets: dict[int, list[tuple[int, int]]],
) -> tuple[str, ...]:
    """What a *wanted* callable is handed when the payload reaches it via ``getattr``.

    ``_DYNAMIC_RUNNER_RE`` and ``_INLINE_CODE_LOADER_RE`` read a name written AT
    the call, and a payload can decline to write it there: ``getattr(runpy,
    "run_module")(<package>)`` keeps the runner's name in a string, and binding the
    result first (``r = getattr(runpy, "run_module")``) moves the call another
    statement away. Both reach the same dispatch the direct spelling reaches, so
    both are read here -- and read the SAME WAY, by what the call is handed, which
    is what keeps ``getattr(runpy, "run_path")("patch.py")`` a file load and
    ``getattr(json, "dumps")`` generic code.

    Narrow by construction. The attribute must be a plain string literal in
    ``getattr``'s second argument -- the fold joins ``"run_" "module"`` before this
    runs -- and an alias must be bound by a bare ``name = getattr(...)``. An
    attribute assembled at run time from values the payload does not carry is a
    residual, noted rather than claimed covered.
    """
    handed: list[str] = []
    aliases: set[str] = set()
    for name, start, end in spans:
        opener = _GETATTR_CALL_RE.match(view, name)
        if opener is None or opener.end() != start:
            continue
        arguments = brackets.get(start, [])
        if len(arguments) < 2:
            continue
        left, right = arguments[1]
        literal = _ATTRIBUTE_NAME_LITERAL_RE.fullmatch(view[left:right].strip())
        if literal is None or literal.group(2) not in wanted:
            continue
        # ``getattr(...)(...)``: the second group is not NAME-prefixed, so `_call_spans`
        # never registers it as a call. Its arguments are read from the bracket index,
        # which indexes every bracket rather than only a call's own.
        cursor = end + 1
        while cursor < len(view) and view[cursor].isspace():
            cursor += 1
        if cursor < len(view) and view[cursor] == "(":
            handed.extend(view[a:b] for a, b in brackets.get(cursor + 1, []))
        binding = _ALIAS_BINDING_RE.search(view[:name])
        if binding is not None:
            aliases.add(binding.group(1))
    for name, start, end in spans if aliases else ():
        called = _CALL_NAME_RE.match(view, name)
        if called is not None and called.group(1) in aliases:
            handed.append(view[start:end])
    return tuple(handed)


def _decoder_call_names(view: str) -> set[int]:
    """Locate decoder calls and explicit from-base64 aliases in this payload.

    Import names are collected once in token order, never from quoted text.
    Only bare calls use imported aliases; unrelated object's methods do not.
    This is static import spelling, not runtime binding or scope evaluation.
    """
    items = [
        (item, start)
        for item, start, _ in _lex(view)
        if item.type not in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING)
    ]
    aliases: set[str] = set()
    calls: set[int] = set()
    i = 0
    while i < len(items):
        item, start = items[i]
        if (
            item.type == token.NAME
            and item.string == "from"
            and [entry[0].string for entry in items[i + 1 : i + 3]] == ["base64", "import"]
        ):
            i += 3
            if i < len(items) and items[i][0].string == "(":
                i += 1
            while i < len(items) and items[i][0].type == token.NAME:
                original = items[i][0].string
                alias = original
                i += 1
                if i + 1 < len(items) and items[i][0].string == "as":
                    alias = items[i + 1][0].string
                    i += 2
                if _B64_DECODE_CALL_RE.fullmatch(original + "("):
                    aliases.add(alias)
                if i >= len(items) or items[i][0].string != ",":
                    break
                i += 1
            continue
        if item.type == token.NAME and i + 1 < len(items) and items[i + 1][0].string == "(":
            if _B64_DECODE_CALL_RE.fullmatch(item.string + "(") or (
                item.string in aliases and (i == 0 or items[i - 1][0].string != ".")
            ):
                calls.add(start)
        i += 1
    return calls


def _argument_spans(view: str) -> dict[int, list[tuple[int, int]]]:
    """Index comma-separated arguments once, without copying nested expressions.

    All brackets participate so commas in lists, dictionaries, nested calls,
    strings and comments cannot split their containing decoder's arguments.
    """
    arguments: dict[int, list[tuple[int, int]]] = {}
    stack: list[tuple[int, int | None]] = []
    for item, start, end in _lex(view):
        if item.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE):
            continue
        if item.type == token.OP and item.string in (")", "]", "}") and stack:
            opening, left = stack.pop()
            if left is not None:
                arguments[opening].append((left, start))
            continue
        if item.type == token.OP and item.string == "," and stack:
            opening, left = stack[-1]
            if left is not None:
                arguments[opening].append((left, start))
            stack[-1] = (opening, None)
            continue
        if stack and stack[-1][1] is None:
            stack[-1] = (stack[-1][0], start)
        if item.type == token.OP and item.string in ("(", "[", "{"):
            stack.append((end, None))
            arguments[end] = []
    for opening, left in stack:
        if left is not None:
            arguments[opening].append((left, len(view)))
    return arguments


def _decoder_input_spans(view: str) -> list[tuple[int, int, int, int]]:
    """``(name start, argument end, value start, value end)`` per decoder call, inner first.

    Which argument carries the bytes is one decision, and both the decode and the
    ownership check below need it, so it is made in one place. A keyword ``s=``
    wins wherever it sits, else the first positional -- the stdlib signature --
    and an unrecognised keyword resolves to nothing rather than to a guess.
    Indices, not slices: a nested call's argument text is never copied.
    """
    arguments = _argument_spans(view)
    decoder_names = _decoder_call_names(view)
    spans: list[tuple[int, int, int, int]] = []
    for name, start, end in reversed(_call_spans(view)):
        if name not in decoder_names:
            continue
        value_start = start
        value_end = end
        for index, (left, right) in enumerate(arguments.get(start, [])):
            keyword = _B64_INPUT_KEYWORD_RE.match(view, left, right)
            if keyword is not None and keyword.group(1) == "s":
                value_start, value_end = keyword.end(), right
                break
            if index == 0 and keyword is None:
                value_start, value_end = left, right
                break
        spans.append((name, end, value_start, value_end))
    return spans


def _decode_call_literal_sources(view: str) -> tuple[tuple[str, str], ...]:
    """``(source literal, decoded text)`` for each decoder call whose input resolves.

    Each decoder occurrence is evaluated once. Nested decoding shrinks the bytes;
    no loop repeatedly decodes arbitrary data until it happens to name a mint.
    Nonliteral arguments are left unresolved, never executed.

    The SOURCE is the base64 that appears in *view*, lower-cased so a caller
    reading the floor's lower-cased view can match it. For a nested chain it is
    the OUTERMOST literal, inherited from the resolved child: the intermediate
    bytes exist only inside this function, so tagging a decoding with them would
    leave it unattributable to the text it came from.
    """
    view = _fold_inline_literals(view)
    resolved: dict[int, tuple[int, str, str]] = {}
    decoded: list[tuple[str, str]] = []
    for name, end, value_start, value_end in _decoder_input_spans(view):
        literal = _INLINE_B64_LITERAL_RE.fullmatch(view, value_start, value_end)
        if literal is not None:
            value = literal.group(2)
            source = value.lower()
        else:
            # Only a directly nested decoder is resolved; no arbitrary expression runs.
            child_start = value_start
            while child_start < value_end and view[child_start].isspace():
                child_start += 1
            qualifier = _B64_MODULE_QUALIFIER_RE.match(view, child_start, value_end)
            if qualifier is not None:
                child_start = qualifier.end()
            child = resolved.get(child_start)
            if child is None or view[child[0] : value_end].strip():
                continue
            value, source = child[1], child[2]
        try:
            text = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            ).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeError):
            continue
        resolved[name] = (end + 1, text, source)
        decoded.append((source, text.lower()))
    return tuple(decoded)


def _decode_call_literals(view: str) -> tuple[str, ...]:
    """The decodings of *view*'s own decoder calls, without their source literals."""
    return tuple(decoded for _, decoded in _decode_call_literal_sources(view))


def _names_an_inline_interpreter(words: list[str]) -> bool:
    """True if *words* read as a command that could run an inline PROGRAM.

    The gate on splitting a text to expose a payload. Splitting removes one
    layer of quotes, which is exactly what uncovers a quoted carrier -- and, on
    a text that is already Python, would remove PYTHON's own quotes and promote
    printed text to code: ``print("b64decode('…')")`` split once reads as a
    real decoder call, fabricating a decoding no program performs. A command
    that can run an inline payload names an interpreter or a shell in one of its
    words (the pipe producer's own program is neither, so the whole command is
    what is asked, not its first word); printed Python names nothing.
    """
    for word in words:
        base = _program_basename(word)
        if base in _NESTED_SHELL_PROGRAMS or _PYTHON_PROGRAM_RE.match(base):
            return True
    return False


def _program_text_candidates(raw_text: str) -> list[str]:
    """Every text the shell could hand an interpreter as a PROGRAM, over-yielded.

    Three levels, because a payload's Python quotes survive exactly as many
    shell splits as the spelling wrapped it in: the whole command reads as
    Python for an UNQUOTED carrier (a heredoc body), its shell WORDS expose a
    quoted carrier with its base64 case intact (a ``-c`` operand, a here-string
    word, a pipe producer's operand), and one further split exposes a payload
    nested inside another shell command, where an inner ``python -c`` payload
    sits inside the outer shell's own quoted operand. The raw text alone reaches
    only the unquoted carrier: a quoted one holds its decoder call inside a single
    string token, where the tokenizer cannot see it.

    Over-yielding is safe here and a bounded cost. Safe because the caller
    attributes a decoding only to the payload that carries its own source
    literal, so a neighbouring command's decoding cannot travel to this one --
    the whole reason the source is tracked. Bounded because the levels are fixed
    and each level is one pass over the text it splits.
    """
    candidates = [raw_text]
    frontier = [raw_text]
    for _ in range(_SHELL_QUOTE_LEVELS):
        deeper: list[str] = []
        for text in frontier:
            words = _shell_tokens(text)
            if not _names_an_inline_interpreter(words):
                continue
            for word in words:
                if word == text:
                    continue  # nothing left to unwrap
                candidates.append(word)
                deeper.append(word)
        frontier = deeper
    return candidates


def _decoded_b64_literal_sources(raw_text: str) -> tuple[tuple[str, str], ...]:
    """``(source literal, decoded text)`` for the decoder calls *raw_text* carries.

    Read from the command AS SUBMITTED because base64 is case-sensitive and does
    not survive the floor's lower-cased view. Only an argument a decoder call is
    actually handed is decoded -- an encoded literal a program merely carries as
    data stays data -- and each ``(source, decoded)`` pair is reported once, so a
    spelling that reaches the same payload at two levels does not double it.
    """
    seen: set[tuple[str, str]] = set()
    sources: list[tuple[str, str]] = []
    for text in _program_text_candidates(raw_text):
        # A decoder call needs a quoted base64 argument somewhere to resolve at all,
        # so this keeps the tokenizer off every ordinary command word.
        if not _INLINE_B64_LITERAL_RE.search(text):
            continue
        for pair in _decode_call_literal_sources(text):
            if pair not in seen:
                seen.add(pair)
                sources.append(pair)
    return tuple(sources)


def _decoded_b64_literals(raw_text: str) -> tuple[str, ...]:
    """The decodings *raw_text* carries, without their source literals."""
    return tuple(decoded for _, decoded in _decoded_b64_literal_sources(raw_text))


def _inline_payload_reaches_cli(
    payload: str, decoded_literals: tuple[tuple[str, str], ...] = ()
) -> bool:
    """True if this payload names the credential mint, plainly or once decoded.

    *decoded_literals* is the whole command's decode pool
    (:func:`_decoded_b64_literal_sources`), because only the raw text still has
    the case base64 needs -- so each entry arrives with the source literal it
    was decoded from, and an entry counts for THIS payload only when the payload
    carries that literal. Unscoped, the pool is shared: a decoder call in one
    command lends its decoding to every other payload in the frame, so
    ``python -c 'print(b64decode("aGVsbG8="))'`` beside an unrelated ``printf``
    of encoded text reads as a mint.

    Matched as a substring rather than as a quoted literal because the floor's
    tokens have had their shell quotes removed: a heredoc body arrives as
    ``exec(b64decode(s=aw1…))``, with the Python quotes already gone. Carrying
    the bytes AND running a decoder over them is the reach; a payload that only
    carries them is data (the pool never decoded them), and a payload with no
    decoder call at all borrows nothing.
    """
    view = _fold_inline_literals(payload)
    if _names_the_mint(view):
        return True
    # Do not attach another command's decoding to a payload with no decoder call.
    if not _decoder_call_names(view):
        return False
    carried = view.lower()
    for source, decoded in decoded_literals:
        if source not in carried:
            continue
        if _SELF_NAME_RE.search(decoded) and _MINT_VERB_RE.search(decoded):
            return True
        if _names_the_mint(decoded):
            return True
    return False


def _names_the_mint(view: str) -> bool:
    """Check each disjoint loader region without recursive descendant rescans.

    Recursive all-loader rescans grow exponentially with nesting. Keeping only
    outer intervals bounds copied/scanned argument text by the source length.
    The scoped import anchor retains the quote-peeling distinction: a statement
    at a string's start is not necessarily a match at the whole view's start.
    """
    if _MINT_SURFACE_RE.search(view):
        return True
    if _PRODUCT_IMPORT_RE.search(view) and _MINT_VERB_RE.search(view):
        return True
    spans = _call_spans(view)
    # Only a payload that reaches for `getattr` needs the bracket index, so an
    # ordinary one pays nothing for the indirection questions below.
    brackets = _argument_spans(view) if "getattr" in view else {}
    runners = _call_arguments(view, _DYNAMIC_RUNNER_RE, spans)
    loaders = _call_arguments(view, _INLINE_CODE_LOADER_RE, spans)
    if brackets:
        runners += _indirect_call_arguments(view, _DYNAMIC_RUNNER_NAMES, spans, brackets)
        loaders += _indirect_call_arguments(view, _INLINE_CODE_LOADER_NAMES, spans, brackets)
    if any(_PACKAGE_LITERAL_RE.search(arg) for arg in runners):
        return True
    for raw_arg in loaders:
        arg = _LOADER_ARG_DELIMITERS_RE.sub("", raw_arg)
        if _CONSOLE_SCRIPT_LITERAL_RE.search(arg) or _MINT_SURFACE_RE.search(arg):
            return True
        if (
            _PRODUCT_IMPORT_RE.search(arg) or _LOADED_IMPORT_RE.search(raw_arg)
        ) and _MINT_VERB_RE.search(arg):
            return True
        if _dynamic_runner_handed_the_package(arg):
            return True
    return False
