"""Which files a program writes, given its arguments (used by simrt.shell_effects).

`classify(prog, args)` returns
- READ: writes no file (output redirections are the caller's business);
- MENTIONED: may write the paths it mentions (`sed -i`, `sort -o`, `curl -o`, `cp`, ...);
- EVERYTHING: may write any file (an option that runs commands, or a program text not seen);
- "": not a program known here; the caller decides.

Short options are unpacked from clusters, so `sed -Ei`, `curl -sSLo FILE` and `sort -uo FILE` are
recognised. The value-taking options of each program come from its man page.
"""

import re
from collections.abc import Callable

READ, MENTIONED, EVERYTHING = "read", "mentioned", "everything"

# Cannot write a file whatever their arguments.
READ_ONLY = frozenset(
    "cat head tail more ls dir grep egrep fgrep ag ack wc stat file du df pwd echo printf which whereis "
    "type whoami id groups hostname uname date printenv ps pgrep pidof top htop free uptime vmstat iostat "
    "netstat ss ip ifconfig route arp lsof diff cmp comm md5sum sha1sum sha256sum sha512sum cksum cut tr "
    "jq od hexdump strings nl column realpath readlink basename dirname test [ [[ true false sleep hash set "
    "unset alias read wait jobs kill pkill killall exit return : telnet ping dig nslookup host fold fmt "
    "rev tac expand unexpand paste join seq base64 locate getent ldd nproc lscpu lsblk journalctl dmesg w "
    "who last man help shopt break continue shift getopts let env command".split()
)
# Short options that take a value, per program (so a value is never read as more options).
_LETTERS = {
    "sed": "efl",
    "sort": "kotST",
    "uniq": "fsw",
    "curl": "AbcCdDeEFHKmoPQrtTuUwxXyYz",
    "wget": "OoaPeiUtTwQBDlARIX",
    "tree": "LPIHTo",
    "less": "bhjkoOpPtTxyz#",
    "nc": "ecgGimoxpswdqIOTVXPM",
    "psql": "cdfvLoFPRThpU",
    "awk": "fvFiE",
    "yq": "oIp",
    "mv": "tS",
    "cp": "tS",
    "ln": "tS",
    "install": "tSmog",
}
_ALIASES = {"ncat": "nc", "netcat": "nc", "mysql": "psql", "mariadb": "psql", "gawk": "awk", "mawk": "awk"}
_ALIASES |= {"nawk": "awk", "sqlite": "sqlite3"}


