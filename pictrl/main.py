import atexit
import json
import os
import time

from pathlib import Path
from threading import Thread

from pictrl.cloudflared import start_tunnel
from pictrl.utils import ProcessGroup, get_config, delete_folder, find_free_port, per_os

def clone(config, pgroup: ProcessGroup):
    source_dir = config["source_dir"]
    if Path(source_dir).exists():
        delete_folder(source_dir)
    pgroup.run(f"git clone {config["git"]} {source_dir}", stream=True)
    
    local_hash = pgroup.get_stdout(pgroup.run("git rev-parse HEAD", cwd=config["source_dir"]))
    version_key = f"{json.dumps(config, sort_keys=True)}-{local_hash}"
    def autoupdate():
        nonlocal pgroup, version_key
        while pgroup.running:
            pgroup.run("git fetch origin", cwd=config["source_dir"])
            remote_hash = pgroup.get_stdout(pgroup.run("git rev-parse refs/remotes/origin/HEAD", cwd=config["source_dir"]))
            check_version_key = f"{json.dumps(get_config(), sort_keys=True)}-{remote_hash}"
            if version_key != check_version_key:
                pgroup.out("Stopping & restarting")
                pgroup.out(version_key)
                pgroup.out(check_version_key)
                pgroup.kill()
                break
            time.sleep(config["autoupdate"])
    
    Thread(target=autoupdate, daemon=True).start()

def run_python(config, pgroup: ProcessGroup):
    source_dir = config["source_dir"]
    venv_dir = str(Path(source_dir).joinpath(".venv"))
    bin_dir = str(Path(venv_dir).joinpath(per_os("Scripts", "bin")))
    python_path = str(Path(bin_dir).joinpath("python"))

    print("Setting up virtual environment")
    pgroup.run(f"python -m venv {venv_dir}", stream=True)
    pgroup.run(f"{python_path} -m pip install --upgrade pip", stream=True)
    for req in ["requirements.txt", "req.txt"]:
        req_path = str(Path(source_dir).joinpath(req))
        if os.path.exists(req_path):
            pgroup.run(f"{python_path} -m pip install -r {req_path}", stream=True)

    env = os.environ.copy() | {"PYTHONUNBUFFERED": "1"} | config["env"]
    path_sep = per_os(";", ":")
    env["PATH"] = f"{bin_dir}{path_sep}{config["env"].get('PATH', '')}{path_sep}{os.environ.get('PATH', '')}"
    
    port = find_free_port()
    env["PORT"] = str(port)

    print("Running main command")
    pgroup.run_async(config["command"], env=env, cwd=source_dir, block=True)
    if "tunnel" in config and config["tunnel"]:
        print("Starting tunnel")
        start_tunnel(config["tunnel"], port, pgroup)
    atexit.register(pgroup.kill)

def main():
    while True:
        config = get_config()
        pgroup = ProcessGroup()
        try:
            clone(config, pgroup)
            if config["type"] == "python":
                run_python(config, pgroup)
                pgroup.wait()
            else:
                print(f"Unsupported config type {config['type']}")
                break
        except Exception as e:
            print(e)
            time.sleep(30)
        finally:
            pgroup.kill()

if __name__ == "__main__":
    main()