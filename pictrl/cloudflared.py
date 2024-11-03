import json

from pathlib import Path

from pictrl.utils import ProcessGroup

CREDS_PATH = Path("./config/tunnel-creds.json").absolute()

def start_tunnel(tunnel_url: str, local_port: int, pgroup: ProcessGroup) -> ProcessGroup:
    tunnel_check = json.loads(pgroup.get_stdout(pgroup.run(f"cloudflared tunnel list --output json --name {tunnel_url}")))
    if not tunnel_check:
        print(f"Tunnel {repr(tunnel_url)} does not exist. Creating...")
        pgroup.run(f"cloudflared tunnel create {tunnel_url}")
    
    pgroup.run(f"cloudflared tunnel route dns --overwrite-dns {tunnel_url} {tunnel_url}")

    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CREDS_PATH.exists():
        CREDS_PATH.chmod(0o777)
        CREDS_PATH.unlink()
    pgroup.run(f"cloudflared tunnel token --cred-file {str(CREDS_PATH)} {tunnel_url}")
    
    pgroup.run_async(f"cloudflared tunnel run --cred-file {str(CREDS_PATH)} --url localhost:{local_port} {tunnel_url}")
