import dataclasses
import datetime
import json
import os
import time

from pathlib import Path
from subprocess import check_output
from threading import Thread
from typing import Iterable, List

from flask import Flask, redirect, render_template, session, request, Blueprint

from pictrl.cloudflared import start_tunnel
from pictrl.utils import LogLine, ProcessGroup, get_config, per_os

PGROUPS_REF: List[ProcessGroup] = []
LOG_START_TIME = int(time.time())
LOG_FILE_NAME = f"pictrl_{LOG_START_TIME}.json"

pictrl_server_config = get_config().get("pictrl_server", {})

router = Blueprint("pictrl", __name__)

def format_epoch_time(epoch_time: float):
    return datetime.datetime.fromtimestamp(epoch_time).strftime("%Y-%m-%d %H:%M:%S")

@router.get("/")
def index():
    if not session.get("is_admin", False):
        return render_template("login.html")
    return render_template("index.html")

if os.name != "nt":
    @router.get("/info")
    def get_info():
        if not session.get("is_admin", False):
            return redirect("/")
        hostname = check_output("hostname", shell=True, text=True).strip()
        ip_address = check_output("hostname -I", shell=True, text=True).strip()
        return {
            "hostname": hostname,
            "ip_address": ip_address
        }

@router.get("/logs")
def logs():
    if not session.get("is_admin", False):
        return redirect("/")
    
    current_time = time.time()
    start_time = request.args.get("start", type=float, default=current_time - 24 * 60 * 60)
    end_time = request.args.get("end", type=float, default=current_time)
    filters = request.args.get("filters", type=str, default="").split(",")

    return "\n".join(f"{format_epoch_time(log.time)} [{log.name}] {log.text.rstrip()}" for log in get_logs(start_time, end_time, filters))

@router.post("/login")
def login():
    if request.form.get("key") == pictrl_server_config.get("key", ""):
        session["is_admin"] = True
    return redirect("/")

@router.route("/logout")
def logout():
    session.pop("is_admin")
    return redirect("/")

@router.get("/restart")
def restart():
    if not session.get("is_admin", False):
        return redirect("/")
    active_pgroup = PGROUPS_REF[1]
    if active_pgroup.running:
        PGROUPS_REF[0].out("pictrl.server", "Restarting...")
        save_logs()
        active_pgroup.kill()
        return "Restarted"
    return "No process to restart"

@router.get("/reboot")
def reboot():
    if not session.get("is_admin", False):
        return redirect("/")
    PGROUPS_REF[0].out("pictrl.server", "Rebooting...")
    save_logs()
    PGROUPS_REF[0].run("pictrl.server", per_os("shutdown /r", "sudo shutdown -r now"))
    return "Rebooting"

def get_logs(start_time: float, end_time: float, filters: List[str]):
    global PGROUPS_REF, LOG_START_TIME, LOG_FILE_NAME

    logs: List[LogLine] = []
    def filter_add_logs(new_logs: Iterable[LogLine]):
        for log in new_logs:
            if not (start_time <= log.time <= end_time):
                continue
            if not any(f"{log.name}.".startswith(f"{filter}.") for filter in filters):
                continue
            logs.append(log)

    if start_time < LOG_START_TIME:
        log_files = list(path for path in Path("./logs").glob("pictrl_*.json") if path.is_file() and path.name != LOG_FILE_NAME)
        log_file_times = sorted((float(file.stem.split("_")[1]), file) for file in log_files)
        for i, (log_start_time, file) in enumerate(log_file_times):
            log_end_time = log_file_times[i + 1][0] if i + 1 < len(log_file_times) else LOG_START_TIME
            if end_time < log_start_time or start_time > log_end_time:
                continue
            with file.open("r") as f:
                filter_add_logs(LogLine(**line) for line in json.load(f))
    
    filter_add_logs(PGROUPS_REF[0].output)
    filter_add_logs(PGROUPS_REF[1].output)

    return logs

def save_logs():
    global PGROUPS_REF, LOG_FILE_NAME
    log_path = Path(f"./logs/{LOG_FILE_NAME}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as f:
        output = sorted(PGROUPS_REF[0].output + PGROUPS_REF[1].output, key=lambda line: line.time)
        json.dump([dataclasses.asdict(line) for line in output], f, indent=2)

def run_pictrl_server(pgroups: List[ProcessGroup]):
    global PGROUPS_REF

    if "secret" not in pictrl_server_config:
        pgroups[0].out("pictrl.server", "No secret key found in config, not starting log server")
        return
    
    PGROUPS_REF = pgroups

    app = Flask(__name__)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.secret_key = pictrl_server_config["secret"]
    app.register_blueprint(router)

    kwargs = {
        "host": "0.0.0.0",
        "port": 80,
        "debug": False,
        "load_dotenv": False,
    }
    Thread(target=app.run, kwargs=kwargs, daemon=True).start()

    def save_logs_thread():
        while pgroups[0].running:
            save_logs()
            time.sleep(15)
    Thread(target=save_logs_thread, daemon=True).start()

    if "tunnel" in pictrl_server_config:
        start_tunnel("pictrl.tunnel", pictrl_server_config["tunnel"], 80, pgroups[0], "./config/pictrl-tunnel-creds.json")
