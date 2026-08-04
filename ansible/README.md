# Servers initial setup with Ansible

```bash
ansible-playbook -i inventory/hosts playbook.yaml --ask-vault-pass
```

## Uptime Kuma WireGuard client

The `monitoring` host is configured as the `10.77.77.3/32` client of the pfSense
`tun_wg0` WireGuard tunnel. It routes `192.168.20.0/24` and Pi-hole
(`192.168.0.201/32`) through that tunnel, allowing Uptime Kuma to reach the
internal Kubernetes network and resolve `home.brennoflavio.com.br` privately.

On the first run, Ansible generates the client private key only on the monitoring
VPS and prints its public key. Add that public key in pfSense under **VPN →
WireGuard → Peers** with these settings:

- Tunnel: `tun_wg0`
- Allowed IPs: `10.77.77.3/32`
- Endpoint: dynamic

The peer and endpoint defaults are in
`roles/wireguard-kuma/defaults/main.yaml`. Update `wireguard_kuma_endpoint` if
the home WAN address changes. The private key and generated configuration stay
on the VPS with `0600` permissions and are not stored in the repository.

Uptime Kuma uses Pi-hole directly as its only container DNS resolver. This
resolves `home.brennoflavio.com.br` privately, but makes all hostname monitoring
dependent on the WireGuard tunnel, Pi-hole, and home network being available.

After adding the pfSense peer, verify that `wg show kuma-home` reports a recent
handshake and test DNS from the Uptime Kuma container:

```bash
docker exec uptime-kuma node -e \
  'require("dns").lookup("pihole.home.brennoflavio.com.br", console.log)'
docker exec uptime-kuma node -e \
  'require("dns").lookup("example.com", console.log)'
```

pfSense must permit `10.77.77.3/32` to access `192.168.20.0/24`. It must also
permit TCP and UDP port `53` from `10.77.77.3/32` to `192.168.0.201`, with that
allow rule above the existing LAN block. The LAN block continues to deny all
other access to `192.168.0.0/24`.