def options(
    args: list[str], letters: str = "", long_values: tuple[str, ...] = ()
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """(options, operands). `-sSLo F` -> -s -S -L (-o, F) when "o" is in `letters`; `--out=F` -> (--out, F)."""
    opts: list[tuple[str, str | None]] = []
    operands: list[str] = []
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        i += 1
        if a == "--":
            operands += args[i:]
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if not eq and name in long_values and i < n:
                value, i, eq = args[i], i + 1, "="
            opts.append((name, value if eq else None))
            continue
        if len(a) < 2 or not a.startswith("-"):
            operands.append(a)
            continue
        for j, ch in enumerate(a[1:], 1):
            if ch in letters:  # the rest of the cluster, or the next word, is the value
                value = a[j + 1 :]
                if not value and i < n:
                    value, i = args[i], i + 1
                opts.append(("-" + ch, value))
                break
            opts.append(("-" + ch, None))
    return opts, operands


def written_words(prog: str, args: list[str]) -> list[str]:
    """Words that may name a file a MENTIONED program writes: operands and option values."""
    opts, operands = options(args, _LETTERS.get(_ALIASES.get(prog, prog), ""))
    return operands + [v for _, v in opts if v]


def copied_pair(prog: str, args: list[str]) -> tuple[str, str] | None:
    """(source, destination) of a plain `cp/mv/install SRC DEST`, else None."""
    if prog not in ("cp", "mv", "install"):
        return None
    opts, operands = options(args, _LETTERS[prog], ("--target-directory",))
    if len(operands) != 2 or {n for n, _ in opts} & {"-t", "--target-directory", "-d", "--directory"}:
        return None
    return operands[0], operands[1]


def deleted_words(prog: str, args: list[str]) -> list[str]:
    """Operands a command removes: rm/unlink/rmdir operands, the source of a plain `mv SRC DEST`."""
    opts, operands = options(args, _LETTERS.get(prog, ""), ("--target-directory",))
    if prog in ("rm", "unlink", "rmdir"):
        return operands
    if prog == "mv" and len(operands) == 2 and not {n for n, _ in opts} & {"-t", "--target-directory"}:
        return operands[:1]
    return []


def _names(args: list[str], prog: str, long_values: tuple[str, ...] = ()) -> set[str]:
    return {name for name, _ in options(args, _LETTERS[prog], long_values)[0]}


# ---- per-program checks ---------------------------------------------------------------------------
# sed commands that write a file (w, W, s///w) or run one (e, s///e)
_SED_EXEC = re.compile(r"(?:^|[^\w\s\\]|[\d$])[gpiImM\d]*\s*[wWe](?:\s|$|[;}])")
# awk statements that run a command or redirect output
_AWK_RUNS = re.compile(r"\bsystem\s*\(|\bprintf?\b[^;{}]*[>|]|\|\s*getline|\|&")
_SQL_WRITES = re.compile(
    r"\\(?:o|g|w|s|copy)\b|\bcopy\b.*\bto\b|\binto\s+(?:out|dump)file\b|(?:^|;)\s*tee\s", re.I | re.S
)
_CURL_WRITES = {"-o", "-O", "-D", "-c", "--output", "--remote-name", "--remote-name-all", "--output-dir"}
_CURL_WRITES |= {"--dump-header", "--cookie-jar", "--trace", "--trace-ascii", "--stderr", "--libcurl"}
_CURL_WRITES |= {"--etag-save", "--hsts", "--alt-svc"}
_REDIS_WRITES = {"config", "save", "bgsave", "bgrewriteaof", "debug", "module", "shutdown", "replicaof"}
_REDIS_WRITES |= {"slaveof", "--rdb", "--functions-rdb"}
_FIND_WRITES = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprint0", "-fprintf"}


def _sed(args: list[str]) -> str:
    opts, operands = options(args, _LETTERS["sed"], ("--expression", "--file", "--line-length"))
    names = {name for name, _ in opts}
    if names & {"-f", "--file"}:
        return EVERYTHING  # a sed script in a file is not read here
    scripts = [v or "" for name, v in opts if name in ("-e", "--expression")] or operands[:1]
    if any(_SED_EXEC.search(s) for s in scripts):
        return EVERYTHING
    return MENTIONED if names & {"-i", "--in-place"} else READ


def _awk(args: list[str]) -> str:
    opts, operands = options(args, _LETTERS["awk"], ("--file", "--assign", "--field-separator", "--include"))
    if {name for name, _ in opts} & {"-f", "--file", "-E", "--exec"}:
        return EVERYTHING  # an awk program in a file is not read here
    if any(name in ("-i", "--include") and v == "inplace" for name, v in opts):
        return MENTIONED  # gawk -i inplace rewrites its input files
    return EVERYTHING if operands and _AWK_RUNS.search(operands[0]) else READ


def _curl(args: list[str]) -> str:
    names = _names(args, "curl")
    if names & {"-J", "--remote-header-name"}:
        return EVERYTHING  # the server chooses the file name
    return MENTIONED if names & _CURL_WRITES else READ


def _wget(args: list[str]) -> str:
    opts, _ = options(args, _LETTERS["wget"], ("--output-document", "--output-file", "--append-output"))
    names = {name for name, _ in opts}
    docs = [v for name, v in opts if name in ("-O", "--output-document")]
    to_stdout = bool(docs) and all(v in ("-", "/dev/null") for v in docs)
    logs = names & {"-o", "-a", "--output-file", "--append-output"}
    return READ if ("--spider" in names or to_stdout) and not logs else MENTIONED


def _sql(args: list[str]) -> str:
    if any(re.search(r"\\!|(?:^|;)\s*system\s", a) for a in args):
        return EVERYTHING  # psql / mysql shell escape
    writes = _names(args, "psql") & {"-o", "-L", "--output", "--log-file", "--tee"}
    return MENTIONED if writes or any(_SQL_WRITES.search(a) for a in args) else READ


def _netcat(args: list[str]) -> str:
    names = _names(args, "nc")
    if names & {"-e", "-c", "--exec", "--sh-exec", "--lua-exec"}:
        return EVERYTHING  # connects a program to the socket
    return MENTIONED if names & {"-o", "-x", "--output", "--hex-dump", "--append-output"} else READ


def _xxd(args: list[str]) -> str:
    operands, skip = [], False  # xxd has no option clusters: `-ps`, `-c 16`, `-c16`
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-") and len(a) > 1:
            skip = a in ("-c", "-g", "-l", "-o", "-s", "-n", "-cols", "-groupsize", "-len", "-seek", "-name")
        else:
            operands.append(a)
    return MENTIONED if len(operands) >= 2 else READ  # `xxd IN OUT` writes OUT


def _yq(args: list[str]) -> str:
    names = _names(args, "yq")
    if names & {"-s", "--split-exp"}:
        return EVERYTHING  # one output file per document, named by an expression
    return MENTIONED if names & {"-i", "--inplace", "--in-place"} else READ


def _with(names: set[str], letters: str) -> Callable[[list[str]], str]:
    """A check for programs that write only with one of `names` (short ones may be clustered)."""
    return lambda args: MENTIONED if {n for n, _ in options(args, letters)[0]} & names else READ


_CHECKS: dict[str, Callable[[list[str]], str]] = {
    "sed": _sed,
    "awk": _awk,
    "curl": _curl,
    "wget": _wget,
    "psql": _sql,
    "nc": _netcat,
    "xxd": _xxd,
    "yq": _yq,
    "sort": _with({"-o", "--output"}, _LETTERS["sort"]),
    "uniq": lambda args: MENTIONED if len(options(args, _LETTERS["uniq"])[1]) >= 2 else READ,  # uniq IN OUT
    "tree": _with({"-o"}, _LETTERS["tree"]),
    "less": _with({"-o", "-O", "--log-file", "--LOG-FILE"}, _LETTERS["less"]),
    "history": _with({"-w", "-a"}, "d"),
    "info": _with({"-o", "--output", "--out"}, "o"),
    "sqlite3": lambda args: EVERYTHING
    if any(re.search(r"(?:^|[\s;])\.(?:shell|system)\b", a) for a in args)
    else MENTIONED,
    "redis-cli": lambda args: EVERYTHING if {a.lower() for a in args} & _REDIS_WRITES else READ,
    "rg": lambda args: EVERYTHING if any(a == "--pre" or a.startswith("--pre=") for a in args) else READ,
    "find": lambda args: EVERYTHING if _FIND_WRITES & set(args) else READ,
}


def classify(prog: str, args: list[str]) -> str:
    """READ / MENTIONED / EVERYTHING for programs known here, "" otherwise."""
    if prog in READ_ONLY:
        return READ
    check = _CHECKS.get(_ALIASES.get(prog, prog))
    return check(args) if check else ""
