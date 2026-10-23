# Storage Scale Azure CES hook patcher

Generate a fully patched, self-discovering `mmcesExtendedIpMgmt.azure` from your
entitled IBM Storage Scale original. No environment configuration, node list,
IP allowlist or Azure resource names are baked into the generated hook.

## Usage

Requires Python 3.9 or newer; no additional Python packages are needed.

```sh
python3 patch_hook.py /secure/mmcesExtendedIpMgmt.azure \
  /scratch/mmcesExtendedIpMgmt.azure.patched
```

The output must not exist. The patcher verifies the original, replaces its hook
functions, preserves IBM's header and dispatcher, and prints the output SHA256.
It does not install or execute the hook, contact Azure, or modify CES internals.
IBM's complete script is not distributed.

## Runtime discovery

Every invocation discovers its context from current system state:

| Source | Information |
|---|---|
| Azure Instance Metadata Service | Local subscription, resource group, VM name and NIC MAC addresses |
| Guest network interfaces | MAC address of the interface passed by CES |
| Azure NIC inventory | Local NIC identity, protocol subnet, and existing service-IP allocations |
| `mmgetstate -Y` | Local Storage Scale node name |
| `mmces node list -Y` | Current CES membership and daemon addresses |
| `mmces address list -Y` | Current service-IP assignment |
| Azure VM and NIC relationships | Identity and attachment of an allocation's owning VM |
| Azure VM instance view | Current peer power state |

Peer membership is established by matching a primary address on one of the
owning VM's attached NICs to exactly one other CES node's daemon address.
This does not depend on VM names matching CES hostnames, DNS naming conventions,
node counts, NIC suffixes or a preconfigured topology.

Adding/removing nodes or changing service-IP assignments does not require
repatching the hook. Ambiguous or undiscoverable relationships fail closed.
Discovery is scoped to the local Azure subscription; resource groups may differ.
CES daemon addresses must be primary private addresses on the corresponding VM's
NICs. IPv6, VM scale-set NIC discovery and cross-subscription takeover are not
supported.

## Hook behavior

- Acquisition checks the CES assignment, reconciles Azure allocation, and
  verifies both Azure and guest state. Repeated calls preserve correct state.
- Stopped-peer takeover releases only a recognized secondary IP configuration
  on a verified peer CES VM, after Azure reports stopped or deallocated.
  Release is confirmed before local acquisition.
- Cleanup reconciles Azure without removing an already working guest address.
- Removal requires assignment to another CES node, touches only the local
  recognized secondary allocation, and verifies removal.
- Local callbacks are serialized with a 20-second `flock` wait. Partial
  operations can be retried, and failures are not reported as success.

IP configurations retain IBM's `<vm>-ces-<service-IP-with-hyphens>` naming
convention, using the owning VM's name. Unrecognized
names, primary allocations, duplicate allocations and uncertain CES ownership
are refused.

## Original compatibility

The supported original is the `mmcesExtendedIpMgmt.azure` supplied with
`gpfs.base-5.2.3-10`, normally at:

```text
/usr/lpp/mmfs/samples/cloud/ces_middleware/mmcesExtendedIpMgmt.azure
```

Required SHA256:

```text
03412ece207f3a0be65f95a8bf55a9959f9b208e2d77c0973930604b0c318e4c
```

Other versions, modified files and already patched hooks are rejected. There
is no force option. Runtime topology independence does not imply compatibility
with every IBM hook version.

## Runtime requirements and installation

The generated hook needs IBM `mmksh`, `mmgetstate` and `mmces`, Azure CLI at
`/usr/bin/az`, `jq`, `curl`, `iproute2` with JSON output, GNU `date`, and
`/usr/bin/flock`. It uses managed-identity login.

### Managed identity permissions

**Additional permissions are required beyond managing the executing node's
own NIC.** Every CES node's managed identity must be able to discover peer NICs,
read peer VM identity and power state, and remove a stale secondary allocation
from a peer's protocol NIC. Local-NIC-only permissions are insufficient.

| Azure RBAC action | Required scope and purpose |
|---|---|
| `Microsoft.Network/networkInterfaces/read` | Subscription scope, because discovery uses the subscription-wide NIC list API, including daemon and protocol NICs |
| `Microsoft.Compute/virtualMachines/read` | All potential peer CES VMs, to verify VM identity and attached NICs |
| `Microsoft.Compute/virtualMachines/instanceView/read` | All potential peer CES VMs, to inspect stopped/deallocated state |
| `Microsoft.Network/networkInterfaces/write` | All participating protocol NICs, to create/remove secondary IP configurations, including on a peer |
| `Microsoft.Network/virtualNetworks/subnets/read` | Relevant protocol subnets, for subnet access during NIC configuration |
| `Microsoft.Network/virtualNetworks/subnets/join/action` | Relevant protocol subnets, to authorize assigning an IP configuration to the subnet |

IP-configuration create/delete operations update the NIC; they do not require
permission to delete the NIC itself. VM start, stop, deallocate and delete
permissions are **not** needed: the hook observes power state but never fences
or changes VM power state.

Use custom roles for these actions where practical. Keep NIC write and subnet
join permissions scoped to the participating protocol resources, not the whole
subscription. For a simpler but broader built-in-role alternative, subscription
Reader covers discovery and VM reads, while Network Contributor on the protocol
NICs and relevant subnets covers network changes. This grants more permissions
than the listed actions and should be reviewed against your access policy.

Apply permissions to every node identity (or the shared managed identity in use).
When topology changes, role assignments must cover the new VMs, protocol NICs
and subnets even though the hook itself needs no repatching. Allow for RBAC
propagation before enabling failover. Missing permissions cause the operation
to fail; this repository does not create role assignments.

See Microsoft's [Compute permissions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute)
and [Networking permissions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/networking)
references for action definitions.

### Installation

Review the generated diff and run `mmksh -n`. Back up the active hook at
`/var/mmfs/etc/mmcesExtendedIpMgmt` on all CES nodes with ownership and mode
preserved, then install the reviewed replacement consistently through your
change-control process. No automatic installer is included.

Rollback means restoring those backups consistently; it does not undo Azure
allocations or CES assignments.

**Safety boundary:** this is not a fencing mechanism or an IBM-supported fix.
Power-state snapshots and CES assignment rechecks are not atomic with Azure
changes. Do not restart a peer during takeover. Local locking does not serialize
different nodes. Running, hung, partitioned and unknown-state peers are not
eligible for forced release. Azure calls may exceed CES timeouts. Validate
failover and application recovery for your deployment; no recovery-time
guarantee is made.

## Automated tests

```sh
python3 -m unittest discover -s tests -v
```

Patcher tests have no external dependencies. Hook behavior tests additionally
need an entitled original, `jq`, and IBM `mmksh`:

```sh
export IBM_AZURE_HOOK=/secure/mmcesExtendedIpMgmt.azure
python3 -m unittest discover -s tests -v
```

Tests mock Azure, CES, metadata and guest-network operations, without cloud
mutations. Set `JQ` if it is not on PATH. Windows uses Git Bash and an emulated
lock rather than native Korn-shell/flock semantics. Do not commit entitled
originals or generated hooks.
