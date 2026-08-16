"""Minimal text-VDF reader/writer.

Steam's controller templates repeat keys ("group" appears many times), so this keeps
nodes as ordered (key, value) pair lists instead of dicts.
"""

from __future__ import annotations

Pairs = list[tuple[str, "str | Pairs"]]

_BOM = "\ufeff"


class VdfError(ValueError):
    pass


class Conditional:
    """Carries a KeyValues platform conditional (``[$WIN32]``) alongside a value.

    The parse tree is plain ``str``/``list`` so callers can treat it as data, but a
    conditional has to survive a load/dump round trip or re-emitting a Steam template
    would silently drop a platform guard. These subclasses keep ``isinstance(value, str)``
    and ``isinstance(value, list)`` true for existing callers.
    """

    conditional: str | None = None


class VdfStr(str, Conditional):
    def __new__(cls, value: str, conditional: str | None = None) -> "VdfStr":
        obj = super().__new__(cls, value)
        obj.conditional = conditional
        return obj


class VdfPairs(list, Conditional):
    def __init__(self, value=(), conditional: str | None = None) -> None:
        super().__init__(value)
        self.conditional = conditional


def _with_conditional(value, conditional: str | None):
    if conditional is None:
        return value
    if isinstance(value, str):
        return VdfStr(value, conditional)
    return VdfPairs(value, conditional)


def _conditional_of(value) -> str | None:
    return getattr(value, "conditional", None)


_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "\\" and i + 1 < n:
            out.append(_ESCAPES.get(text[i + 1], text[i + 1]))
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _escape(text: str) -> str:
    """Escape on write so a value containing a quote or backslash round-trips.

    Without this, a Windows-style path or a name with a quote in it produced output this
    same parser could no longer read back."""
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _tokens(text: str) -> list[tuple[str, str]]:
    """Return (kind, text) tokens, where kind is 'brace', 'cond' or 'str'.

    Conditionals are kept distinct because a bare ``[$WIN32]`` run would otherwise be
    indistinguishable from a key, silently shifting every following pair by one.
    """
    out: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            newline = text.find("\n", i)
            # A trailing comment with no newline ends the file.
            i = n if newline == -1 else newline + 1
        elif char in "{}":
            out.append(("brace", char))
            i += 1
        elif char == "[":
            end = text.find("]", i)
            if end == -1:
                raise VdfError("unterminated conditional")
            out.append(("cond", text[i : end + 1]))
            i = end + 1
        elif char == '"':
            end = i + 1
            while end < n and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            if end >= n:
                raise VdfError("unterminated string")
            out.append(("str", _unescape(text[i + 1 : end])))
            i = end + 1
        else:
            end = i
            while end < n and text[end] not in ' \t\r\n{}"[':
                end += 1
            out.append(("str", text[i:end]))
            i = end
    return out


def loads(text: str) -> Pairs:
    tokens = _tokens(text.lstrip(_BOM))
    pos = 0

    def take_conditional() -> str | None:
        nonlocal pos
        if pos < len(tokens) and tokens[pos][0] == "cond":
            conditional = tokens[pos][1]
            pos += 1
            return conditional
        return None

    def parse_block() -> Pairs:
        nonlocal pos
        pairs: Pairs = []
        while pos < len(tokens):
            kind, token = tokens[pos]
            if kind == "cond":
                raise VdfError(f"conditional {token!r} where a key was expected")
            if token == "}" and kind == "brace":
                pos += 1
                return pairs
            if token == "{" and kind == "brace":
                raise VdfError("unexpected '{'")
            pos += 1
            if pos >= len(tokens):
                raise VdfError(f"key {token!r} without a value")
            next_kind, next_token = tokens[pos]
            if next_kind == "cond":
                raise VdfError(f"key {token!r} without a value")
            if next_kind == "brace" and next_token == "{":
                pos += 1
                value: "str | Pairs" = parse_block()
            elif next_kind == "brace":
                raise VdfError(f"key {token!r} without a value")
            else:
                value = next_token
                pos += 1
            pairs.append((token, _with_conditional(value, take_conditional())))
        return pairs

    root = parse_block()
    if pos != len(tokens):
        raise VdfError("trailing data after the root node")
    return root


def dumps(pairs: Pairs, indent: int = 0) -> str:
    pad = "\t" * indent
    out: list[str] = []
    for key, value in pairs:
        conditional = _conditional_of(value)
        suffix = f" {conditional}" if conditional else ""
        if isinstance(value, str):
            out.append(f'{pad}"{_escape(key)}"\t\t"{_escape(value)}"{suffix}')
        else:
            out.append(f'{pad}"{_escape(key)}"')
            out.append(f"{pad}{{")
            out.append(dumps(value, indent + 1))
            out.append(f"{pad}}}{suffix}")
    return "\n".join(part for part in out if part != "")


def get(pairs: Pairs, key: str) -> "str | Pairs | None":
    for candidate, value in pairs:
        if candidate == key:
            return value
    return None


def set_value(pairs: Pairs, key: str, value: "str | Pairs") -> None:
    for index, (candidate, _) in enumerate(pairs):
        if candidate == key:
            pairs[index] = (key, value)
            return
    pairs.append((key, value))
