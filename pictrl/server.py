import datetime
import os
import time

from subprocess import check_output
from threading import Thread
from typing import List, Optional

from flask import Flask, redirect, render_template, session, request, Blueprint

from pictrl.cloudflared import start_tunnel
from pictrl.utils import LogLine, ProcessGroup, get_config

PGROUPS_REF: List[Optional[ProcessGroup]] = []

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

    print(start_time, end_time, filters)

    def check_filter(line: LogLine):
        if not (start_time <= line.time <= end_time):
            return False
        return any(f"{line.name}.".startswith(f"{filter}.") for filter in filters)

    output = PGROUPS_REF[0].output.copy()
    if PGROUPS_REF[1]:
        output += PGROUPS_REF[1].output
    
    filtered_output = sorted((line.time, line.name, line.text) for line in output if check_filter(line))
    full_output = "\n".join(f"{format_epoch_time(time)} [{name}] {text.rstrip()}" for time, name, text in filtered_output)

    return full_output

@router.post("/login")
def login():
    if request.form.get("key") == pictrl_server_config.get("key", ""):
        session["is_admin"] = True
    return redirect("/")

@router.route("/logout")
def logout():
    session.pop("is_admin")
    return redirect("/")

def run_pictrl_server(pgroups: List[Optional[ProcessGroup]]):
    global PGROUPS_REF

    if "secret" not in pictrl_server_config:
        pgroups[0].out("pictrl.server", "No secret key found in config, not starting log server")
    
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

    if "tunnel" in pictrl_server_config:
        start_tunnel("pictrl.server", pictrl_server_config["tunnel"], 80, pgroups[0], "./config/pictrl-tunnel-creds.json")
