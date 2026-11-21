"""Shell command-line tokenising for simrt.shell_effects: comments, heredocs, substitutions, words.

Not a full shell parser: enough structure (simple commands, redirections, variables, cwd) for a
conservative write analysis. Callers treat a None result (unparseable text) as "may write anything".
"""

import posixpath
import re
import shlex
from dataclasses import dataclass

_PUNCT = set("();<>|&")
_ARRAY_OPEN = re.compile(r"^[A-Za-z_]\w*\+?=$")  # `name=` / `name+=` just before `(`
_VAR = re.compile(r"\$\{([A-Za-z_]\w*)(?::?[-=?+]([^}]*))?\}|\$([A-Za-z_]\w*)|\$[0-9@*#?$!-]|\$\{[^}]*\}")
UNRESOLVED = "\0"


@dataclass
class SimpleCommand:
    words: list[str]
    outputs: list[str]  # output redirection targets
    inputs: list[str]  # input redirection sources
    heredoc: bool = False  # stdin is a heredoc (its body is in Prepared.heredocs)


@dataclass
class Prepared:
    main: str
    substitutions: list[str]  # bodies of $(...), `...`, <(...)
    heredocs: list[str]  # heredoc bodies


# ---- tokenising -----------------------------------------------------------------------------
def _matching_paren(text: str, start: int) -> int:
    """Index of the ')' closing the '(' just before `start`, or -1."""
    depth, quote, i = 1, None, start
    while i < len(text):
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"':
                i += 1
        elif c in "'\"":
            quote = c
        elif c in "()":
            depth += 1 if c == "(" else -1
            if depth == 0:
                return i
        i += 1
    return -1


def prepare(text: str) -> Prepared | None:
    """Drop comments, set heredoc bodies and command substitutions aside, turn newlines into ';'.

    Returns None when the text cannot be parsed (unbalanced quotes or parentheses).
    """
    out: list[str] = []
    prep = Prepared("", [], [])
    pending: list[str] = []  # heredoc delimiters waiting for the end of the line
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            quote = None if c == "'" else quote
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if text[i + 1] != "\n":  # backslash-newline is a line continuation
                out.append(text[i : i + 2])
            i += 2
            continue
        if text.startswith("$((", i):  # arithmetic: no side effects we track
            end = _matching_paren(text, i + 2)
            if end < 0:
                return None
            out.append("0")
            i = end + 2
            continue
        if text.startswith("$(", i) or (quote is None and text[i : i + 2] in ("<(", ">(")) or c == "`":
            end = text.find("`", i + 1) if c == "`" else _matching_paren(text, i + 2)
            if end < 0:
                return None
            prep.substitutions.append(text[i + 1 : end] if c == "`" else text[i + 2 : end])
            out.append("$CMDSUB")
            i = end + 1
            continue
        if quote == '"':
            quote = None if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        elif text.startswith("<<", i) and not text.startswith("<<<", i):
            m = re.match(r"<<-?\s*(['\"]?)([^\s'\";|&<>()]+)\1", text[i:])
            if m:
                pending.append(m.group(2))
                out.append(" << HEREDOC ")
                i += m.end()
                continue
        elif c == "\n":
            out.append(" ; ")
            i += 1
            for delim in pending:
                body: list[str] = []
                while i < n:
                    end = text.find("\n", i)
                    line = text[i : n if end < 0 else end]
                    i = n if end < 0 else end + 1
                    if line.strip() == delim:
                        break
                    body.append(line)
                prep.heredocs.append("\n".join(body))
            pending = []
            continue
        out.append(c)
        i += 1
    if quote is not None:
        return None
    prep.main = "".join(out)
    return prep


def simple_commands(text: str) -> list[SimpleCommand] | None:
    """Split a prepared command line into simple commands (None if it cannot be tokenised)."""
    text = re.sub(r"(?<=\s)\d+(?=[<>])", "", text)  # file descriptor numbers: `2>/dev/null`
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.commenters = ""
    lex.wordchars += "${}[],+:@%^!#~=\0"
    try:
        tokens = list(lex)
    except ValueError:
        return None
    cmds: list[SimpleCommand] = []
    cur = SimpleCommand([], [], [])
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if _ARRAY_OPEN.match(t) and i + 1 < len(tokens) and tokens[i + 1].startswith("("):  # `a=( x y )`
            cur.words.append(t + "$__simrt_array")  # elements are data, not commands; the value is unknown
            end = _array_end(tokens, i + 1)
            if end is None:
                return None
            i = end
            if any(ch in tokens[i - 1] for ch in ";|&"):  # `a=(x);` also ends the command
                cmds.append(cur)
                cur = SimpleCommand([], [], [])
            continue
        if not set(t) <= _PUNCT:
            cur.words.append(t)
            i += 1
            continue
        if t == "()" and len(cur.words) == 1:  # function definition: `name() { ... }`
            cur.words.clear()
        elif any(ch in t for ch in ";|()") or t in ("&", "&&"):
            cmds.append(cur)
            cur = SimpleCommand([], [], [])
        if ("<" in t or ">" in t) and i + 1 < len(tokens) and not set(tokens[i + 1]) <= _PUNCT:
            target = tokens[i + 1]
            i += 1
            if t.startswith("<<"):  # heredoc (prepare() left `<< HEREDOC`) or `<<<` here-string
                cur.heredoc = cur.heredoc or t == "<<"
            elif ">" in t and not (t.endswith("&") and (target.isdigit() or target == "-")):
                cur.outputs.append(target)
            elif "<" in t:
                cur.inputs.append(target)
        i += 1
    cmds.append(cur)
    return [c for c in cmds if c.words or c.outputs or c.inputs]


def _array_end(tokens: list[str], start: int) -> int | None:
    """Index just after the `)` closing the array whose `(` token is at `start` (None if unclosed)."""
    depth = 0
    for i in range(start, len(tokens)):
        if set(tokens[i]) <= _PUNCT:  # count operator tokens only
            depth += tokens[i].count("(") - tokens[i].count(")")
            if depth <= 0:
                return i + 1
    return None


# ---- words and paths ------------------------------------------------------------------------
def expand(word: str, variables: dict[str, str]) -> str:
    """Substitute known variables; unknown ones become UNRESOLVED."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(3)
        if name and name in variables:
            return variables[name]
        if m.group(1) and m.group(2) is not None:
            return m.group(2)  # ${NAME:-default}
        return UNRESOLVED

    return _VAR.sub(sub, word)


def resolve(word: str, cwd: str | None, home: str = "/root") -> str | None:
    """Absolute normalised path for a word, or None if it is relative and cwd is unknown."""
    if word.startswith("~"):
        word = home + word[1:]
    if not word.startswith("/"):
        if cwd is None:
            return None
        word = posixpath.join(cwd, word)
    path = posixpath.normpath(word)
    return "/" + path.lstrip("/")


def option_value(word: str) -> str | None:
    """The path-like part of an argument: the word itself, or the value of `--opt=value`."""
    if word.startswith("-"):
        return word.split("=", 1)[1] if "=" in word else None
    return word
