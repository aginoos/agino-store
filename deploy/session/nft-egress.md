# Egress rules for interactive sessions (design 8.6)

These rules go into the infra role `store_sandbox` (`templates/agino-sandbox.nft.j2`, plus the
tap script for `tc`). They apply to **all** slots (automatic jobs and sessions), because the
slot a session gets is not known in advance. Nothing here is an `accept`: a drop in any table is
final, accepts for guests stay in `firewall_forward_rules` (host_vars/store.yml), unchanged.

Facts from the infra role: bridge `agsbx0`; slot N has tap `agsbx-tN`, MAC `52:54:00:5b:00:0N` and the
fixed DHCP lease `10.231.0.(10+N)` (dnsmasq `--dhcp-host`); guests get no IPv6 (sysctl) and the main
forward chain already drops IPv6 and guest-to-guest routing, and `store_sandbox_blocked_dst` (private
ranges, loopback, the servers' public IPs including this host's).

The syntax below was written without a running nftables: run `nft -c -f /etc/nftables.d/agino-sandbox.nft`
(check only) before loading, and run the tests at the end on the store host.

## 1. Anti-spoofing, bridge family (extends `table bridge agino_sandbox`)

Frames from a tap may carry only the slot's MAC and the slot's address (0.0.0.0 is allowed for DHCP
discover / ARP probes). Everything that is not IPv4 or ARP is dropped, which removes IPv6, router
advertisements, neighbour discovery, VLAN tags and odd ether types in one rule. `prerouting` at
priority -300 sees every frame that enters a bridge port before the bridge decides where it goes.

Template (one block per slot, `N` = 0 .. `store_sandbox_slots - 1`):

```
{% for n in range(store_sandbox_slots) %}
        iifname "agsbx-t{{ n }}" ether saddr != {{ store_sandbox_mac_prefix }}{{ n }} counter drop
        iifname "agsbx-t{{ n }}" ether type != { ip, arp } counter drop
        iifname "agsbx-t{{ n }}" ether type ip ip saddr != { 0.0.0.0, {{ store_sandbox_ip_base }}{{ store_sandbox_ip_first + n }} } counter drop
        iifname "agsbx-t{{ n }}" ether type arp arp saddr ip != { 0.0.0.0, {{ store_sandbox_ip_base }}{{ store_sandbox_ip_first + n }} } counter drop
        iifname "agsbx-t{{ n }}" ether type arp arp saddr ether != {{ store_sandbox_mac_prefix }}{{ n }} counter drop
{% endfor %}
```

Rendered for slot 0 and placed in the existing bridge table next to `chain forward`:

```
table bridge agino_sandbox
delete table bridge agino_sandbox
table bridge agino_sandbox {
    chain spoof {
        type filter hook prerouting priority -300; policy accept;
        iifname "agsbx-t0" ether saddr != 52:54:00:5b:00:00 counter drop
        iifname "agsbx-t0" ether type != { ip, arp } counter drop
        iifname "agsbx-t0" ether type ip ip saddr != { 0.0.0.0, 10.231.0.10 } counter drop
        iifname "agsbx-t0" ether type arp arp saddr ip != { 0.0.0.0, 10.231.0.10 } counter drop
        iifname "agsbx-t0" ether type arp arp saddr ether != 52:54:00:5b:00:00 counter drop
        # ... slots 1..3 the same with their MAC and address
    }
    chain forward {
        type filter hook forward priority filter; policy accept;
        iifname "agsbx-t*" oifname "agsbx-t*" drop
    }
}
```

The `counter` on each rule gives per-tap drop counters (`nft list table bridge agino_sandbox`).

## 2. Denied services, connection count and rate (new drop-only table)

Append to `agino-sandbox.nft.j2` (the file is loaded by the existing `nft -f`, each table is deleted
and recreated, so it stays idempotent). Priority `filter - 10` runs before the main forward chain.
Routed guest traffic enters through the bridge device, so the match is `iifname "agsbx0"`.

