import atexit
import json
import os
import shutil
import stat
import socket
import subprocess
import time

from collections import deque
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen
from threading import Thread
from typing import Callable, Deque, Dict, List, Literal, Optional, Tuple

import psutil

@dataclass
class LogLine:
    id: int
    time: float
    name: str
    text: str

DEFAULT_CONFIG = {
    "env": {},
    "source_dir": str(Path("./source").absolute()),
}
def get_config():
    with open("./config/config.json", "r") as f:
        return DEFAULT_CONFIG | json.load(f)

def fully_kill_process(process: Optional[Popen]):
    if process is None:
        return
    try:
        for child_process in psutil.Process(process.pid).children(True):
            try:
                child_process.kill()
            except psutil.NoSuchProcess:
                pass
        process.kill()
    except psutil.NoSuchProcess:
        pass

class ProcessGroup:
    def __init__(self, limit: Optional[int] = 100000):
        self.__limit = limit
        self.__running = True
        self.__stdout: Deque[LogLine] = deque()
        self.__stderr: Deque[LogLine] = deque()
        self.__output: Deque[LogLine] = deque()
        self.__processes: List[Popen[str]] = []
        self.__process_id_counter = 0

    def restart(self):
        self.kill()
        self.__running = True
        self.__processes.clear()

    def run(self, name: str, command: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, stream: bool = False):
        id, process = self.__start_process(name, command, cwd=cwd, env=env, stream=stream)
        rc = process.wait()
        while not getattr(process, "__stdout_finished", False) or not getattr(process, "__stderr_finished", False):
            time.sleep(1)
        if rc != 0:
            raise subprocess.CalledProcessError(rc, command)
        process.stdout.close()
        process.stderr.close()
        process.kill()
        return id
    
    def run_async(self, name: str, command: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, block: bool = False):
        return self.__start_process(name, command, cwd=cwd, env=env, block=block)[0]
    
    def __start_process(self, name: str, command: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, stream: bool = True, block: bool = False):
        if len(self.__processes) >= 20:
            i = 0
            while i < len(self.__processes) and len(self.__processes) > 5:
                if self.__processes[i].poll() is not None:
                    self.__processes.pop(i)
                else:
                    i += 1

        self.__process_id_counter += 1
        process = Popen(command, shell=True, bufsize=1, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, cwd=cwd, env=env)
        setattr(process, "__block", block)
        self.out(name, f"> {command}")
        Thread(target=self.capture_output, args=(name, process, self.__process_id_counter, "stdout", stream), daemon=True).start()
        Thread(target=self.capture_output, args=(name, process, self.__process_id_counter, "stderr", stream), daemon=True).start()
        self.__processes.append(process)
        atexit.register(lambda: fully_kill_process(process))
        return self.__process_id_counter, process

    def capture_output(self, name: str, process: Popen, id: int, capture: Literal["stdout", "stderr"] = "stdout", stream: bool = True):
        process_out = process.stdout if capture == "stdout" else process.stderr
        
        logs = [self.__output, (self.__stdout if capture == "stdout" else self.__stderr)]
        def add_to_logs(*lines: str):
            nonlocal self, id, capture, logs
            for log in logs:
                log.extend(LogLine(id, time.time(), name, line) for line in lines)
                while self.__limit and len(log) > self.__limit:
                    log.popleft()
        
        if stream:
            for line in process_out:
                if not self.__running:
                    break
                print(line, end="")
                add_to_logs(line)
        else:
            lines = list(process_out)
            print("".join(lines), end="")
            add_to_logs(*lines)

        fully_kill_process(process)
        setattr(process, f"__{capture}_finished", True)
    
    def out(self, name: str, message: str):
        message = message.rstrip() + "\n"
        print(message, end="")
        self.__stdout.append(LogLine(0, time.time(), name, message))
        self.__output.append(LogLine(0, time.time(), name, message))

    def __gather_output(self, output: Deque[LogLine], id: Optional[int] = None):
        return "".join(line.text for line in output if id is None or line.id == id).strip()

    def get_stdout(self, id: Optional[int] = None):
        return self.__gather_output(self.__stdout, id)
    
    def get_stderr(self, id: Optional[int] = None):
        return self.__gather_output(self.__stderr, id)
    
    def get_output(self, id: Optional[int] = None):
        return self.__gather_output(self.__output, id)

    @property
    def output(self):
        return self.__output

    @property
    def running(self):
        return self.__running

    def kill(self):
        self.__running = False
        for process in self.__processes:
            fully_kill_process(process)
        self.__processes.clear()

    def wait(self):
        for process in self.__processes:
            if getattr(process, "__block", False):
                process.wait()
        for process in self.__processes:
            fully_kill_process(process)

def delete_folder(path: str):
    def onerror(func, path, exc_info):
        if not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IWUSR)
            func(path)
        else:
            raise
    shutil.rmtree(path, onexc=onerror)

def find_free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])

def per_os(win: str, unix: str) -> str:
    return win if os.name == "nt" else unix

def autoupdate(name: str, pgroup: ProcessGroup, cwd: Optional[str] = None, on_restart: Optional[Callable] = None):
    pgroup_name = f"{name}.autoupdate"
    local_hash = pgroup.get_stdout(pgroup.run(pgroup_name, "git rev-parse HEAD", cwd=cwd))
    def check_for_update():
        nonlocal pgroup, local_hash
        while pgroup.running:
            try:
                pgroup.out(pgroup_name, f"Checking for update [{name}]")
                pgroup.run(pgroup_name, f"git fetch origin", cwd=cwd)
                remote_hash = pgroup.get_stdout(pgroup.run(pgroup_name, "git rev-parse refs/remotes/origin/HEAD", cwd=cwd))
                pgroup.out(pgroup_name, f"[{name}] {local_hash=}")
                pgroup.out(pgroup_name, f"[{name}] {remote_hash=}")
                if local_hash != remote_hash:
                    pgroup.out(pgroup_name, f"Stopping & restarting [{name}]")
                    pgroup.kill()
                    if on_restart:
                        on_restart()
                    break
            except Exception as e:
                pgroup.out(pgroup_name, str(e))
            time.sleep(120)
        pgroup.out(pgroup_name, f"check_for_update [{name}] thread stopped")
    
    Thread(target=check_for_update, daemon=True).start()

def check_internet_restart(pgroup: ProcessGroup):
    def check():
        nonlocal pgroup
        while pgroup.running:
            time.sleep(300)
            try:
                pgroup.run("pictrl.internet", f"ping -w {per_os('5000', '5')} google.com", stream=True)
            except subprocess.CalledProcessError:
                pgroup.out("pictrl.internet", "No internet connection. Restarting...")
                pgroup.run("pictrl.internet", per_os("shutdown /r", "sudo shutdown -r now"), stream=True)
    Thread(target=check, daemon=True).start()
