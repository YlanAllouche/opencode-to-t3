"""Run an explicitly supplied OpenCode 2 binary in a new, private environment."""
import os
import re
from pathlib import Path
import subprocess

from .common import require
from .source import Source

TESTED_CONVERTER = "2.0.27"


def environment(root):
    root = Path(root).resolve()
    # Avoid inherited provider keys, shell hooks, OpenCode flags and XDG paths.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR") if key in os.environ}
    for name in ("DATA", "CONFIG", "CACHE", "STATE"):
        path = root / name.lower()
        path.mkdir(parents=True, exist_ok=True)
        env["XDG_" + name + "_HOME"] = str(path)
    home = root / "home"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), OPENCODE_TEST_HOME=str(home),
               OPENCODE_CONFIG_DIR=str(root / "config/opencode"),
               OPENCODE_DB=str(root / "data/opencode/opencode.db"),
               OPENCODE_DISABLE_AUTOUPDATE="true")
    return env


def convert(source, ids, root, binary):
    require(binary, "OpenCode 1.x requires --opencode2 /path/to/opencode (tested version " + TESTED_CONVERTER + ")")
    binary = Path(binary).expanduser().resolve()
    require(binary.is_file() and os.access(binary, os.X_OK), "OpenCode 2 binary is not executable: " + str(binary))
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    env = environment(root)
    version = subprocess.run([str(binary), "--version"], cwd=root, env=env, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=30)
    require(version.returncode == 0 and re.fullmatch(r"(?:opencode v?)?" + re.escape(TESTED_CONVERTER), version.stdout.strip()),
            "Unsupported converter version; expected " + TESTED_CONVERTER + ", got " + version.stdout.strip())
    database = Path(env["OPENCODE_DB"])
    source.isolate_v1(ids, database)
    with (root / "conversion-export.json").open("wb") as out, (root / "conversion.log").open("wb") as err:
        result = subprocess.run([str(binary), "session", "export", "--standalone", ids[0]], cwd=root,
                                env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=600)
    require(result.returncode == 0, "Official OpenCode conversion failed; inspect conversion.log in the private work directory")
    with Source(database) as converted:
        require(converted.format == "v2", "Official converter did not complete migration")
        require(set(ids) <= {s["id"] for s in converted.sessions()}, "Official converter omitted selected sessions")
        return {sid: converted.snapshot(sid) for sid in ids}
