# Storage Scale Azure CES hook patch

Offline patcher for IBM Storage Scale's `mmcesExtendedIpMgmt.azure`, carrying
forward the hook-only changes exercised in the October 2026 two-node Azure lab.
It writes a new candidate file. It does **not** connect to Azure, install the
hook, change CES internals, or redistribute IBM's complete script.

**Experimental, not production-qualified or an IBM-supported fix.** The patch
can remove an allowlisted secondary service-IP allocation from a peer NIC only
when Azure reports the peer VM as stopped or deallocated and CES assigns the IP
to the executing node. A power-state snapshot is not fencing. Local locking
does not serialize different nodes.

## Supported original

Use your entitled, unmodified copy of:

```text
/usr/lpp/mmfs/samples/cloud/ces_middleware/mmcesExtendedIpMgmt.azure
```

The lab used `gpfs.base-5.2.3-10`. The patcher accepts **only** the exact original
with SHA256:

```text
03412ece207f3a0be65f95a8bf55a9959f9b208e2d77c0973930604b0c318e4c
```

Other package builds, edited files and already patched hooks are rejected. There
is no force option. IBM's original header, top-level declarations and dispatcher
are preserved. Only hook functions change.

## Create a patched copy

Requires Python 3.9 or newer; no Python packages or patch utility are needed.

1. Copy `config.example.json` to `config.local.json` and replace the example
   values with your subscription, resource group, CES service IPv4 addresses,
   stable IP-configuration prefix, and exactly two protocol-node mappings.
   `ces_node` must match the full node name returned by `mmces address list -Y`;
   `nic` is the Azure protocol NIC resource name, not the Linux interface.
2. Run the patcher against the original, writing to a **new scratch file**:

```sh
python3 patch_hook.py /secure/mmcesExtendedIpMgmt.azure \
  /scratch/mmcesExtendedIpMgmt.azure.patched --config config.local.json
```

The output must not already exist. The patcher reports its SHA256 and never
overwrites the input. Configuration values are validated before being embedded
as literals; no configuration file is read when the resulting hook runs.
The two nodes and their protocol NICs must be in the specified resource group
and subscription, with their CES service addresses on the same protocol subnet.
Use resource names without spaces or shell metacharacters.

The stable Azure IP-configuration name is
`<ip_config_prefix>-<service-IP-with-hyphens>`. Existing
`<vm>-ces-<service-IP-with-hyphens>` names are also accepted for migration.
Use the same configuration on both nodes. Explicitly map both peer NIC names;
they need not follow a VM-name suffix convention.

## What changes

| Path | Behavior |
|---|---|
| Acquisition | Check current CES assignment, inspect subnet allocations, release only a recognized stopped/deallocated peer's non-primary allocation, confirm release, create locally, and read back Azure and guest state |
| Repeated acquisition | Keep an already correct local allocation/address instead of recreating it |
| Cleanup callback | Reconcile Azure ownership without removing an already working guest address |
| Removal | Require assignment to another CES node, remove only a recognized local secondary allocation, verify removal, and tolerate completed/partially completed retries |
| Local overlap | Serialize enable, cleanup and delete with a 20-second `flock` wait on `/var/mmfs/etc/.azure-ces-ip.lock` |
| Failures | Propagate command failures, refuse missing/ambiguous ownership and unexpected allocations, and emit timing/error messages on stderr |

`azure-ces.patch.in` is the lab's final pool-named unified patch, with deployment
values replaced by template tokens, a neutral lock filename, and explicit peer
NIC mappings. Configuration rendering is an adaptation: the resulting
environment-specific output has **not** thereby been live-qualified.

## Before installation

The resulting hook needs IBM `mmksh` and `mmces`, Azure CLI at `/usr/bin/az`,
`jq`, `curl`, `iproute2` with JSON output, GNU `date`, and `/usr/bin/flock`.
Each node's managed identity needs permission to inspect both VMs' instance
views, list/read the relevant NICs and update their IP configurations. Scope
permissions to the deployment; this project does not create RBAC assignments.
Validate guest NIC/MAC discovery and managed-identity authentication in your
environment.

Review the generated diff and run `mmksh -n` on an approved node. Follow your IBM
support/change-control process, back up `/var/mmfs/etc/mmcesExtendedIpMgmt` on
**both** nodes with ownership and mode intact, and install the reviewed
candidate consistently during a controlled maintenance window. This repository
deliberately has no automatic installer. For rollback, restore both backups
using the same change-control process; restoring a script does not undo Azure
allocations or current CES assignments.

Do not restart a stopped peer concurrently with takeover. Do not use this as
fencing for a running, hung, partitioned or unknown-state peer. Validate orderly
movement, stopped-owner takeover, return/rebalance, duplicate calls, stale
deletes, API failures and NFS recovery in a non-production environment before
considering deployment.

## Evidence and limitations

The prior lab recorded native `mmksh` tests (21 groups per node/naming mode),
duplicate no-ops, partial-operation retries and concurrent local callbacks.
The timed predecessor recovered ordinary NFSv4.1 checksum I/O in one orderly
and one forced-owner trial, with largest I/O gaps of **75.4 and 120.8 seconds**:
the under-30-second target was **not** met. The final pool-named revision had
four live CES moves and final ownership/health checks, not a repeat of the
complete failure matrix.

Held-lock recovery, idle-client reconnection, different-target overlap,
concurrent restart safety, broader infrastructure failures, and Azure calls
exceeding CES timeouts remain unqualified. Assignment rechecks are not atomic
with Azure operations. This is a narrow port of the tested changes, not a
general high-availability controller or a fresh security audit.

## Regression tests

Run the dependency-free patcher tests:

```sh
python3 -m unittest discover -s tests -v
```

The stateful hook regression suite is skipped unless you provide an entitled
original and `jq`. To exercise the actual generated hook, on a node with native
IBM `mmksh`:

```sh
export IBM_AZURE_HOOK=/secure/mmcesExtendedIpMgmt.azure
NAMING=legacy python3 -m unittest discover -s tests -v
NAMING=pool python3 -m unittest discover -s tests -v
```

These tests mock Azure, CES, metadata and guest-network calls; they do not
perform cloud mutations. Run as a normal user. `JQ` can specify a jq executable
if it is not on PATH. The Windows harness uses Git Bash and an emulated lock:
it checks behavior but is **not** proof of native Korn-shell/flock semantics.
No IBM script, local environment configuration or generated hook should be
committed to this public repository.