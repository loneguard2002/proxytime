# pod_security_audit.py
import os, json, subprocess

def audit_pod_security():
    report = {}
    
    # Check if running as root
    report["running_as_root"] = os.geteuid() == 0
    print(f"[{'+' if report['running_as_root'] else '-'}] Running as root: {report['running_as_root']}")
    
    # Check privileged mode
    try:
        with open("/proc/1/status") as f:
            for line in f:
                if "CapEff" in line:
                    caps = line.split(":")[1].strip()
                    report["capabilities"] = caps
                    print(f"[*] Effective capabilities: {caps}")
    except:
        pass
    
    # Check if privileged (all caps)
    report["privileged"] = report.get("capabilities", "") == "000001ffffffffff"
    print(f"[{'+' if report['privileged'] else '-'}] Privileged container: {report['privileged']}")
    
    # Check read-only root filesystem
    try:
        with open("/proc/mounts") as f:
            for line in f:
                if " / " in line and "ro" in line:
                    report["read_only_root"] = True
                    break
            else:
                report["read_only_root"] = False
    except:
        report["read_only_root"] = False
    print(f"[{'+' if report['read_only_root'] else '-'}] Read-only root filesystem: {report['read_only_root']}")
    
    # Check service account token mounted
    sa_token = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    report["sa_token_mounted"] = os.path.exists(sa_token)
    print(f"[{'+' if report['sa_token_mounted'] else '-'}] Service account token mounted: {report['sa_token_mounted']}")
    
    # Check if service account token is readable
    if report["sa_token_mounted"]:
        try:
            with open(sa_token) as f:
                token = f.read()
            report["sa_token_readable"] = True
            report["sa_token_preview"] = token[:50] + "..."
            print(f"[+] Service account token readable: {report['sa_token_preview']}")
        except:
            report["sa_token_readable"] = False
            print(f"[-] Service account token not readable")
    
    # Check for sensitive env vars
    sensitive_env = []
    for key, val in os.environ.items():
        if any(s in key.upper() for s in ["PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL"]):
            sensitive_env.append(key)
    report["sensitive_env_vars"] = sensitive_env
    print(f"[{'+' if sensitive_env else '-'}] Sensitive env vars: {sensitive_env}")
    
    # Check for mounted secrets
    report["mounted_secrets"] = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                if "secret" in line.lower() or "credential" in line.lower():
                    report["mounted_secrets"].append(line.strip())
    except:
        pass
    print(f"[{'+' if report['mounted_secrets'] else '-'}] Mounted secrets: {len(report['mounted_secrets'])}")
    
    # Check network interfaces
    report["network_interfaces"] = os.listdir("/sys/class/net/")
    print(f"[*] Network interfaces: {report['network_interfaces']}")
    
    # Check open ports
    result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True)
    report["listening_ports"] = result.stdout
    print(f"[*] Listening ports:\n{result.stdout}")
    
    # Check running processes
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    report["processes"] = result.stdout
    print(f"[*] Running processes:\n{result.stdout}")
    
    # Save report
    with open("/tmp/security_audit.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n[+] Full report saved to /tmp/security_audit.json")
    
    return report

audit_pod_security()
