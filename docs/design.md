# How Virgil Works

This document explains how Virgil works and why it was designed this way. It's written for anyone who wants to understand what's happening inside a Clock Box: someone deciding whether to trust Virgil with a deployment, someone debugging one, someone who wants to contribute, or someone like me who just wants to know how things work.

To install and use Virgil, see the [README](../README.md). For the background (why Dante's clock can't cross a wireless bridge, and why a Clock Box on each side of the link solves that) see [wireless-dante](https://github.com/BRCook/wireless-dante), particularly its "Required Reading" and "How It Works" sections. This document assumes you've read those, or already know what PTP, NTP and a PHC are.

## Contents

- [Terms](#terms)
- [Architecture](#architecture)
- [Timing](#timing)
- [Leader Election](#leader-election)
- [The Group](#the-group)
- [Watching PTP](#watching-ptp)
- [Dante Devices](#dante-devices)
- [Serving PTPv1](#serving-ptpv1)
- [Automatic Trim](#automatic-trim)
- [Alerts](#alerts)
- [The Web UI](#the-web-ui)
- [Graphs and Logging](#graphs-and-logging)
- [Setting the Time](#setting-the-time)
- [Networking](#networking)
- [Configuration](#configuration)
- [APIs](#apis)
- [Installation](#installation)
- [Testing](#testing)
- [Known Limitations](#known-limitations)

## Terms

| Term | Meaning |
|---|---|
| Clock Box | A small computer on one side of a wireless link, plugged into (or near) one or more Dante devices, which serves them their clock. |
| Virgil | The software on each Clock Box. A Clock Box running Virgil is often just called "a Virgil". |
| Group | The Virgils that work together. Each member holds every other member's certificate, which is how they recognise each other. |
| Leader | The one Virgil in the group whose clock runs free. Every other Virgil follows it, making its clock the reference for the whole deployment. |
| Follower | Every Virgil that isn't the leader. A follower steers its own clock to match the leader's over NTP. |
| PTP interface | The network port facing the Dante device. Virgil serves PTP from this port, so it needs hardware timestamping. |
| Management interface | The network port Virgil uses for everything else: talking to the rest of the group, NTP, the web UI and its API. It *can* be the same port as the PTP interface, though a separate one is better. |
| Local Dante devices | The Dante devices on a Virgil's PTP interface (often just one, such as a console). |

A Virgil doesn't know (nor care) where or how it is deployed on the network. There is no need to always have a specific Virgil tied to a specific Dante device, or switch, or anything else. Ultimately, every Virgil is *exactly the same* by design, and "Leader" and "follower" are roles that can move from one Virgil to another at any time.

## Architecture

### Processes and Privileges

Virgil is three programs:

- **`virgild`**, the daemon, which does almost all of the work: it finds the other Virgils, takes part in the leader election, configures the timing services, watches the Dante devices, and serves the web UI.
- **`virgilctl`**, the command-line tool, which can read `virgild`'s status and change its settings.
- **`virgil-root`**, a small helper for the few jobs that need root.

`virgild` runs as an unprivileged system user, `dante-virgil`. It needs exactly two capabilities, both granted by its systemd unit: `CAP_NET_RAW`, to watch PTP traffic and to serve PTPv1 (see [Watching PTP](#watching-ptp) and [Serving PTPv1](#serving-ptpv1)), and `CAP_NET_BIND_SERVICE`, to listen on ports below 1024 (80 and 443 for the web UI, 319 and 320 for PTP).

Anything that needs root — writing the timing services' configs, setting the hardware clock, editing network configuration — is done by `virgil-root`, run from one-shot systemd units. `virgild` asks for one of these jobs by writing a small request file and starting the matching unit. It's allowed to start those units (and only those units) by a polkit rule that lets the `dante-virgil` user start and stop units whose names begin with `dante-virgil-`. Before acting on a request, `virgil-root` checks it thoroughly: interface names must match a strict pattern, numbers must be in sensible ranges, and every file it reads is opened without following symlinks and with a size limit. A compromised `virgild` therefore can't use `virgil-root` to do anything beyond what Virgil normally does.

The actual timekeeping is done by three standard programs: **chrony** (NTP), **ptp4l** (PTP) and **phc2sys** (which copies the system clock onto the network card's hardware clock). `virgild` configures them, starts and stops them, and watches them — but it isn't in the timing path itself. If `virgild` stops or crashes, the timing services carry on normally. As Virgil was designed for use in live productions, this "fail-safe" behaviour is essential to prevent possibly catastrophic failure of the Dante system in the event of a Virgil crash. The one exception is PTPv1, which `virgild` serves itself — but only when nothing else on its side of the link can do it instead (see [Serving PTPv1](#serving-ptpv1)).

### Systemd Units

| Unit | Runs |
|---|---|
| `dante-virgil` | `virgild` itself. |
| `dante-virgil-prepare` | `virgil-root prepare`, which generates configs for chrony, ptp4l and phc2sys from `virgild`'s request, along with a plan for the steps below. |
| `dante-virgil-sync` | `virgil-root sync`, which (on a follower) steps the system clock to the leader's, then sets the PHC from the system clock, sets the PHC's rate to the trim (see [Automatic Trim](#automatic-trim)), and saves the time to the hardware clock. |
| `dante-virgil-chrony`, `dante-virgil-ptp4l`, `dante-virgil-phc2sys` | The timing services, with the generated configs. Debian's own `chrony.service` is masked, since only one chrony can run at a time. |
| `dante-virgil-trim` | `virgil-root trim`, which changes a leader's PHC rate without restarting anything. |
| `dante-virgil-settime` | `virgil-root settime`, which steps a leader's system clock and PHC together (see [Setting the Time](#setting-the-time)). |
| `dante-virgil-dhcpnames` | `virgil-root dhcpnames`, which tidies the network configuration (see [Networking](#networking)). |
| `dante-virgil-hwstamp` | `virgil-root hwstamp`, which turns hardware timestamping on when ptp4l won't be running to do it (see [Serving PTPv1 Instead of PTPv2](#serving-ptpv1-instead-of-ptpv2)). |
| `dante-virgil-netaudio` | netaudio's daemon, which Virgil uses to talk to the Dante devices (see [Dante Devices](#dante-devices)). It listens on loopback only. |

### Files

| Path | Holds |
|---|---|
| `/etc/dante-virgil/virgil.toml` | This Virgil's configuration (see [Configuration](#configuration)). |
| `/etc/dante-virgil/tls/` | This Virgil's identity certificate and key (see [TLS](#tls)), plus `web/` for an imported web certificate. |
| `/run/dante-virgil/` | Requests for `virgil-root`, the timing services' current state, and the control socket `virgilctl` uses to reach `virgild`. |
| `/run/dante-virgil-generated/` | The configs `virgil-root prepare` generates. |
| `/var/lib/dante-virgil/` | `events.jsonl` (the history of alerts and the group's events, which survives reboots), `recordings/` (logging sessions), `timestamping-probe.json` (each card's probe results) and `timestamping.json` (cards caught losing transmit timestamps); see [Transmit Timestamp Slots](#transmit-timestamp-slots). |
| `/opt/dante-virgil/venv` | The Python environment Virgil runs from. |

### Ports

| Port | Serves |
|---|---|
| 80 | A redirect to HTTPS on port 443. |
| 443 | The web UI and its API, using an imported web certificate if there is one. |
| 8740 | The same web UI and API, but always with the identity certificate. `virgilctl`, invites and joining use this port, since they check the identity certificate. |
| 8741 | The group API, which only accepts connections from other members (see [TLS](#tls)). |
| 8742 | netaudio's own web UI, passed through behind Virgil's login, and only on the management interface. |
| 4780 | netaudio's API, on loopback only. |
| 319, 320 | PTP. ptp4l and Virgil's PTPv1 grandmaster share these. |

## Timing

The timing setup is the same one the `wireless-dante` prototype proved in the field. Virgil's contribution is that either Clock Box can play either role, and that roles can change while everything keeps running.

### Leader

On the leader:

- The PHC (the clock on the network card) runs free, at a rate set by the trim (`phc_ctl <phc> -- freq <trim>`). This clock is the reference for the whole deployment.
- chrony uses the PHC as its reference clock (`refclock PHC`). The leader's system clock therefore follows its PHC, and chrony then serves the system time to the followers over NTP, on the management interface.
- ptp4l serves the PHC's time to Dante devices over PTP.
- phc2sys doesn't run at all, since the PHC is already the reference.

### Follower

On a follower:

- chrony follows the leader over NTP, with the following filtering: `minpoll 0 maxpoll 0 xleave maxdelayquant 0.25 minsamples 64 maxsamples 256`, with `corrtimeratio 20` and `maxslewrate 10`. In short, chrony asks the leader for the time once a second, and keeps only the quickest quarter of the replies (the ones least delayed by the radio and thus closest to the "true" time), averaging them over several minutes. `wireless-dante` explains each setting in detail. Chrony discards older measurements whenever they suggest the leader's rate has changed, down to `minsamples`, and each time it does, its estimate of the rate (and so the follower's clock) jumps. `minsamples` is a setting, `chrony_minsamples`: keeping more measurements means smaller, smoother corrections, at the cost of following real changes (such as the leader's crystal drifting) more slowly. `maxsamples` is raised to match when needed.
- phc2sys copies the system clock onto the PHC. Its servo is deliberately gentle (`-P 0.03 -I 0.0005` by default) so that the PHC follows the system clock's average rate and ignores any short-term wobble. With phc2sys's defaults, every small correction chrony makes will appear on the PHC within a second or two, and a Dante device will see its grandmaster's rate jump around. Even so, a Dante device behind a follower wanders by about a ppm over minutes, since chrony's estimate of the leader's rate wanders as it measures across a jittery link, and phc2sys passes some of that on. The servo's two values are settings (`phc2sys_kp` and `phc2sys_ki`): gentler values make the PHC's rate steadier, at the cost of a larger offset between the PHC and the system clock. phc2sys never steps the PHC by less than 1 ms (`-S 0.001 -F 0`), so restarting it, or a Clock Box becoming a follower, can't make the Dante devices' grandmaster jump.
- ptp4l serves the PHC's time to the Dante device over PTP.

Every follower follows the leader directly, no matter how many followers there are.

### ptp4l Never Changes

ptp4l's configuration is identical on the leader and the followers:

| Setting | Why |
|---|---|
| `serverOnly 1` | A Clock Box only ever serves PTP. It must never follow anything it hears on the PTP interface. |
| UDPv4 transport | As Dante uses. |
| `logSyncInterval -3` | Eight Sync messages a second, as the AES67 media profile specifies (see below). |
| `logAnnounceInterval 1` | An Announce message every two seconds. |
| `ptp_minor_version 0` | Identify as PTPv2.0, rather than PTPv2.1 (see below). |
| `hybrid_e2e 1` | A device that sends its Delay Requests by unicast gets its Delay Responses by unicast too. Multicast requests are still answered by multicast. |
| `dscp_event 56`, `dscp_general 56` | Mark PTP as CS7, as Dante marks its own PTP, so that switches with QoS treat Virgil's PTP the same way. |

With linuxptp's defaults (one Sync message a second, identifying as PTPv2.1), the test Dante cards wouldn't lock to ptp4l at all. Both settings were changed at the same time in the prototype, so it isn't known which of the two was the problem.

Because the configuration doesn't depend on the role, ptp4l never needs restarting when the role changes: it simply keeps serving whatever is in the PHC. The Dante device therefore hears the same grandmaster, with the same identity, before, during and after a change of leader. Updating Virgil doesn't restart ptp4l either. If an update changes ptp4l's configuration, the change takes effect the next time the timing services are set up from scratch (at the next boot, for example).

### Changing Roles

When a follower becomes the leader, Virgil stops phc2sys and restarts chrony as a leader. The PHC keeps the rate phc2sys last gave it — the old leader's rate, as closely as the follower-becoming-leader Virgil could measure it — so the clocks don't jump. The new leader then adopts that rate as its own trim, so that the natural rate + trim of the new leader matches the rate of the old leader. This allows Virgils to swap roles seamlessly, with only minimal disturbance to the clock — certainly within 5 ppm, which is a trivial difference and can be quickly corrected by auto-trim.

When the leader becomes a follower, Virgil restarts chrony as a follower and then starts phc2sys. chrony starts following the new leader, and phc2sys starts steering the PHC towards the system clock, gently. The steering must be gentle to not disturb any connected Dante devices, but it *can* be gentle because ultimately the clock hasn't changed meaningfully. The new leader will have adopted the clock rate of the old leader to within a few ppm, which will not drift significantly in the few seconds it takes to swap roles.

Neither change touches ptp4l (see above).

### Setting Up From Scratch

A *setup from scratch* stops the timing services, sets the clocks again, and starts the services afresh. Virgil does one whenever the timing services aren't already running when it needs them, or when a change means they can't carry on as they are:

- **after any reboot**, since nothing is running yet;
- **when `virgild` starts and finds the timing services stopped** (stopped by hand, or crashed);
- **when timing is started again after "Stop timing"**;
- **when the PTP interface, or its NIC profile, changes**, since a different card has a different PHC;
- **on each follower, after "Sync time"** (see [Setting the Time](#setting-the-time)).

Restarting `virgild` on its own (after an update, a crash, or saving interfaces in the web UI) is *not* a setup from scratch, as long as the timing services are still running. `virgild` reads how they were set up (from a record in `/run/dante-virgil/`, which only lasts until the next boot) and carries on from there, so the Dante devices notice nothing. Changing roles, the trim and other settings is applied to the running services too.

A setup from scratch stops everything, then (on a follower) steps the system clock to the leader's time with a one-off `chronyd -q`, sets the PHC from the system clock, and starts the services again. A connected Dante device sees one re-lock while this happens, since its grandmaster disappears and comes back. Before the services start again, Virgil may also probe the PTP interface's transmit timestamp slots (see [Transmit Timestamp Slots](#transmit-timestamp-slots)). Every setup ends by saving the system time to the machine's hardware clock (`hwclock --systohc`). Clock Boxes are designed to run without internet access, so on the next boot, that saved time is the best starting point they have.

### Software Timestamping

A Virgil can use a PTP interface without hardware timestamping, but it's not a good idea. Such an interface has no PHC, so ptp4l serves the system clock directly (`-S`), and phc2sys doesn't run. This is only meant for testing. Software timestamps are taken whenever the kernel gets around to handling a packet, so they wobble by far more than a Dante device's clock can tolerate in live use. Will it work? Probably. However, any sort of load on the system that affects when the processor has time to stamp packets will cause significant jitter, which will not be pretty. For as long as a Virgil's PTP interface only has software timestamping, a warning will be displayed and distributed via heartbeat.

### Hardware Timestamps for NTP

chrony's NTP measurements across the link are more precise if the management interface timestamps them in hardware (`hwtimestamp <interface> rxfilter all`). Virgil turns this on when three things are true: the `ntp_hwtimestamp` setting is on (as it is by default), the management interface can timestamp in hardware, and the management interface is on a different network card from the PTP interface. It isn't enough for them to be different ports: two ports can share one hardware clock, and chrony must never share a network card's hardware timestamps with ptp4l. Some cards (such as the Realtek RTL8126) can only hold one pending timestamp at a time, and two programs asking for timestamps will fight over it. Virgil applies the rule to every card, rather than keeping a list of which cards are safe. `virgil-root` checks the rule again before writing it into chrony's configuration.

Ultimately, software timestamping for chrony is *good enough*, since the averaging we apply takes care of any jitter from processor scheduling.

## Leader Election

Exactly one Virgil in the group must be the leader. An election decides which.

### The Coin Toss

Each Virgil draws a random 64-bit number (the "coin toss") when it starts, and draws a new one whenever it becomes leader. It includes this value — its "coin" — in every heartbeat (see [Heartbeats](#heartbeats)), so every Virgil knows every other Virgil's coin. When an election is needed, the highest coin wins. Every Virgil makes the same comparison of the same coins, so they all reach the same result, and there's no need for a separate vote.

A follower that loses the leader doesn't act immediately. It first announces in its heartbeat that it has lost the leader, and waits a few seconds, so that every other follower that lost the leader has time to announce it too. Only then does the follower with the highest coin take over. A leader that can still be heard but has stopped leading (because it was stopped from the web UI, for example) counts as lost straight away.

### Rules

1. **The incumbent stays leader.** A Virgil that starts up and finds a leader already running follows it. This is what keeps a reboot or an update from disturbing the rest of the group.
2. **If the leader is lost,** the followers that lost it (and can still see each other) choose a new leader by coin toss (see [The Coin Toss](#the-coin-toss)).
3. **If two or more leaders see each other,** only one may stay leader. This happens when a broken link comes back: while the link was down, each side lost sight of its leader, so each side ended up with a leader of its own. To choose between them, Virgil asks whether each leader was *established* (whether it had been part of the group, hearing other members, before it started leading). An established leader always beats one that isn't. Between two established leaders, the one that wins the coin toss stays leader. Every other leader becomes a follower.
4. **If no Virgil is leading at startup,** the Virgils that can see each other choose a leader by coin toss.
5. **A Virgil can be made leader on request** (see [Become Leader](#become-leader)).

No Virgil is better suited to lead than any other, so a coin toss decides. It might seem better to choose by something meaningful, such as whose clock is most accurate, but comparing clocks is unreliable at exactly the moments an election happens: after a cold start, or after a long outage, when nobody's clock has been checked against anyone else's.

The exception is a newcomer that boots while it can't hear the group (perhaps because its network interface isn't up yet). After the listening timeout, it leads alone as if there are no other Virgils. When it finally meets the group's leader, rule 3 means it always steps down, whatever the coin toss says. After a radio outage, by contrast, both leaders had been part of the group before the link broke, so they're equals (and their clocks should still be mostly aligned), and the coin toss decides. The same applies after a cold start in which the radio link comes up after the Virgils have booted and elected themselves leaders.

### States

| State | Meaning | Leaves for |
|---|---|---|
| `LISTEN` | Looking for the other members of the group. | `SYNC`, if a leader is already running or another listener wins the coin toss; `LEADER`, if this Virgil wins, or hears nobody before the listening timeout. |
| `SYNC` | Setting up as a follower of the leader. | `FOLLOWER`, once set up; `LISTEN`, if the leader disappears meanwhile. |
| `FOLLOWER` | Following the leader. | `LEADER`, under rule 2 or 5. |
| `LEADER` | Leading. | `FOLLOWER`, under rule 3 or 5. |
| `STOPPED` | Stopped from the web UI or `virgilctl`, with the timing services off. | `LISTEN`, when started again. |

### Timings

| Timing | Value | Meaning |
|---|---|---|
| Visible | 3 s | A member counts as visible if this Virgil has heard its heartbeat within this time. |
| Listen settle | 3 s | How long a Virgil listens (or announces that it has lost the leader) before the coin decides. |
| Listen timeout | 10 s | How long a Virgil listens for a leader at startup before leading alone. |
| Leader loss | 30 s | How long a follower goes without hearing its leader before treating it as lost. In the meantime, chrony holds its last measured frequency, so the follower's clock carries on at the leader's rate, and its Dante devices notice nothing. A shorter timeout would make every brief radio dropout start an election, and the extra leader would have to step down again when the link returned. |
| Network wait | 60 s | How long the election waits for the management interface to get an address. A Virgil without an address can't hear anyone, and would otherwise wrongly conclude it was alone. |

### Become Leader

"Become leader" (in the web UI) and `virgilctl role lead` move the lead to a chosen Virgil on purpose. `virgilctl` passes the request to the local `virgild` over a control socket that only root and the `dante-virgil` user can open. `virgild` then asks the current leader to hand over, through the group API.

The chosen Virgil then becomes the leader, the old leader becomes a follower, and every other follower starts following the new leader. Due to the fundamental responsibility of Virgil being to keep the group's clocks in sync, a change in leader doesn't require much clock adjustment amongst followers. Dante devices shouldn't notice.

## The Group

### Finding Each Other

Virgils aren't configured with each other's addresses, as DHCP would wreak havoc, and setting static IPs or reserved leases isn't always practical. Virgil therefore makes such network wizardry unnecessary. Instead, each Virgil advertises itself over mDNS (the same multicast DNS that Dante devices use to find each other) on its management interface. It uses the service type `_dante-virgil._tcp`, an instance name of `Virgil on <name>`, and a host name of `virgil-<Virgil ID>.local`. Using a unique, Virgil-only host name avoids a clash with the machine's own host name, which other software might publish on other interfaces. Each advertisement carries the Virgil's ID, its version, its ports, and its group's ID.

A Virgil only pays attention to other Virgils that advertise the same group ID. That way, several separate groups can share one network and politely ignore each other. A Virgil that isn't in a group yet shows *every* Virgil it can see, and necessarily so — it could not accept a group invitation otherwise.

### Joining

Joining a group uses a one-time invite token:

1. On any member, `virgilctl group invite` (or "Make an invite" in the web UI) creates a token. The token contains the member's addresses, its certificate's fingerprint, and a random secret. The secret works once and expires after an hour.
2. On the new Virgil, `virgilctl group join <token>` (or "Join group" in the web UI) connects to the member over TLS. Before sending anything, it checks that the member's certificate matches the fingerprint in the token, so the token can't be used to trick the new Virgil into joining an impostor. Then it sends the secret, along with its own certificate.
3. The member checks the secret, adds the new Virgil to its member list, and replies with the member list and every member's certificate. It then tells every other member about the newcomer straight away (see [Syncing Group Members](#syncing-group-members)).

The web UI shows a new invite's token until the invite is used or expires, then closes it. Status lists the group's outstanding invites, and an invitation drops off that list when it's used. A join is refused if a different Virgil with the same Virgil ID is already a member (as happens when a Clock Box's disk is cloned to make another). The first Virgil of a group creates it with `virgilctl group create`.

The choice to use tokens rather than a password was deliberate: Virgil, when deployed, is a *critical piece of production infrastructure*. Under no circumstances should any device, Virgil or otherwise, be able to join a clocking group without an explicit invitation generated through human intervention on one of the existing group members.

### Syncing Group Members

Each Virgil keeps its own list of the group's members, and the group API only accepts members on that list. So every member has to agree on who's in the group, including members that were offline when someone joined or left.

Every entry in the list therefore records when it last changed: a member (with its name and certificate), or a removal (with the removed Virgil's certificate fingerprint). Two lists are merged by keeping, for each Virgil, whichever entry changed most recently. A removal therefore beats the joining that came before it, and re-inviting a removed Virgil beats its removal. Clock Boxes all keep the same time (which is rather the point of them), so "most recently" is reliable here. If a member and a removal changed at exactly the same moment, the removal wins.

Changes spread in two ways:

- **Straight away:** the member making a change (accepting a newcomer, or removing someone) sends its list to every member currently online.
- **Catching up:** every heartbeat carries a short digest of the sender's list. A member whose own digest differs will fetch the sender's list (at most every five seconds) and merge it into their own.

Any member can change the list, just as any member can invite a newcomer.

### Removing and Leaving

"Remove from group" in the web UI and `sudo virgilctl group remove <name>` both remove another member. The removed Virgil is told to leave the group (while it still trusts the member removing it), and then every other member is informed of the departure. If the removed Virgil was the leader, the remaining members drop it from their election straight away, and elect a new leader within a few seconds.

A Virgil that has been removed from a group but was not online at the time of its removal will return to find no other Virgils talking to it. After a minute of no group activity, it will reach out to the other Virgils in its (former) group and ask if has been kicked out. If they reply with yes, the web UI will offer to clear Virgil's group membership and forget its former groupmates.

If the Virgil being removed is the one you are working from, "Remove from group" is replaced with "Leave group" in the web UI or `sudo virgilctl group leave`. Leaving is just like a removal, except instead of being told that it has been removed from the group, Virgil announces its departure.

A removed member's certificate can't be taken out of the group port's running TLS setup, so until `virgild` restarts, its connections still get as far as each request. Every request checks the sender against the current member list, though, so the connections are refused.

### Shared Settings

Most settings describe one Clock Box and do not need to be shared. Two settings are shared by the whole group:

- **`auto_trim`** only matters on the leader, but leadership moves! If it were on in one Clock Box and off in another, auto-trim would quietly switch on and off whenever the lead changed hands.
- **The web UI's password:** members already trust each other, so it makes sense to set it once for the whole group. Every Clock Box in a Virgil group will thus share the same web UI password. Note that when a Virgil leaves the group, it will keep the group password until changed by another group (or manually by you).

These spread the same way as the member list: each setting records when it was last changed, heartbeats carry the changes, and a member that hears a newer change adopts it. A change made on any Clock Box therefore reaches every other within a second or so, including those that were offline at the time (once they're back). If two Clock Boxes had different passwords before sharing them (when one joins a group, for example), the more recently set password is adopted by the whole group. Every change goes into the event history (see [The Event History](#the-event-history)).

### TLS

Each Virgil generates its own self-signed certificate (EC P-256) when it's installed.

That certificate is the Virgil's identity. When a Virgil joins a group, every member of that group keeps a copy of the new member's certificate, and the group API (port 8741) will only accept connections presenting a group member's certificate, using mutual TLS. This is how members authenticate each other without any shared password or key. This secures clocking groups with two layers of security: explicit, one-time invites, and TLS authentication amongst group members. 

A certificate from your own CA (or a public one) can be imported for use in the web UI (see [Web Certificate](#web-certificate)). It's a second certificate, used only in the context of the web services, so a custom certificate does not change Virgil's identity within its group.

### Heartbeats

Every Virgil sends a heartbeat to the group once a second, through the group API. A heartbeat carries:

- `id`: the Virgil's ID.
- `name`: its display name.
- `version`: the version of Virgil it's running.
- `state`: its election state (see [States](#states)).
- `coin`: its coin (see [The Coin Toss](#the-coin-toss)).
- `leader`: the ID of the Virgil it's following (or nothing, if it's the leader).
- `lost_leader`: whether it has lost its leader, and is waiting for the coin to decide who takes over.
- `handover_to`: the ID of the Virgil it's handing the lead to, while a handover is in progress (see [Become Leader](#become-leader)).
- `observing`: whether it's only observing the election, rather than taking part (see [Configuration](#configuration)).
- `established`: whether it had been part of the group before it started leading (see [Rules](#rules)).
- `ptp_identity`: the identity its ptp4l announces as grandmaster.
- `ptp_warnings`: any warnings from watching PTP (see [Watching PTP](#watching-ptp)).
- `dante_devices`: its local Dante devices, each with:
  - `name`, `address`, `mac` and `model`;
  - `ptpv2` and `aes67`: whether each is turned on;
  - `v1` and `v2`: what the Dante device is doing on each PTP version (following, leading, bridging, passive or off);
  - `grandmaster` and `servo`: the Dante device's PTPv2 grandmaster, and the state of its clock servo;
  - `follows_this_virgil`: whether it's following this Virgil (see [Each Device's Own View](#each-devices-own-view));
  - `clock_offset_ppb`: how hard it's pulling its own clock (see [Automatic Trim](#automatic-trim));
  - `latency_ns` and `latency_average_ns`: its worst and average receive latency across its flows;
  - `latency_setting_ns`: its receive latency setting;
  - `late_packets`: its late packets, counted across all its flows;
  - `warnings`: any warnings about it.
- `clock`: what chrony and phc2sys are doing:
  - `source`: what chrony is following (the leader's address on a follower, or the PHC on the leader);
  - `system_offset_ns`, `last_offset_ns` and `rms_offset_ns`: the system clock's offset from that source, as chrony measures it;
  - `frequency_ppm` and `skew_ppm`: chrony's estimate of the system clock's frequency error, and how uncertain that estimate is;
  - `synchronised`: whether chrony considers itself synchronised;
  - `phc_offset_ns`, `phc_adjustment_ppm`, `phc_locked` and `phc_read_delay_ns`: phc2sys's figures for the PHC, on a follower;
  - `phc_trim_ppm`: the trim, on the leader.
- `alerts`: its open alerts (see [Alerts](#alerts)), each with an `id`, `kind`, `severity`, `label`, `device`, `title`, `summary`, and the time it opened (`since`).
- `recording`: whether it's logging (`active`), and how much it has written so far (`size`), or nothing if it isn't logging.
- `serving`: what it's serving:
  - `v1` and `v2`: whether it's serving each PTP version;
  - `v1_reason`: why it is or isn't serving PTPv1 (see [Serving PTPv1](#serving-ptpv1)).
- `members_digest`: a short digest of its member list (see [Syncing Group Members](#syncing-group-members)).
- `shared_settings`: the settings every member shares, as well as when it was changed (see [Shared Settings](#shared-settings)).
- `device_settings`: its local Dante devices' enforce settings, by MAC address, each with `native_ptpv2` and `aes67` (see [Enforcing PTPv2 and AES67](#enforcing-ptpv2-and-aes67)).

Every field is checked when the heartbeat arrives. Heartbeats are how every Virgil's web UI can show the whole group, rather than just itself.

## Watching PTP

Each Virgil listens to all the PTP traffic on its PTP interface, using a raw socket. A filter in the kernel passes only PTP (UDP ports 319 and 320) up to Virgil, so it ignores audio streams (which are necessarily on the same interface). From what it hears, Virgil learns:

- **Its own grandmaster identity**, from the Announce messages its own ptp4l sends. When those Announces stop, Virgil knows ptp4l has stopped serving (and gets rightfully upset).
- **Its local Dante devices.** Anything sending PTP on the PTP interface must be local, since PTP isn't (*shouldn't be*, but that's up to you to sort out) allowed to cross the wireless link. For each device, Virgil notes whether it's sending PTPv1 Sync messages (leading PTPv1), whether those Syncs come from a boundary clock naming this Virgil as their grandmaster (bridging this Virgil's PTPv2 into PTPv1; see [Serving PTPv1](#serving-ptpv1)), whether it's sending PTPv1 Delay Requests (following this Virgil over PTPv1), and whether it's sending PTPv2 Delay Requests (following this Virgil over PTPv2).
- **Leaks and impostors.** An Announce from another member of the group means PTP is crossing the link, which defeats the whole design, and as such Virgil will complain. An Announce from any other grandmaster gets a warning too (unless it's a local Dante device that has already been reported for making itself the grandmaster; see [Dante Devices](#dante-devices)).
- **Silence.** If nothing but this Virgil has sent PTP on its PTP interface for 60 seconds, while the interface has a link, Virgil will alert you. On the test bench, a switch that had stopped passing PTP between ports looked exactly like this (see [Switches and the Link](#switches-and-the-link)).

## Dante Devices

Virgil was built and tested with two mixing consoles, but nothing in it is specific to consoles. Any Dante device can follow a Virgil's PTP: an interface, a stage box, a headphone amp, a wireless microphone receiver, and so on. This section explains how Virgil finds and watches the Dante devices on its PTP interface, and what it can change on them.

### netaudio

Virgil talks to Dante devices through [netaudio](https://github.com/BRCook/netaudio), a Dante control tool by Christopher Ritsen. Virgil uses my fork of it, which adds PTPv2 control (among many other things). Virgil runs netaudio's daemon on the PTP interface, and reads its HTTP API. netaudio's daemon won't start without an IP address, so while the PTP interface has none, Virgil runs it on the management interface instead, and moves it back once the PTP interface has an address. netaudio is installed from prebuilt release files.

netaudio sees every Dante device it can reach, which includes devices on the far side of the link. Every five seconds, Virgil reads netaudio's list of devices and keeps only the ones [Watching PTP](#watching-ptp) has heard on the PTP interface — this Virgil's local Dante devices. For each of those, it also reads netaudio's diagnostics: every receive flow's latency history, and its late-packet count.

If netaudio can't be reached, Virgil drops its readings for the Dante devices, rather than keep showing ones that are out of date.

### Each Device's Own View

netaudio reports what each Dante device itself says about its clock: the state of each of its PTP ports (following, leading, passive or off, for each PTP version), its PTPv2 grandmaster, the state of its clock servo, and its PTPv1 grandmaster. Virgil takes each device at its word wherever it can, since the device (ostensibly) knows best:

- A Dante device is "following this Virgil" if its PTPv2 grandmaster is this Virgil, or (when its PTPv2 port has gone passive) if its PTPv1 grandmaster is this Virgil.
- A Dante device leading PTPv1 with this Virgil as its grandmaster is bridging this Virgil's PTPv2 into PTPv1 (see [Serving PTPv1](#serving-ptpv1)).

Only when those figures are missing does Virgil fall back on the Delay Requests it hears itself.

Virgil warns when a Dante device:

- has PTPv2 turned off, so it can't follow this Virgil directly (it may still follow over PTPv1);
- has PTPv2 on, but isn't following this Virgil;
- is following another grandmaster (which Virgil names, if it's another member of the group);
- has made itself the grandmaster, which almost always means it can't hear this Virgil's PTP (see [Switches and the Link](#switches-and-the-link));
- has had PTPv2 on for 20 seconds, but its servo still isn't synchronised;
- has AES67 mode set, but is waiting for a restart before it takes effect.

### Enforcing PTPv2 and AES67

A Dante device has to speak PTPv2 to follow Virgil directly. There are two ways to turn PTPv2 on:

- **Native PTPv2** turns on PTPv2 alone. My netaudio fork can do this, but it's only been tested on the Behringer Wing. Native PTPv2 allows AES67 mode to remain off.
- **AES67 mode** turns on PTPv2 too, but changes more than just the clock, and requires a 48 kHz sample rate.

Each Dante device has two settings, "Enforce native PTPv2" and "Enforce AES67", both off by default. While one is on, Virgil checks that setting on the device, and turns it back on whenever it finds it off (at most once every 30 seconds). This matters because some devices forget these settings: the Wing, for one, forgets native PTPv2 every time it reboots. Virgil only ever turns these settings *on*, never off. If you've asked Virgil to enforce something, turning it off on the device itself won't stick — Virgil will just [turn it on again](https://youtu.be/FCagg5Ax7EU), like you asked.

A Dante device's settings belong to the Virgil it's plugged into, which includes them in its heartbeats. Changing a device's setting from another Virgil's web UI sends the change to that device's own Virgil through the group API, which applies and saves it exactly as if it had been changed locally. If that Virgil can't be heard, its devices' settings can't be changed, and the web UI greys them out.

## Serving PTPv1

Dante's native clock runs on PTPv1, but some (all?) Dante devices can use PTPv2. A Dante device with PTPv2 turned on acts as a bridge: it follows its Virgil over PTPv2, and leads PTPv1 for everything else on its side as a *boundary clock*, naming the Virgil (by its MAC address) as the grandmaster. A Dante PTPv2 devices thus acts as a sort of translator, ingesting Virgil's PTPv2 and spitting out PTPv1 for any devices that don't speak PTPv2.

That said, it's possible that *no* available Dante devices speak PTPv2, so Virgil can serve PTPv1 when necessary.

### How It Serves PTPv1

Serving PTPv1 meant writing a PTPv1 grandmaster, because there isn't a maintained one for Linux. ptp4l only speaks PTPv2, and PTPd 1.x (the last PTPv1 implementation I could find) hasn't been maintained since around 2010, and only ever used software timestamps. That's not surprising: PTPv2 replaced PTPv1 in 2008, and almost nothing but Dante still uses PTPv1. Dante does, though, and turning PTPv2 on isn't always possible. Dante Controller only offers it as part of AES67 mode. Dante Domain Manager can turn native PTPv2 on by itself, but only while the device is enrolled in a domain: take it out, and it drops back to PTPv1 (and Audinate, for reasons that can only be explained by money, doesn't offer the switch anywhere else). My netaudio fork can now flip that switch directly, but not every device will accept it. Some Dante devices will therefore only ever speak PTPv1, and with nothing suitable already packaged for Linux, Virgil brings its own.

Virgil's PTPv1 grandmaster sends a Sync message every 0.25 seconds, each followed by a Follow-Up message carrying the exact time the Sync left. It answers each Delay Request with a Delay Response. All of these use the subdomain `_DFLT` (as Dante does) and announce the flags `PTP_EXT_SYNC` (synchronised to an external reference), preferred, and stratum 1. Together, those make Dante devices choose Virgil as their PTPv1 leader (rather than the typical Dante behaviour of electing a leader amongst themselves).

The PTPv1 grandmaster runs inside `virgild` (the only instance where `virgild` is in the timing path) and shares ports 319 and 320 with ptp4l. It uses the network card's hardware timestamps, which are in PHC time — the same clock ptp4l serves over PTPv2 — so Virgil's PTPv1 and PTPv2 agree exactly. ptp4l has already set the network card up to timestamp every packet, so the grandmaster only has to ask to be given those timestamps, and never changes the card's settings. If a card gives no hardware timestamps, the grandmaster falls back on the kernel's software timestamps, and Virgil will display an alert.

Sharing the card's timestamps isn't entirely free, though. When a program asks for an outgoing packet to be timestamped, the card keeps that timestamp in a *transmit timestamp slot* until the program collects it. The Intel i226 (with a recent kernel) has four slots, so ptp4l and the PTPv1 grandmaster can each have a packet waiting without getting in each other's way. Some cards have only one. On those (such as the Realtek RTL8126), a request made while the slot is full is simply ignored. If the ignored request is ptp4l's, ptp4l gives up with "timed out while polling for tx timestamp" and resets its port, which Dante devices see as their grandmaster disappearing. So, on a card with one slot, the PTPv1 grandmaster takes its transmit timestamps from the kernel instead. Timestamps for packets the card *receives* don't use the slots, however.

### Transmit Timestamp Slots

Virgil plans around what it has *measured* about each card's transmit timestamp slots, since the card itself doesn't report how many it has. Until a card has been measured, Virgil treats it as having one slot: the PTPv1 grandmaster leaves the card's transmit timestamps to ptp4l, and takes its own from the kernel. Virgil is built around PTPv2, so this prevents PTPv1 from overruling PTPv2.

Virgil measures a card by probing it, during a setup from scratch (see [Setting Up From Scratch](#setting-up-from-scratch)). At that point ptp4l is stopped and the Dante devices are re-locking anyway, so nothing depends on the card for the few seconds the probe takes. The probe sends small test frames, each asking the card for a hardware transmit timestamp, and counts how many timestamps come back:

1. **Hardware timestamps at all:** one frame. If no timestamp comes back, the card can't timestamp what it sends, and there's nothing more to measure.
2. **Slots:** several sender processes, one per CPU core (up to ten), wait for the same instant on the system's clock, then each send one frame. That asks the card for that many timestamps at once, so the number that come back is the number of slots (up to the number of CPU cores). Twenty such rounds are run, and the median is the count, so a round thrown off by the operating system doesn't skew it. If every sender gets its timestamp, all the probe can say is that the card has *at least* that many slots.
3. **Contention:** two of above slot processes, standing in for ptp4l and the PTPv1 grandmaster (which are separate programs, and can send at the same moment on different cores), each send a frame at the same instant, 50 times. A card that can't share loses a timestamp from nearly every pair; one that can share will lose none. This test decides whether sharing is possible at all. On a machine with a single core, the two senders share it, so their frames go out back to back rather than together; a slot stays occupied until the driver collects its timestamp, so a card that can't share still loses timestamps.
4. **Load,** only if the contention test passed: the two senders at 100 times their real rates (ptp4l's 8 Syncs a second, and PTPv1's 4), independently, for one second. If any timestamps go missing, the test runs again at 50 times the real rates for two seconds, then at 10 times for five, stopping at the first that loses nothing. Losses at 100 times that vanish at 50 times are the card (or the CPU) falling behind on sheer volume, rather than running out of slots. The level that passes is the card's margin; a card that loses timestamps even at 10 times can't share.

A timestamp that doesn't reach the probe isn't necessarily one the card dropped: on a busy machine (especially at boot, with few cores), a sender can give up waiting before its timestamp arrives. Each sender therefore waits up to 200 ms for its timestamp, reads the NIC driver's lost timestamp counters. A loss only counts against the card if those counters rose. Drivers that don't keep track of lost timestamps are judged on the losses measured by Virgil, which is more likely to yield a false negative.

The test frames are plain Ethernet frames with the EtherType reserved for local experiments (`0x88B5`), sent to an unused, locally administered address. They aren't IP or PTP, so Dante devices ignore them, and switches discard them. In the worst case, the probe takes about ten seconds.

The result is saved in `/var/lib/dante-virgil/timestamping-probe.json`. Like the evidence below, it belongs to one card, on one driver, on one kernel, so a card is only probed again after a driver or kernel update. The probe can be turned off with the `probe_timestamps` setting (or "Probe timestamp slots at boot" in the web UI); a card that has never been probed stays unmeasured, and is treated as having one slot. If the probe can't run (because the PTP interface has no link, for example), the card stays unmeasured until the next setup from scratch. The probe never stops the timing services from starting.

Once a card is known to have more than one slot, ptp4l and the PTPv1 grandmaster both use its transmit timestamps. Every 30 seconds, Virgil checks the driver's statistics (`ethtool -S`) for a rising count of skipped or timed-out transmit timestamps, and ptp4l's log for "timed out while polling for tx timestamp". Either one means the card couldn't keep up with both PTPv2 and PTPv1, so the PTPv1 grandmaster moves to software timestamps, and Virgil records the card in `/var/lib/dante-virgil/timestamping.json` as having one slot. That record outlasts reboots. It belongs to one card, on one driver, on one kernel, so after a driver or kernel update, the card gets measured again.

The web UI shows each card's slots in the small table of facts under the PTP interface, with the full story (including where the figure came from) on hover. When the card has one slot (or hasn't been measured yet), the PTPv1 section explains that PTPv1 takes its transmit timestamps from the kernel while ptp4l is running. `virgilctl interfaces` shows the same in its TX SLOTS column.

Timestamps for packets a card *receives* don't use the slots, so the PTPv1 grandmaster takes those from the card whenever it can. The RTL8126 is the exception: its NIC profile sets its receive filter to PTPv2 event messages only (`hwstamp_ctl -r 12`), so it doesn't timestamp received PTPv1 packets at all. On that card, the PTPv1 grandmaster uses the kernel's timestamps in both directions, and Virgil warns that PTPv1 is using software timestamps only.

### Measurements

On the test bench, a Wing Compact followed Virgil's PTPv1 within seconds of it starting, with its PTPv2 port going passive (as expected). Here's its clock correction (how hard it was pulling its own clock to follow) on each path:

| Path | Spread | Range |
|---|---|---|
| PTPv2, from ptp4l | 0.22 ppm | +1.48 to +2.73 ppm |
| PTPv1, with software timestamps | 2.19 ppm | −4.05 to +11.18 ppm |
| PTPv1, with hardware timestamps | 0.87 ppm | +0.39 to +2.74 ppm |

Hardware timestamps made PTPv1 usable, but PTPv2 was still about four times steadier (not sure why). The timestamps at Virgil's end were equally precise on both paths, so the remaining difference is most likely in the Wing's own PTPv1 servo.

### When It Serves

The `ptpv1` setting chooses when Virgil serves PTPv1:

- **`fallback`** (the default) serves PTPv1 only when nothing local can bridge this Virgil's PTPv2 into PTPv1. Virgil stands by while a local Dante device is bridging (seen within the last 15 seconds), while netaudio shows a local Dante device with PTPv2 or AES67 on (such a device could bridge, and would go passive if Virgil served instead), and while no local Dante device uses PTPv1 at all. After `virgild` starts, it watches the network for 20 seconds before deciding anything.
- **`always`** serves PTPv1 regardless.
- **`off`** never serves PTPv1.
- **`only`** serves PTPv1 *instead of* PTPv2: ptp4l doesn't run at all (see [Serving PTPv1 Instead of PTPv2](#serving-ptpv1-instead-of-ptpv2)).

Every time Virgil starts or stops serving PTPv1, every PTPv1 device on its side has to notice, and then choose a new PTPv1 leader. Both Virgil leaders serve the same clock, so the time doesn't jump, but every Dante device still has to change course and settle again. Virgil therefore avoids switching on a whim: under `fallback`, it re-checks every five seconds whether it should be serving PTPv1, but only switches once the answer has been "yes" or "no" for 15 seconds without a break. If a bridging device reboots, for example, Virgil doesn't step in the moment it stops bridging — Virgil re-classifies the device as non-bridging after 15 seconds of silence, and then only decides to change PTPv1 modes once that state holds for another 15 seconds, so Virgil steps in after about half a minute. If the console comes back before then, the pending switch is simply dropped, and its Dante devices never notice it was gone. A change made from the web UI applies straight away, since we assume you know what you're doing.

### Serving PTPv1 Instead of PTPv2

Some deployments have no Dante devices that can speak PTPv2 at all. There, ptp4l serves nobody, and only takes the card's transmit timestamp slots away from PTPv1. The `only` setting ("Instead of PTPv2" in the web UI) turns ptp4l off, and leaves PTPv1 to serve on its own.

Nothing else changes. ptp4l normally turns the card's hardware timestamping on when it starts, so without it, `virgil-root hwstamp` does that instead (run by `dante-virgil-hwstamp`, after a setup from scratch, and whenever ptp4l stops). With the card's transmit timestamp slots to itself, PTPv1 uses its hardware transmit timestamps, however many slots the card has. PTPv1 also serves straight away, without the usual 20 seconds of watching, and names the PTP interface's MAC address as the grandmaster (normally it learns that from ptp4l's announcements).

Switching into or out of `only` mode is *make before break*. PTPv1 and PTPv2 both serve the same PHC, so they agree on the time from the first message, and the switch doesn't need a setup from scratch:

- **Into `only` mode:** PTPv1 starts first, alongside ptp4l, and Virgil waits until the Dante devices follow it (up to 10 seconds, if any PTPv1 devices are present), then a few seconds more. Only then does ptp4l stop, and PTPv1 take over the card's transmit timestamps.
- **Out of `only` mode:** ptp4l starts first, and Virgil waits until its first Announce goes out, then a few seconds more, so the Dante devices have heard it. Only then does PTPv1 go back to sharing (if the card can), and the usual rules decide whether it carries on serving.

Starting or stopping ptp4l touches nothing else: chrony and phc2sys carry on as they were.

## Automatic Trim

### What Dante Devices Report

Each Dante device reports how hard it's pulling its own clock to follow PTP, in parts per billion (netaudio's `clock_frequency_offset_parts_per_billion`). A positive figure means the device is speeding its clock up to keep pace, so the grandmaster is running faster than the device's crystal would like.

A Dante device that has to pull hard all the time has less margin for anything else: devices pulling about 20 ppm occasionally lost lock. The leader can relieve them by moving its own rate (its *trim*) towards what the devices prefer. Auto-trim does that automatically, so that the stress of an unusually fast or slow crystal falls on the Clock Box rather than the Dante devices.

### The Rule

While it leads, a Virgil records the figures of every Dante device in the group that's following *any* group member every five seconds, ignoring any figure beyond ±100 ppm (which is unsalvageable). Once a minute, it considers a trim step:

1. It averages each device's figures over the last five minutes, then averages those across the devices.
2. If the average is within ±0.2 ppm (of zero), it leaves the trim alone.
3. Otherwise, it moves the trim 30% of the way towards cancelling the average, but by no more than 0.1 ppm in one step, and never beyond ±100 ppm in total. It applies the new trim through `virgil-root trim` without restarting anything, and saves it to the configuration.

Two Dante devices can't always both be satisfied. Each has its own crystal, and two crystals can differ by a few ppm, so a shared clock would never be able to accommodate both. The solution is to take the average of the whole group and try to match that; this minimizes the total offset amongst the group, even if it means individual devices might have larger offsets than if they were individually targeted.

### Why It's Slow

These trim changes are deliberately small and slow. Each trim step changes the rate that every follower is measuring. chrony on the followers averages its measurements over several minutes, to filter out the jitter from the link (which is ultimately why any of this works in the first place). That means a change of rate on the leader takes tens of seconds, or longer, to show up on the followers. With steps of 0.5 ppm every 30 seconds, chrony on a follower measured a skew of 0.6 ppm (its own estimate of how uncertain its frequency was). That's far too aggressive for chrony to follow the leader confidently, and it coincided with the follower console's receive latency climbing.

So auto-trim waits: it considers a step at most once a minute, and only when chrony on every follower reports an RMS offset under 20 µs (which shows the followers have caught up with the previous step). It also waits a full minute after the trimming Virgil takes the lead, to let things settle, and never steps until the previous trim has been applied. From a cold start, auto trim can take up to half an hour to settle to the lowest group offset, but the system should typically be ready to pass audio within only a few minutes. Half an hour is a good warm-up target to be production-ready.

### When Dante Devices Don't Respond

If the trim has moved by 1 ppm or more over its last 16 steps, but the Dante devices' average has moved by less than a fifth of that, then the devices aren't responding to the trim at all. This makes auto-trim effectively blind, and if left unchecked, auto-trim would keep pulling harder and harder with no change in group offset. In this case, auto-trim pauses, and Virgil will display an alert. The pause ends after 10 minutes, or as soon as the set of Dante devices it steers by changes, whichever comes first.

(A small but important detail: `phc_ctl` reads a negative number as a command-line option, so every `phc_ctl` command Virgil runs puts `--` before its arguments. Without it, any negative trim would fail.)

## Alerts

netaudio's daemon already detects audio problems on Dante devices — lost clock locks, muted audio, late packets and failed subscriptions — and reports them as *issues* (from netaudio 2.0 onwards). Every three seconds, Virgil reads the open issues for its own local Dante devices. Each Virgil only reports its own local devices.

When a new issue opens, Virgil logs it as an error, shows it on every Virgil's web UI in a red banner, and includes it in its heartbeats. When the issue closes, Virgil logs how long it lasted. Every opening and closing is also written to the event history (see [The Event History](#the-event-history)). To keep the banner readable, alerts are grouped into one bar per device, with at most three bars visible at a time.

### Out-of-Date Issues

netaudio keeps an issue open until it sees the problem clear. If the device or flow goes away entirely, it never sees that, and the issue stays open indefinitely. Virgil therefore ignores any issue netaudio hasn't seen for two minutes, whatever its state says, and records it as having "gone quiet".

### Recoveries netaudio Is Still Confirming

When a Dante device's clock comes back after a lost lock, netaudio keeps the issue open — summarised as "Recovering; waiting for five seconds of continuous healthy clock status" — until it has seen five healthy seconds in a row. In one test, an issue stayed that way for two hours, reopening every 10 to 15 minutes, while the device reported itself synchronised throughout. The cause is in netaudio: its five-second count starts again whenever two healthy observations are further apart than its freshness window, so a device it observes less often than that can never complete the count.

Virgil therefore shows a "Recovering" issue quietly (as a note in Events, rather than a red banner), but only while the device itself reports that it's synchronised and following its Virgil. If it doesn't, or stops doing so, the issue becomes a full alert again immediately.

### The Event History

Alerts share one history with the things the group does, so the order of events is clear: a lock lost just after a new leader took over, for example, will make it abundantly clear if there is a causal relationship. Each Virgil records what it sees:

- Virgils joining, leaving, or being removed (and by whom), learned as the member lists merge (see [Syncing Group Members](#syncing-group-members));
- a new leader, whoever it is;
- the group's time being set (on the leader) and each follower resynchronising to it;
- PTPv1 starting or stopping, and switching into or out of PTPv1-only mode;
- timing being stopped or started on that Virgil.

Everything goes into `/var/lib/dante-virgil/events.jsonl` with one event per line. The log is rotated at 1 MB. The latest are read back when `virgild` starts, so the web UI's Events section and `virgilctl events` show what happened before a restart, too, which may be relevant! The Events menu shows the latest ten, with the rest on request.

## The Web UI

### Reading and Changing

Anyone who can reach a Virgil can read its web UI. Changing anything requires the password, which is set with `sudo virgilctl password` (on any member: it's shared by the whole group; see [Shared Settings](#shared-settings)) and stored as a scrypt hash. Until a password is set, the web UI is read-only.

Logging in gives a session cookie that lasts 12 hours. The cookie is marked `Secure`, `HttpOnly` and `SameSite=Strict`. After five wrong passwords in a minute from one address, further attempts from that address are refused (even with the right password) until the minute is up. Every change also needs an `X-Virgil-Request` header, so no other page can make changes on a logged-in user's behalf. Changing the password logs everyone out.

The page is plain HTML, CSS and JavaScript, with no external resources, so it works on a network with no internet access. A strict content security policy only lets it load its own files. It builds every part of the page with `textContent` rather than HTML strings, because device names come from the network and could contain anything.

Plain `http://` on port 80 redirects to HTTPS on port 443. netaudio's own web UI is passed through on port 8742, behind Virgil's login. 

### Web Certificate

Browsers warn about Virgil's self-signed identity certificate. To avoid that, `sudo virgilctl tls import-web --cert fullchain.pem --key privkey.pem` installs a certificate from your own CA (or a public one) for ports 443 and 8742. Before installing it, `virgilctl` checks that the key belongs to the certificate, that the certificate is valid now, and which of the Virgil's names and addresses it covers. It warns if the certificate covers none of them, or expires within 30 days. It then installs the certificate with its key readable only by `virgild`, and restarts `virgild` to use it. The `--yes` option skips the questions, which suits automatic renewal (certbot's `--deploy-hook`, for example).

A browser only accepts a certificate for the name or address it was used to reach. To browse to a Virgil by IP address or a local name, that address or name has to be in the certificate's subject alternative names. A private CA can issue such certificates, but public CAs won't.

Port 8740 and the group port keep using the identity certificate, since `virgilctl` and the other members check that certificate specifically.

## Graphs and Logging

### Graphs

Each Virgil keeps the last ten minutes of its own measurements in memory:

- its clock offsets;
- its local Dante devices' clock corrections, receive latency (the worst flow, and the average across flows, against the device's latency setting) and late packets;
- the trim, while it leads;
- the link's round trip, jitter and packet loss, from four small pings a second to each other member (with the jitter smoothed the way RTP smooths it, as in RFC 3550).

When a web UI asks for graphs, its Virgil fetches every other member's measurements through the group API, and merges them with its own. That way, a Dante device on the far side of the link is graphed in as much detail as a local one. When there are more points than fit on a chart, latency, round trip, jitter and late packets keep each stretch's worst value (so a spike is never averaged away), while the others keep the average.

Each line in a chart's legend shows its current value, and after it, in muted text, one figure that sums it up over the window shown. Which figure depends on what the chart measures:

| Chart | Summary |
|---|---|
| Clock offset, PHC offset | Peak |
| Receive latency (worst flow) | Max |
| Receive latency (average) | Median and spread |
| Late packets | Total in the window |
| Device clocks, trim | Range, lowest to highest |
| Link round trip, jitter | Max |
| Link packet loss | Mean |

The page draws the charts itself, on canvases, with windows from 30 seconds to 10 minutes. They're arranged in three labelled rows of related charts: **Clocks** (offset from the leader, PHC offset, and the trim), **Dante devices** (receive latency across the full width, then late packets and each device's clock correction), and **Link** (round trip, jitter and packet loss). Each row's charts share its width equally (at most three side by side on a wide screen, two on a medium one, and one on a phone), so a row never has a gap in it. Each chart of times has its own unit, chosen by clicking the unit beside its title and remembered by the browser. Clock offsets start in the page's unit (nanoseconds, by default), receive latency and round trip in milliseconds, and jitter in microseconds.

### Logging

"Start logging" logs this Virgil and, if asked, every other group member it can hear. No audio is recorded. Each Virgil writes the following under `/var/lib/dante-virgil/recordings/`:

- its full status, every two seconds;
- its graphs, every 30 seconds (each covering 40 seconds, so no data is lost);
- each local Dante device's diagnostics, every 30 seconds, excluding data covered by the graphs;
- its timing services' logs;
- any notes added from the web UI.

"Stop logging and download" stops logging on every Virgil it started, fetches their logs through the group API, bundles them all into one `.tar` file, and downloads it through the browser. Logging stops by itself after 24 hours, or when the disk has 500 MB left. Internally (and in the API), a logging session is called a recording.

## Setting the Time

A Virgil's system clock isn't independent of the Dante devices' clock. On the leader, the system clock follows the PHC, and the PHC is the PTP time every Dante device follows. Setting the time of day therefore moves the Dante devices' clock, which makes every one of them re-lock, and interrupts audio for a few seconds.

Every 30 seconds, the web UI compares its Virgil's clock with the clock of the device it's open on (your laptop, for example), allowing for the time the request takes to travel there and back. "Sync time" arms on its first press, showing a warning, and acts only on a second press within 15 seconds. It then measures the difference again, and sends the correction:

1. On a follower, the request is passed to the leader.
2. On the leader, `virgil-root settime` steps the system clock by the correction, sets the PHC to match, and saves the new time to the hardware clock.
3. The leader then tells each follower to set up its timing services from scratch (see [Setting Up From Scratch](#setting-up-from-scratch)), so the follower steps its clock straight to the leader's new time. Without this, chrony on the follower would slew towards the new time instead, which could take hours.

## Networking

### Choosing Interfaces

Any interface can be the management interface. For the PTP interface, Virgil sorts the interfaces by what the kernel reports about their timestamping: hardware timestamping (offered normally, with its PHC found automatically) or software only (offered with a warning; see [Software Timestamping](#software-timestamping)).

Each interface is shown with its network card's real name, such as "Intel I226-V", rather than just its interface name. Virgil finds the card's PCI IDs in `/sys/class/net/<interface>/device/`, and looks them up in the PCI ID database (Debian's `pci.ids`, which the installer adds if it can). A card the database doesn't know yet is shown with its driver's name instead.

Some network cards need different settings. When the chosen PTP interface's driver has a *profile*, Virgil explains the difference and offers to apply it:

| Driver | Network card | Status | Differences |
|---|---|---|---|
| `igc` | Intel i225 and i226 | Tested (i226) | None. |
| `r8126` | Realtek RTL8126 | Tested | `hwstamp_ctl` must run before the PHC runs. Only one pending transmit timestamp. |
| `igb` | Intel i210 | Expected to work | Untested. |
| `r8169` | Realtek (in-kernel driver) | Tested | No PHC, so software timestamping only. |

Changing interfaces from the web UI restarts `virgild` by itself; after `virgilctl config set`, you restart it yourself (`virgilctl` says so). Changing the PTP interface also sets the timing services up from scratch, so its Dante devices re-lock once.

### Addresses

On the management interface, a self-assigned address (in `169.254.0.0/16`) only counts once it has been the interface's only address for 30 seconds. That gives DHCP time to provide a real address first, while still letting Virgil work on Dante networks without a DHCP server. On the PTP interface, any address counts immediately.

### ifupdown

Virgil is designed for Debian's default network setup, ifupdown with dhcpcd. At startup, and whenever the chosen interfaces change, `virgil-root dhcpnames` does three things:

- **It names each DHCP lease.** It adds `hostname <hostname>-ptp` (or `-mgmt`) to each chosen interface's stanza in `/etc/network/interfaces`, marked `# Set by Virgil`, so a router's list of leases shows which address belongs to which interface. It only touches DHCP stanzas, leaves alone any `hostname` line someone else wrote, and keeps a one-time backup of the original file. The new names take effect the next time each interface comes up.
- **It adds missing stanzas, when asked.** The Debian installer only configures the interface it installed through, so a Clock Box's other interface may never come up at boot. The web UI flags such an interface as "Interface not set up in OS", and choosing it offers to add a standard DHCP stanza (marked `# Added by Virgil`) and bring the interface up.
- **It keeps the PTP interface from becoming the default route.** It adds a marked block to `/etc/dhcpcd.conf` with `nogateway` for the PTP interface, and removes any existing default route through it straight away (as long as the management interface has a default route of its own).

The uninstaller removes Virgil's `hostname` lines and its `dhcpcd.conf` block. It leaves any stanzas Virgil added, since removing them could take a Clock Box off the network.

### Switches and the Link

Your network must prevent PTP from crossing the wireless bridge, but everything else should go through. That means blocking `224.0.1.129` (and `224.0.0.107`, if anything uses PTP's peer-delay mechanism). Dante's discovery and control (`224.0.0.230` to `224.0.0.233`) and its multicast audio (`239.x.x.x`) must still cross. Blocking all multicast is an easy mistake to make, and **will** break Dante.

Switches between a Clock Box and its Dante devices need care with IGMP snooping, just as with a regular Dante deployment. Please familiarise yourself with multicast handling and the capabilities of your network hardware.

You could always plug the PTP interface of a Clock Box directly into a Dante device. So long as the Dante device has multiple ports, you could use one for the Clock Box and the other for audio. However, if you need to serve *multiple* Dante devices with a single Clock Box, this may not work depending on whether the Dante device ports are switched or redundant.

## Configuration

Each Virgil has one configuration file, `/etc/dante-virgil/virgil.toml`. The web UI and `virgilctl` both edit it, and `virgild` reloads it within a couple of seconds of a change. Every setting applies to that Virgil only, except `auto_trim` and `web_password`, which are shared by the whole group (see [Shared Settings](#shared-settings)).

| Setting | Default | Meaning |
|---|---|---|
| `id` | generated at install | The Virgil ID, which identifies this Virgil within its group. |
| `name` | the host name | The name shown in the web UI and in other Virgils' status. |
| `ptp_interface`, `mgmt_interface` | none | The chosen interfaces (see [Choosing Interfaces](#choosing-interfaces)). |
| `nic_profile` | none | The network card profile in use, if any. |
| `trim_ppb` | 0 | The PHC's rate while this Virgil leads, in parts per billion.|
| `auto_trim` | true | Whether to steer the trim towards the Dante devices' preferred rate while leading (see [Automatic Trim](#automatic-trim)). Shared by the group. |
| `ntp_hwtimestamp` | true | Whether chrony may use hardware timestamps on the management interface, where it's safe (see [Hardware Timestamps for NTP](#hardware-timestamps-for-ntp)). |
| `ptpv1` | `fallback` | When to serve PTPv1: `fallback`, `always`, `off`, or `only` (instead of PTPv2) (see [When It Serves](#when-it-serves)). |
| `probe_timestamps` | true | Whether to probe the PTP interface's transmit timestamp slots during a setup from scratch (see [Transmit Timestamp Slots](#transmit-timestamp-slots)). |
| `phc2sys_kp`, `phc2sys_ki` | 0.03, 0.0005 | phc2sys's servo on a follower (see [Follower](#follower)). Gentler values make the PHC's rate steadier, at the cost of a larger offset from the system clock. |
| `chrony_minsamples` | 64 | The fewest measurements chrony on a follower keeps for its estimate of the leader's rate, from 64 to 1,024 (see [Follower](#follower)). |
| `observe_only` | false | Only take part in the election as an observer; roles are then set by hand with `virgilctl role`. Meant for testing. |
| `web_password` | none | The web UI's password, as a scrypt hash. Shared by the group. |
| `auto_trim_changed`, `web_password_changed` | 0 | When each shared setting was last changed, so the latest change wins across the group. |
| `devices` | none | For each local Dante device, by MAC address: whether to enforce native PTPv2, and whether to enforce AES67. |
| `group` | none | The group's ID, and its members with their certificates. Set by creating or joining a group. |

## APIs

### Web UI and Monitoring

The web UI's API is on ports 443 and 8740. Reading is open to anyone who can reach the Virgil; everything else needs a login.

| Method and path | Does |
|---|---|
| `GET /api/v1/status` | Everything about this Virgil, and about its members (from their heartbeats) and their Dante devices. `virgilctl` and monitoring tools read this. |
| `GET /api/v1/graphs?window=<seconds>` | The graphs, merged across the group. |
| `GET /api/v1/time` | This Virgil's clock. |
| `GET /api/v1/membership?id=<Virgil ID>` | Whether a Virgil is a member of this Virgil's group, or was removed from it. |
| `GET /api/v1/session`, `POST /api/v1/login`, `POST /api/v1/logout` | Logging in and out. |
| `POST /api/v1/lead`, `/stop`, `/start` | Becoming leader, and stopping and starting the timing services. |
| `POST /api/v1/trim`, `/auto-trim`, `/ptpv1`, `/probe-timestamps` | Settings. |
| `POST /api/v1/device` | A Dante device's enforce settings. With `virgil` set to another member's ID, the change is passed to that member. |
| `GET`, `POST /api/v1/interfaces` | Listing and choosing interfaces. |
| `POST /api/v1/group/create`, `/invite`, `/join` | Creating a group, inviting a Virgil into it, and joining one. |
| `POST /api/v1/group/remove`, `/leave` | Removing another member, and leaving the group. |
| `POST /api/v1/time/sync` | Setting the group's time (see [Setting the Time](#setting-the-time)). |
| `POST /api/v1/recording/start`, `/stop`, `/note`, `GET /api/v1/recordings[/<name>]`, `POST /api/v1/recordings/delete` | Logging sessions. |

### Group

The group API is on port 8741, and only accepts connections from other members (see [TLS](#tls)). Its paths are `/group/v1/heartbeat`, `/members`, `/ping`, `/handover`, `/device`, `/graphs`, `/settime`, `/resync`, `/recording`, `/recordings/<name>` and `/recordings/delete`.

### virgilctl

| Command | Does |
|---|---|
| `status`, `events` | Show the group, its Dante devices, clocks and alerts, or the recent history of alerts. |
| `interfaces` | List the interfaces, with their network cards, timestamping and profiles. |
| `config show`, `config set <key> <value>` | Show or change a setting. Choosing an interface offers its network card's profile, and a missing stanza, where needed. |
| `role lead`, `role stop`, `role show` | Make this Virgil the leader, stop it, or show its role. With `observe_only` set, `role leader` and `role follower` set the role by hand. |
| `group create`, `invite`, `join <token>`, `show`, `remove <name>`, `leave` | Manage the group. |
| `device <device> [ptpv2\|aes67] [on\|off]` | Show or change a local Dante device's enforce settings. |
| `trim [ppm]` | Show the trim and each Dante device's clock correction, or set the trim. |
| `password [--clear]` | Set (or clear) the web UI's password. |
| `tls init`, `show`, `import-web`, `remove-web` | Manage certificates. |

## Installation

```
git clone https://github.com/BRCook/dante-virgil.git
cd dante-virgil
sudo ./install.sh
```

`install.sh` does the following:

1. installs the Debian packages Virgil needs, skipping `apt` entirely when nothing is missing (and adding `pci.ids` if it can);
2. creates the `dante-virgil` user, with permission to read the system journal (where phc2sys's reports appear);
3. checks that `requirements.txt` matches `pyproject.toml` and `uv.lock`, refusing to go on if it doesn't, then builds the Python environment from `requirements.txt`;
4. installs the systemd units, a tmpfiles entry, the polkit rule, and an AppArmor addition that lets chrony read Virgil's configuration;
5. masks Debian's own `chrony.service`;
6. generates the Virgil ID and certificate, if they don't exist yet;
7. links `virgilctl` and `netaudio` into `/usr/local/bin`; and
8. starts `virgild`.

Running it again from a newer copy of the repository updates Virgil, keeping its configuration, ID and certificate. It only restarts `virgild` itself, not the timing services, and thus updating Virgil doesn't disturb any Dante devices. When the Python environment is rebuilt, it also restarts netaudio, so a new netaudio version takes effect straight away; that only pauses monitoring and alerts for a few seconds. `uninstall.sh` reverses the installation, and `--purge` will additionally remove the configuration.

## Testing

The test suite (`pytest`, about 480 tests) covers the configuration, interface detection, config generation (checked against the prototype's reference configuration), `virgil-root`'s requests and plans, role changes, the election (including randomised simulations of many Virgils), TLS and joining between real running daemons, heartbeats, discovery, PTP parsing and its kernel filter, Dante device monitoring against fake netaudio daemons modelled on the real one, alerts, auto-trim, the graphs, logging, PTPv1's messages and decisions, and the web UI's API and authentication. The suite must pass when run as an ordinary user, and `virgil-root`'s tests are guarded so that none of them can touch the real system.

Only consoles have been tested so far. Other Dante devices should be well-behaved (Virgil only relies on standard Dante behaviour, as reported through netaudio), but that's yet to be proven.

## Known Limitations

- **Two radio hops** haven't been tested. Every follower follows the leader directly, even when that means crossing more than one link.
- **Dante devices other than consoles** haven't been tested yet (see [Testing](#testing)).
- **Realtek management interfaces** (with the `r8169` driver) have occasionally failed to come up after a soft reboot, until the power was cut. ASPM has been ruled out, and the cause isn't known yet. Not likely related to Virgil.
- **Offline installation** isn't possible yet. A self-contained package for each architecture, with every dependency included, would allow it. It would need to carry the licences of everything it bundles, including the Rust crates inside netaudio.
