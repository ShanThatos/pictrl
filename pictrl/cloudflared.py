import json

from pathlib import Path

from pictrl.utils import ProcessGroup

def start_tunnel(tunnel_url: str, local_port: int, pgroup: ProcessGroup, creds_path: str) -> ProcessGroup:
    tunnel_check = json.loads(pgroup.get_stdout(pgroup.run(f"cloudflared tunnel list --output json --name {tunnel_url}")))
    if not tunnel_check:
        print(f"Tunnel {repr(tunnel_url)} does not exist. Creating...")
        pgroup.run(f"cloudflared tunnel create {tunnel_url}")
    
    pgroup.run(f"cloudflared tunnel route dns --overwrite-dns {tunnel_url} {tunnel_url}")

    creds_path = Path(creds_path).absolute()
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    if creds_path.exists():
        creds_path.chmod(0o777)
        creds_path.unlink()
    pgroup.run(f"cloudflared tunnel token --cred-file {str(creds_path)} {tunnel_url}")
    
    pgroup.run_async(f"cloudflared tunnel run --cred-file {str(creds_path)} --url localhost:{local_port} {tunnel_url}")
