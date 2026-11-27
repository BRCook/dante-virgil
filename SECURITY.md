# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue:

1. Go to the repository's [Security tab](https://github.com/BRCook/dante-virgil/security).
2. Choose **Report a vulnerability**.

That opens a private report only the maintainer can see. It helps to include the version of Virgil (`virgilctl status`, or the bottom of the web UI), what you did, what happened, and what you expected.

You should hear back within a week. Virgil is maintained by one person, so a fix may take longer than that, but you'll be told what's happening, and credited in the release notes if you'd like to be.

## Supported versions

Only the [latest release](https://github.com/BRCook/dante-virgil/releases/latest) gets security fixes. If you're on an older one, please update first and check whether the problem is still there.

## What's in scope

- Virgil itself: `virgild`, `virgilctl`, `virgil-root`, the web UI, and the installers and packages in this repository.
- Problems in [netaudio](https://github.com/BRCook/netaudio), which Virgil runs to talk to Dante devices, belong in that repository, unless they come from how Virgil runs it.

## What Virgil assumes

Virgil is built for a production audio network, not the open internet. Some things are by design, and aren't vulnerabilities by themselves:

- Anyone who can reach a Virgil can read its status. Changing anything needs the web UI's password.
- Members of a group trust each other completely: any member can set the group's time, change its shared settings, or remove another member.
- Dante and PTP have no authentication of their own, and NTP between Clock Boxes isn't authenticated either. Anyone on the Dante network can disturb its clocking, with or without Virgil.

The [design document](docs/design.md) describes how Virgil separates privileges and how members authenticate each other.
