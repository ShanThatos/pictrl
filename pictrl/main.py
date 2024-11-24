import atexit
import os
import time

from pathlib import Path
from typing import List, Optional

from pictrl.cloudflared import start_tunnel
from pictrl.server import run_pictrl_server
from pictrl.utils import ProcessGroup, get_config, delete_folder, find_free_port, per_os, autoupdate, check_internet_restart

def run_python(config, pgroup: ProcessGroup):
    setup_pgroup_name = "source.setup"

    source_dir = config["source_dir"]
    if Path(source_dir).exists():
        delete_folder(source_dir)
    pgroup.run(setup_pgroup_name, f"git clone {config["git"]} {source_dir}", stream=True)

    venv_dir = str(Path(source_dir).joinpath(".venv"))
    bin_dir = str(Path(venv_dir).joinpath(per_os("Scripts", "bin")))
    python_path = str(Path(bin_dir).joinpath("python"))

    pgroup.run(setup_pgroup_name, f"python -m venv {venv_dir}", stream=True)
    pgroup.run(setup_pgroup_name, f"{python_path} -m pip install --upgrade pip", stream=True)
    for req in ["requirements.txt", "req.txt"]:
        req_path = str(Path(source_dir).joinpath(req))
        if os.path.exists(req_path):
            pgroup.run(setup_pgroup_name, f"{python_path} -m pip install -r {req_path}", stream=True)

    env = os.environ.copy() | {"PYTHONUNBUFFERED": "1"} | config["env"]
    path_sep = per_os(";", ":")
    env["PATH"] = f"{bin_dir}{path_sep}{config["env"].get('PATH', '')}{path_sep}{os.environ.get('PATH', '')}"
    
    port = find_free_port()
    env["PORT"] = str(port)

    pgroup.run_async("source.run", config["command"], env=env, cwd=source_dir, block=True)
    if "tunnel" in config and config["tunnel"]:
        start_tunnel("source.tunnel", config["tunnel"], port, pgroup, "./config/source-tunnel-creds.json")
    atexit.register(pgroup.kill)

def main():
    main_group = ProcessGroup()
    pgroups: List[Optional[ProcessGroup]] = [main_group, ProcessGroup()]
    run_pictrl_server(pgroups)

    def kill_active_pgroup():
        nonlocal pgroups
        if active_pgroup := pgroups[1]:
            active_pgroup.kill()
    autoupdate("pictrl", main_group, on_restart=kill_active_pgroup)
    check_internet_restart(main_group)

    while main_group.running:
        config = get_config()
        pgroups[1].reset()
        try:
            if config["type"] == "python":
                run_python(config, pgroups[1])
            else:
                main_group.out("pictrl.run", f"Unsupported config type {config['type']}")
                break
            autoupdate("source", pgroups[1], cwd=config["source_dir"])
            pgroups[1].wait()
        except Exception as e:
            main_group.out("pictrl.run", str(e))
            main_group.out("pictrl.run", "Trying again in 2 minutes...")
            pgroups[1].kill()
            time.sleep(120)

if __name__ == "__main__":
    main()