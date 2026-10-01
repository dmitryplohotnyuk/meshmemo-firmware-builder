# Imported firmware material

This repository contains patches for Meshtastic firmware and protobufs, and the
MeshMemo USB replay adapter originally maintained in the MeshMemo server project.
Their provenance and SHA-256 values are recorded in `docs/provenance.json`.

The pinned Meshtastic firmware and protobuf repositories supply GPL version 3
license texts. Copies are retained in `licenses/`. Upstream source is downloaded
at preparation time from the exact commits listed in `registry/catalog.json`;
copyright and license notices in that source must be preserved when distributing
firmware. Other upstream dependencies retain their own notices and licenses.

The import record distinguishes unchanged files from extracted patch sections.
Splitting optional patches does not change the license of the upstream material.
Newly authored builder code is licensed under [GPL-3.0-only](LICENSE), matching
the [MeshMemo server](https://github.com/dmitryplohotnyuk/meshmemo) from which the
local MeshMemo code was imported. This does not replace upstream or dependency
licenses and notices. This source repository excludes compiled firmware and
vendored dependencies; distributing firmware binaries requires the corresponding
source and notices for the exact build.