```
table inet agino_sandbox_egress
delete table inet agino_sandbox_egress
table inet agino_sandbox_egress {
    # per guest address; dynamic sets are filled by the rules below
    set sbx_ctcount { type ipv4_addr; size 16; flags dynamic; }
    set sbx_newrate { type ipv4_addr; size 16; flags dynamic; timeout 1m; }
    set sbx_ssh     { type ipv4_addr; size 16; flags dynamic; timeout 1m; }

    chain forward {
        type filter hook forward priority filter - 10; policy accept;
        iifname != "agsbx0" return

        # mail: SMTP, SMTPS, submission (also 2525)
        meta l4proto { tcp, udp } th dport { 25, 465, 587, 2525 } counter drop
        # Windows file sharing, NetBIOS, RPC endpoint mapper, telnet
        meta l4proto { tcp, udp } th dport { 135, 137, 138, 139, 445 } counter drop
        tcp dport 23 counter drop
        # outbound SSH is allowed but not as a scanner: at most 6 new connections per minute per guest
        tcp dport 22 ct state new add @sbx_ssh { ip saddr limit rate over 6/minute burst 6 packets } counter drop

        # at most 1000 tracked connections per guest, at most 50 new connections per second (burst 100)
        ct state new add @sbx_ctcount { ip saddr ct count over 1000 } counter drop
        ct state new add @sbx_newrate { ip saddr limit rate over 50/second burst 100 packets } counter drop
    }
}
```

## 3. Bandwidth, 20 Mbit/s per tap (`tc`, in `agino-sandbox-net.sh.j2`, inside the tap loop)

nftables can only drop, which shapes TCP badly; `tc` is the right tool. Idempotent (`replace`):

```
tc qdisc replace dev "$tap" root tbf rate 20mbit burst 64kb latency 100ms          # host -> guest
tc qdisc replace dev "$tap" handle ffff: ingress                                   # guest -> host
tc filter replace dev "$tap" parent ffff: protocol all prio 1 matchall \
    action police rate 20mbit burst 128k drop
```

## 4. Traffic per session

The daemon counts `rx_bytes + tx_bytes` of the slot's tap from `/sys/class/net/agsbx-tN/statistics/`
(world-readable, no privilege) and ends the session as `failed` ("network traffic limit reached") when the
difference since the start exceeds `[session] traffic_cap_gib` (2 GiB). The nft counters above are for
operators only.

## 5. Source address of the guests' traffic (known risk, D-203 decision 4)

Guest traffic leaves with the store host's public IP (masquerade). An abuse complaint can therefore
touch the production store service. Accepted by the user; mitigations are the rules above, the traffic
cap, the time limits and the audit trail (who, which app, when, source IP).

## 6. Tests to run on the store host after loading

1. `nft -c -f` passes; `nft list ruleset | grep -c agsbx` shows both new chains.
2. From a session VM: `nc -vz smtp.example.com 25` / `465` / `587` / `445` / `23` time out; `curl https://example.com` works.
3. Spoofing: inside a VM, `ip link set eth0 address 52:54:00:5b:00:0f` and `ip addr add 10.231.0.77/24 dev eth0`
   then ping out: dropped, and the bridge counters rise.
4. IPv6: `ip -6 addr add fd00::5/64 dev eth0; ping -6 fd00::1` and a forged router advertisement: dropped, no guest gets an IPv6 route.
5. Guest to guest: from slot 0 ping slot 1's address: dropped (bridge forward rule), also via ARP.
6. Host services: from a VM reach the host's bridge address 10.231.0.1 on 22, 80, 9100: only DHCP answers; the host's
   public IP on 22 and 443 is blocked (`store_sandbox_blocked_dst`).
7. Limits: `for i in $(seq 200); do curl -s -m5 https://example.com & done` hits the new-connection rate rule
   (counter rises), 20 Mbit/s: `curl -o /dev/null https://speed.example/100MB` stays near 2.5 MB/s in both directions.
