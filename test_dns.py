import socket, subprocess, os

# Check if it's DNS, HTTP, or everything
def diagnose():
    # 1. Can we resolve DNS?
    try:
        ip = socket.gethostbyone("example.com")
        print(f"[+] DNS works: {ip}")
    except Exception as e:
        print(f"[-] DNS failed: {e}")

    # 2. Can we connect to an IP directly (bypassing DNS)?
    try:
        s = socket.create_connection(("1.1.1.1", 443), timeout=3)
        s.close()
        print("[+] Direct IP:443 works")
    except Exception as e:
        print(f"[-] Direct IP failed: {e}")

    # 3. Check what's reachable on the network
    for port in [80, 443, 53, 8080, 22]:
        try:
            s = socket.create_connection(("10.0.0.1", port), timeout=2)
            s.close()
            print(f"[+] Port {port} open on 10.0.0.1")
        except:
            pass

diagnose()
