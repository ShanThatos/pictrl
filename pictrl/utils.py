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
from pathlib import Path
from subprocess import Popen
from threading import Thread
from typing import Deque, Dict, List, Literal, Optional, Tuple

import psutil

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
    def __init__(self, limit: Optional[int] = None):
        self.__limit = limit
        self.__running = True
        self.__stdout: Deque[Tuple[int, str]] = deque()
        self.__stderr: Deque[Tuple[int, str]] = deque()
        self.__output: Deque[Tuple[int, str]] = deque()
        self.__processes: List[Popen[str]] = []
        self.__process_id_counter = 0

    def run(self, command: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, stream: bool = False, timeout: Optional[float] = None):
        id, process = self.__start_process(command, cwd=cwd, env=env, stream=stream)
        rc = process.wait(timeout=timeout)
        print(f"Process {command=} after wait with rc {rc}")
        for _ in range(12):
            if getattr(process, "__stdout_finished", False) and getattr(process, "__stderr_finished", False):
                break
            time.sleep(5)
        else:
            fully_kill_process(process)
            raise TimeoutError(f"Timed out stdout & stderr never finished: {command=}")
        if rc != 0:
            raise subprocess.CalledProcessError(rc, command)
        print("Closing stdout")
        process.stdout.close()
        print("Closing stderr")
        process.stderr.close()
        print("Killing")
        process.kill()
        return id
    
    def run_async(self, command: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, block: bool = False):
        return self.__start_process(command, cwd=cwd, env=env, block=block)[0]
    
    def __start_process(self, command: str, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, stream: bool = True, block: bool = False):
        if len(self.__processes) >= 20:
            i = 0
            while i < len(self.__processes) and i < 10:
                if self.__processes[i].poll() is not None:
                    self.__processes.pop(i)
                    break

        self.__process_id_counter += 1
        process = Popen(command, shell=True, bufsize=1, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, cwd=cwd, env=env)
        setattr(process, "__block", block)
        self.out(f"> {command}")
        Thread(target=self.capture_output, args=(command, process, self.__process_id_counter, "stdout", stream), daemon=True).start()
        Thread(target=self.capture_output, args=(command, process, self.__process_id_counter, "stderr", stream), daemon=True).start()
        self.__processes.append(process)
        atexit.register(lambda: fully_kill_process(process))
        return self.__process_id_counter, process

    def capture_output(self, command: str, process: Popen, id: int, capture: Literal["stdout", "stderr"] = "stdout", stream: bool = True):
        process_out = process.stdout if capture == "stdout" else process.stderr
        
        logs = [self.__output, (self.__stdout if capture == "stdout" else self.__stderr)]
        def add_to_logs(*lines: str):
            nonlocal self, id, capture, logs
            for log in logs:
                log.extend((id, line) for line in lines)
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
    
    def out(self, message: str):
        message = message.rstrip() + "\n"
        print(message, end="")
        self.__stdout.append((0, message))
        self.__output.append((0, message))

    def __gather_output(self, output: Deque[Tuple[int, str]], id: Optional[int] = None):
        return "".join(text for i, text in output if id is None or i == id).strip()

    def get_stdout(self, id: Optional[int] = None):
        return self.__gather_output(self.__stdout, id)
    
    def get_stderr(self, id: Optional[int] = None):
        return self.__gather_output(self.__stderr, id)
    
    def get_output(self, id: Optional[int] = None):
        return self.__gather_output(self.__output, id)

    @property
    def running(self):
        return self.__running

    def kill(self):
        self.__running = False
        for process in self.__processes:
            fully_kill_process(process)

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