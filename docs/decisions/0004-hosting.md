# 0004 — Hosting: one machine at home behind a tunnel, LiveKit Cloud for media

- **Status:** Accepted
- **Date:** 2026-10-28
- **Revised:** 2026-10-28: the cloud link 3.0 names the media's server when each conversation starts.

## Context

- Phase one serves one owner, for nothing ([Phases](../architecture.md#phases)). The cloud's processes (`api`, `agent`, `worker`) and stores (PostgreSQL, Redis) fit on one machine ([ADR-0001](0001-architecture.md)).
- With the cloud link in two channels (ai-platform [ADR-0006](https://github.com/trungnguyen278/ai-platform/blob/main/docs/decisions/0006-two-channels.md)), the cloud's own servers need only HTTPS: the control channel is a WebSocket. Media needs UDP, but only between devices, the agent and the LiveKit server.
- What the options require:

| Option | Facts |
|---|---|
| A self-hosted LiveKit server | UDP 50000–60000, or one UDP port with its mux; TCP 7881 for clients without UDP; TURN over TLS on 443. It needs an address that devices reach from outside. |
| A home connection | It may sit behind carrier-grade NAT, with no public address to open. Even with one, opening ports exposes the home. |
| A Cloudflare Tunnel | Free, outbound only, and it carries HTTPS and WebSockets: what Luni Cloud ran behind. |
| LiveKit Cloud, Build plan | $0, without a card: 5,000 WebRTC minutes a month, 50 GB of downstream data, 100 concurrent connections, 5 concurrent agent sessions, and 1,000 agent session minutes. The agents framework runs on our own machine and connects out to it. |

- The owner chose to start on LiveKit Cloud's free plan, and to move media when it runs short (2026-10-28). Conversations may be long: a wake word opens a conversation of many turns (ai-platform ADR-0006).

## Decision

1. **The cloud runs on one machine the owner has**, at home, under Docker Compose: `api`, `agent`, `worker`, PostgreSQL and Redis.
2. **The outside world reaches it through a Cloudflare Tunnel**, HTTPS and WebSockets only. No port is opened at home.
3. **Media goes through LiveKit Cloud, on the Build plan.** Devices and the agent connect out to it, so nothing at home needs UDP. The cloud names the LiveKit server when each conversation starts (the cloud link 3.0), so the media's host can change without touching a device.
4. **The allowance is metered.**
   - A conversation uses two participant-minutes a minute, the device's and the agent's. 5,000 a month is about 2,500 minutes of conversation, some 80 a day.
   - If the plan's 1,000 agent session minutes count our own agent too, which its page does not say, the ceiling is about 33 minutes a day.
   - `operations` counts both, and warns before either runs out.
5. **When the allowance runs short, media moves** to LiveKit self-hosted on a rented server with a public address. That server is either a small one near Vietnam, or Oracle Cloud's Always Free offer, which costs nothing but may reclaim an idle instance.
   - The move is a change of configuration, since the cloud names the server for each conversation.
   - Until then, a spent allowance pauses conversations until the month renews, while the control channel keeps working.
6. **Growth** follows the [growth table](../architecture.md#growth):
   - LiveKit Cloud's paid plans, or LiveKit self-hosted on rented servers with public addresses;
   - the cloud's processes moved to rented servers when the home machine no longer suffices.

   Neither changes code: they are configuration and deployment.

## Alternatives considered

| Alternative | Why not |
|---|---|
| LiveKit self-hosted at home | It needs UDP from outside: often impossible behind carrier-grade NAT, and it exposes the home's address. |
| LiveKit self-hosted on a rented server, now | It removes the ceiling for a few dollars a month, or for nothing on Oracle's offer at the risk of losing an idle instance. The owner chose to start on the free plan; this is the step when the ceiling bites (decision 5). |
| Audio over the control channel's WebSocket, as Luni Cloud and xiaozhi | No UDP and no ceiling. But TCP stalls on a lossy Wi-Fi, there is no path for the camera's video, and LiveKit Agents is lost. The owner prefers WebRTC. |
| A private network such as Tailscale, with the robot inside it | It works for one owner's devices, but every other owner's devices would have to join the owner's network. |
| Rooms that stay open all day, on LiveKit Cloud | About 86,400 participant-minutes a month for one robot: far past the free plan (ai-platform ADR-0006). |

## Consequences

- Positive:
  - phase one costs nothing, and opens no port at home;
  - the media's host and the cloud's host change by configuration;
  - the same images run at home and on rented servers.
- Negative / accepted trade-offs:
  - the cloud is up only while the home machine and its connection are: acceptable for its owner alone;
  - it depends on two free offers, LiveKit Cloud's and Cloudflare's, whose terms may change. Self-hosting either is the fallback, at a monthly cost;
  - conversation minutes have a monthly ceiling, about 33 to 80 minutes a day, which `operations` watches.
- Follow-up:
  - the Compose files and the tunnel's configuration, in `deploy/`;
  - backups of PostgreSQL to object storage;
  - measure the latency from the robot to LiveKit Cloud's nearest region.

## References

Retrieved 2026-10-28:

- LiveKit: [pricing](https://livekit.com/pricing) (Build plan), [ports and firewall](https://docs.livekit.io/home/self-hosting/ports-firewall/).
- Cloudflare: [WebSockets](https://developers.cloudflare.com/network/websockets/), and [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).
- [Luni Cloud's architecture](https://github.com/trungnguyen278/Luni_Cloud/blob/main/docs/architecture.md): Docker Compose behind a Cloudflare Tunnel.
