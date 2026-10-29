# 0007 — Media at home: devices at home talk through a LiveKit server at home

- **Status:** Proposed
- **Date:** 2026-10-29
- **Amends:** [ADR-0004](0004-hosting.md), decision 3: LiveKit Cloud carries the media of devices away from home only

## Context

- ADR-0004 runs the cloud on one machine at home, behind a Cloudflare Tunnel, and sends all media through LiveKit Cloud's free Build plan, so that nothing at home needs UDP. The plan includes 5,000 WebRTC minutes a month. A conversation uses two participant-minutes a minute, so that is about 80 minutes of conversation a day, or about 33 if the agent's session minutes count too (ADR-0004, decision 4).
- In phase one, one owner has two machines: a PC, which is the cloud's machine, and the robot's Raspberry Pi, on the same home network (2026-10-29). With LiveKit Cloud, the robot's audio and the agent's leave the home and come back, though both ends are in it.
- The cloud link names the media server of each conversation in `conversation.start` (ai-platform [ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md)). The cloud may therefore give different devices different servers, and change them by configuration (ADR-0004, decision 5).
- LiveKit's server is open source, with builds for Linux and Windows; ai-robot's tests run it on both. What it needs on a network:
  - signaling on port 7880, behind a load balancer or reverse proxy that terminates TLS;
  - media over UDP, either on one port with its mux (`rtc.udp_port`) or on a range;
  - ICE over TCP on port 7881 (`rtc.tcp_port`), for networks without UDP; it cannot sit behind TLS;
  - `use_external_ip: true` makes it look up a public address through STUN, and `node_ip` announces an address of one's choice when it is false;
  - `rtc.ips` limits it to some addresses, such as those of the home network rather than a Windows host's Hyper-V and WSL adapters.
- The cloud link requires TLS on every connection but those to the device itself (SEC-1, [the cloud link](https://github.com/trungnguyen278/ai-platform/blob/main/protocols/cloud/README.md#security-and-privacy)), and devices trust the public certificate authorities.
  - Let's Encrypt's DNS-01 challenge validates names whose servers are not exposed to the internet, and wildcards. It validates no IP address, so a certificate needs a name.
  - Caddy obtains such certificates through the caddy-dns/cloudflare module, with a Cloudflare API token limited to `Zone.Zone:Read` and `Zone.DNS:Edit`.
  - A tunnel with stable hostnames, as ADR-0004 has, needs a domain whose DNS is on Cloudflare; quick tunnels are for testing only. A name under that domain may resolve to an address on the home network.
- Windows keeps ranges of UDP ports for Hyper-V and WSL (`netsh int ipv4 show excludedportrange protocol=udp`), and a port in them cannot be bound (seen 2026-10-29 on the owner's PC).

## Decision

1. **Devices at home talk through a LiveKit server at home.** It runs beside the cloud, on its machine (ADR-0004), and serves the home network only: no port opens to the internet.
   - Media on one UDP port (`rtc.udp_port`), outside the ranges the host keeps, and ICE over TCP (`rtc.tcp_port`) for networks without UDP.
   - It announces the machine's address on the home network: `use_external_ip: false`, with `node_ip` where it runs in a container, and `rtc.ips` limited to the home network.
   - Signaling goes through the cloud's reverse proxy, Caddy, which terminates TLS.
2. **Devices at home reach the cloud on the home network** too, at a name under the owner's domain that resolves to the cloud machine's address there. The cloud tells devices at home by the address their control channel comes from, which is in the home network, a setting.
3. **Devices away reach the cloud through the tunnel, and talk through LiveKit Cloud,** as ADR-0004 decided.
4. **Certificates that devices already trust.** Caddy obtains Let's Encrypt certificates for the home's names by DNS-01, through Cloudflare, with a token limited to `Zone.Zone:Read` and `Zone.DNS:Edit` on that domain. Devices then need nothing of the home's own.
5. **Development without the domain** uses a local certificate authority (mkcert), whose root is installed on the development machines and the robot used for development. It never goes into a device's image.

## Alternatives considered

| Alternative | Why not |
|---|---|
| LiveKit Cloud for every device (ADR-0004 as it was) | Nothing to run at home, but every word leaves the home and comes back, and the free plan caps conversations at about 80 minutes a day, or 33. |
| A LiveKit server at home that devices away reach too | Its UDP would open the home to the internet, which carrier-grade NAT may not even allow (ADR-0004). |
| A rented LiveKit server with a public address, for every device | No cap, but audio leaves the home, and the server costs money, or may be reclaimed when idle, as Oracle Cloud's Always Free offer may be (ADR-0004, decision 5). |
| No TLS on the home network, as Home Assistant's Wyoming protocol has | Simpler, but it breaks SEC-1, which is ai-platform's to change, and anything on the network could read the audio or speak in it. |
| A certificate authority of the home's own, installed on every device | Devices would need it at set-up, a change to the set-up protocol, and the home would keep a CA's key safe. |

## Consequences

- Positive:
  - conversations at home have no monthly cap, and less delay;
  - audio stays in the home until the agent sends it to its model;
  - still no port opens to the internet, and devices need no change: they trust public authorities, and take the server the cloud names.
- Negative / accepted trade-offs:
  - the owner needs a domain on Cloudflare, which the tunnel needs anyway, and the cloud machine keeps a Cloudflare token;
  - one more service at home, and the reverse proxy, to run;
  - the cloud machine's address on the home network must not change: a reservation on the router;
  - on Windows, the UDP port must avoid the ranges Hyper-V keeps, and the firewall must allow both ports on the private network;
  - with only the PC as the cloud's machine, devices talk only while it runs, as with ADR-0004.
- Follow-up:
  - in the Compose file and the configuration: the LiveKit server, Caddy with caddy-dns/cloudflare, and the home network's setting in `devices`;
  - a development guide: mkcert on the PC, its root on the robot, the firewall and the UDP port on Windows;
  - once this is accepted, ADR-0004 says that it is amended.

## References

Retrieved 2026-10-29:

- LiveKit: [firewall configuration](https://docs.livekit.io/transport/self-hosting/ports-firewall/) (ports, and `rtc.udp_port` against a range); [deployment](https://docs.livekit.io/transport/self-hosting/deployment/) (TLS at the load balancer); [`config-sample.yaml`](https://github.com/livekit/livekit/blob/master/config-sample.yaml) (`use_external_ip`, `node_ip`, `rtc.ips`, `rtc.interfaces`).
- Let's Encrypt: [challenge types](https://letsencrypt.org/docs/challenge-types/), DNS-01.
- Caddy: [caddy-dns/cloudflare](https://github.com/caddy-dns/cloudflare) (the `dns cloudflare` directive, and the token's permissions).
- Cloudflare: [Tunnel, get started](https://developers.cloudflare.com/tunnel/get-started/) (quick tunnels for testing only; hostnames on a domain in the account).
- mkcert: [README](https://github.com/FiloSottile/mkcert).
