import atexit
import os
import time

from pathlib import Path
from threading import Thread
from typing import Callable, Optional

from pictrl.cloudflared import start_tunnel
from pictrl.utils import ProcessGroup, get_config, delete_folder, find_free_port, per_os

def autoupdate(name: str, pgroup: ProcessGroup, cwd: Optional[str] = None, on_restart: Optional[Callable] = None):
    local_hash = pgroup.get_stdout(pgroup.run("git rev-parse HEAD", cwd=cwd))
    def check_for_update():
        nonlocal pgroup, local_hash
        while pgroup.running:
            try:
                pgroup.out(f"Checking for update [{name}]")
                pgroup.run(f"git fetch origin", cwd=cwd, timeout=120)
                pgroup.out(f"Checking for update [{name}] 1")
                remote_hash = pgroup.get_stdout(pgroup.run("git rev-parse refs/remotes/origin/HEAD", cwd=cwd, timeout=120))
                pgroup.out(f"[{name}] {local_hash=}")
                pgroup.out(f"[{name}] {remote_hash=}")
                if local_hash != remote_hash:
                    pgroup.out(f"Stopping & restarting [{name}]")
                    pgroup.kill()
                    on_restart()
                    break
            except Exception as e:
                pgroup.out(e)
            time.sleep(60)
        print(f"check_for_update [{name}] thread stopped")
    
    Thread(target=check_for_update, daemon=True).start()

def clone(config, pgroup: ProcessGroup):
    source_dir = config["source_dir"]
    if Path(source_dir).exists():
        delete_folder(source_dir)
    pgroup.run(f"git clone {config["git"]} {source_dir}", stream=True)

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
    main_group = ProcessGroup()
    active_pgroup_ref = [None]
    def kill_active_pgroup():
        nonlocal active_pgroup_ref
        active_pgroup = active_pgroup_ref[0]
        if active_pgroup:
            active_pgroup.kill()
    autoupdate("pictrl", main_group, on_restart=kill_active_pgroup)

    while main_group.running:
        config = get_config()
        pgroup = ProcessGroup()
        active_pgroup_ref[0] = pgroup
        try:
            clone(config, pgroup)
            if config["type"] == "python":
                run_python(config, pgroup)
            else:
                print(f"Unsupported config type {config['type']}")
                break
            autoupdate("source", pgroup, cwd=config["source_dir"])
            pgroup.wait()
        except Exception as e:
            print(e)
            time.sleep(30)
        finally:
            pgroup.kill()

if __name__ == "__main__":
    main()