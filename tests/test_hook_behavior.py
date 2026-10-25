import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import patch_hook
import shutil

ORIGINAL = os.environ.get("IBM_AZURE_HOOK")
JQ = shutil.which("jq") or os.environ.get("JQ")
COMPAT = os.name == "nt"
SHELL = os.environ.get("KSH") or (
    r"C:\Program Files\Git\bin\bash.exe" if COMPAT else "/usr/lpp/mmfs/bin/mmksh"
)
TIMEOUT = 120 if COMPAT else 30
MOCK = r'''
import io, json, os, pathlib, sys, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, newline="\n")
case = os.environ["CASE"]
state = pathlib.Path(os.environ["STATE"])
args = sys.argv[1:]
with state.open("a") as f:
    f.write(json.dumps(args) + "\n")
calls = [json.loads(l) for l in state.read_text().splitlines()]
guestfile = state.with_suffix(".guest")
if not guestfile.exists():
    guestfile.write_text("yes" if case in ("healthy", "delete-local", "stale-delete",
                                         "guest-delete-error", "delete-unconfirmed",
                                         "guest-only", "mixed") else "no")
subnet = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/example-network/providers/Microsoft.Network/virtualNetworks/v/subnets/protocol"
local = "example-ces01"
peer = "example-ces02"
if case == "third-node":
    peer = "example-ces03"
ip = "10.0.2.20"
if case == "dynamic-address":
    ip = "10.0.2.77"
subscription = "/subscriptions/00000000-0000-0000-0000-000000000000"
group = subscription + "/resourceGroups/EXAMPLE-SCALE"
def vm_id(vm):
    vm_group = group.replace("EXAMPLE-SCALE", "OTHER-GROUP") if case == "cross-group" and vm == peer else group
    return vm_group + "/providers/Microsoft.Compute/virtualMachines/" + vm
def nic_id(vm):
    nic_group = group.replace("EXAMPLE-SCALE", "OTHER-GROUP") if case == "cross-group" and vm == peer else group
    return nic_group + "/providers/Microsoft.Network/networkInterfaces/" + vm + "-protocol"
def nic(vm, configs, mac="00-00-00-00-00-02"):
    return {"id": nic_id(vm), "name": vm+"-protocol", "macAddress": mac,
            "resourceGroup":nic_id(vm).split("/")[4],
            "virtualMachine": {"id": vm_id(vm)}, "ipConfigurations": configs}
def primary(vm, address):
    return {"name": "primary", "privateIPAddress": address, "primary": True, "subnet":{"id":subnet}}
def config(vm, address=ip, primary=False):
    name = vm+"-ces-"+ip.replace(".", "-")
    if os.environ.get("MIXED_CASE"):
        name = name.upper()
    return {"name": name,
            "privateIPAddress": address, "primary": primary,
            "subnet": {"id": subnet}}
deleted = state.with_suffix(".peer-deleted").exists()
local_deleted = state.with_suffix(".local-deleted").exists()
created = state.with_suffix(".created").exists()
if args[0] == "state":
    if case == "local-state-error":
        sys.exit(35)
    print("mmgetstate::0:1:::ces01.example.test:1:active:")
elif args[0] == "ces" and args[1] == "node":
    if case == "node-list-error":
        sys.exit(36)
    print("mmces:nodeList:0:1:::1:ces01.example.test:ces01-daemon.example.test:10.0.1.4:e::")
    if case not in ("not-ces-peer", "single-node"):
        print("mmces:nodeList:0:1:::2:ces02.example.test:ces02-daemon.example.test:10.0.1.5:e::")
    if case == "third-node":
        print("mmces:nodeList:0:1:::3:ces03.example.test:ces03-daemon.example.test:10.0.1.6:e::")
    if case == "ambiguous-peer":
        print("mmces:nodeList:0:1:::4:ces04.example.test:ces04-daemon.example.test:10.0.1.5:e::")
elif args[0] == "ces":
    n = sum(c[0:2] == ["ces", "address"] for c in calls)
    if case == "ces-error":
        sys.exit(21)
    if case == "no-assignment":
        sys.exit(0)
    node = "ces01.example.test"
    if case in ("wrong-owner", "delete-local", "delete-absent", "guest-delete-error",
                "delete-unconfirmed", "delete-retry", "delete-once-error", "mixed") or (
                case in ("ownership-change", "free-ownership-change") and n > 1):
        node = "ces02.example.test"
    if case == "mixed":
        node = "ces01.example.test" if guestfile.read_text() == "yes" else node
    print("mmces:addressList:0:1:::"+ip+":"+node+":::")
elif args[0] == "guest":
    if case in ("guest-error", "guest-delete-error") or (
            case == "guest-once-error" and sum(c[0] == "guest" for c in calls) == 1):
        sys.exit(29)
    if case != "guest-unconfirmed":
        guestfile.write_text("no" if "del" in args else "yes")
elif args[0] == "ip":
    if "link" in args:
        print(json.dumps([{"address":"02:00:00:00:00:01"}]))
        sys.exit(0)
    if case == "guest-inspect-error":
        sys.exit(31)
    if case == "guest-inspect-malformed":
        print("{broken")
        sys.exit(0)
    active = guestfile.read_text() == "yes"
    print(json.dumps([{"addr_info":[{"local":ip, "prefixlen":24}] if active else []}]))
elif "login" in args:
    sys.exit(22 if case == "login-error" else 0)
elif "get-instance-view" in args:
    if case == "power-error":
        sys.exit(23)
    print({"running":"PowerState/running", "unknown":"None",
           "stopping":"PowerState/stopping",
           "deallocated":"PowerState/deallocated"}.get(case, "PowerState/stopped"))
elif "delete" in args:
    required = ("--subscription", "--resource-group", "--nic-name", "--name", "-o")
    if (args[:5] != ["az", "network", "nic", "ip-config", "delete"] or
            len(args[5:]) != 2 * len(required) or
            set(args[5::2]) != set(required)):
        sys.exit("Unsupported IP configuration delete arguments: "+repr(args))
    options = dict(zip(args[5::2], args[6::2]))
    target = local if options["--nic-name"] == local+"-protocol" else peer
    if (options["--subscription"] != subscription.split("/")[-1] or
            options["--resource-group"] != nic_id(target).split("/")[4] or
            options["--nic-name"] != target+"-protocol" or
            options["--name"] != config(target)["name"] or options["-o"] != "none"):
        sys.exit("Incorrect IP configuration delete target: "+repr(args))
    if case == "delete-error" or (case == "delete-once-error" and
                                 sum("delete" in c for c in calls) == 1):
        sys.exit(24)
    state.with_suffix(".local-deleted" if target == local else ".peer-deleted").touch()
elif "create" in args:
    if case == "slow":
        time.sleep(0.5)
    if case == "create-error":
        sys.exit(25)
    state.with_suffix(".created").touch()
elif "list" in args and "--query" in args:
    print(local+"-protocol")
elif "list" in args:
    if "--subscription" not in args or args[args.index("--subscription")+1] != subscription.split("/")[-1]:
        sys.exit("NIC discovery must explicitly select the local subscription")
    if case == "list-error":
        sys.exit(26)
    local_configs = [config(local)] if case in ("local", "healthy") or created else []
    if case == "name-conflict":
        local_configs = [config(local, "10.0.2.99")]
    remote_configs = []
    if case not in ("free", "single-node", "free-ownership-change", "local", "healthy", "slow", "name-conflict", "create-error",
                    "unconfirmed", "guest-error", "guest-once-error", "guest-unconfirmed",
                    "guest-inspect-error", "guest-inspect-malformed",
                    "login-error", "mixed", "guest-only"):
        if not deleted or case == "release-unconfirmed":
            c = config(peer, primary=(case == "primary"))
            if case == "unknown-primary":
                c.pop("primary")
            if case == "unexpected-config":
                c["name"] = "not-our-service"
            remote_configs = [c]
    peer_primary_ip = "10.0.1.6" if case == "third-node" else "10.0.1.5"
    nics = [nic(local, [primary(local, "10.0.1.4")] + local_configs, "02-00-00-00-00-01"),
            nic(peer, [primary(peer, peer_primary_ip)] + remote_configs)]
    if case == "unexpected-nic":
        nics[1]["virtualMachine"]["id"] = vm_id("unrelated")
    if case == "duplicate-mac":
        nics.append(nic(local, [], "02-00-00-00-00-01"))
    if case == "unattached-nic":
        nics.append({"id":nic_id("unattached"), "name":"unattached",
                     "virtualMachine":None, "macAddress":None, "ipConfigurations":[]})
    if case == "ambiguous":
        nics.append(nic("third", [config(peer)]))
    print(json.dumps(nics))
elif args[1:3] == ["vm", "show"]:
    if case == "vm-show-error":
        sys.exit(37)
    print(json.dumps({"id":vm_id(peer), "name":peer, "networkProfile":{"networkInterfaces":[
        {"id":nic_id("unrelated" if case == "detached-peer" else peer)}
    ]}}))
elif "show" in args:
    if "--query" in args:
        print(subnet)
    else:
        configs = [config(local)] if case in ("local", "healthy", "delete-local",
                    "stale-delete", "guest-delete-error", "delete-unconfirmed", "delete-retry",
                    "delete-once-error") or created else []
        if local_deleted and case != "delete-unconfirmed":
            configs = []
        if case == "unconfirmed":
            configs = []
        print(json.dumps({"ipConfigurations":configs}))
else:
    sys.exit("Unhandled mock arguments: "+repr(args))
'''


@unittest.skipUnless(JQ and Path(SHELL).is_file(), "Install jq and set KSH to a Korn shell")
class CandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not COMPAT:
            probe = subprocess.run(
                [SHELL, "-c", "function inner { value=changed; }; "
                 "function outer { typeset value=local; inner; print -r -- \"$value\"; }; "
                 "outer; print -r -- \"$value\""],
                capture_output=True, text=True, check=True,
            )
            if probe.stdout.splitlines() != ["local", "changed"]:
                raise RuntimeError("KSH must implement ksh93 function static scoping, not Bash semantics")
        if ORIGINAL:
            cls.candidate = patch_hook.patch_hook(Path(ORIGINAL).read_bytes()).decode()
        else:
            # Exercise the shipped replacement functions without distributing IBM source.
            cls.candidate = patch_hook.FUNCTIONS.read_text(encoding="utf-8") + '''
funcName=$1
shift
cmd=$1
${funcName} ${cmd}
rc=$?
return ${rc}
'''

    @unittest.skipIf(COMPAT, "Requires native ksh93 static function scoping")
    def test_ksh93_caller_context_shadowing_reproducer(self):
        original = self.candidate
        declaration = (
            "  typeset subscription vm_name resource_group vm_id nics local_nic "
            "azure_nic_id azure_nic_name subnet cesNode cesNodes\n"
        )
        try:
            for function, case, action in (
                ("reconcileAzureCesIP", "free",
                 ["enableIP", "guest_mock a replace 10.0.2.20/24 dev eth2"]),
                ("deleteIP", "delete-local",
                 ["deleteIP", "guest_mock a del 10.0.2.20/24 dev eth2"]),
            ):
                with self.subTest(function=function):
                    marker = f"function {function}\n{{\n"
                    self.candidate = original.replace(marker, marker + declaration, 1)
                    runs, calls = self.run_case(case, helper_only=False, actions=[action])
                    self.assertNotEqual(runs[0].returncode, 0)
                    self.assertIn("ownership of 10.0.2.20 for ", runs[0].stderr)
                    self.assertFalse(any("create" in c or "delete" in c or c[0] == "guest"
                                         for c in calls))
        finally:
            self.candidate = original

    def run_case(self, case, helper_only=True, address="10.0.2.20",
                 actions=None, concurrent=False, azure_client_id="", mixed_case=False):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mock = tmp / "mock.py"
            mock.write_bytes(MOCK.encode())
            state = tmp / "calls"
            prefix = '''
function az_mock { "$PYTHON" "$MOCK" az "$@"; }
function ces_mock { "$PYTHON" "$MOCK" ces "$@"; }
function state_mock { "$PYTHON" "$MOCK" state "$@"; }
function guest_mock { "$PYTHON" "$MOCK" guest "$@"; }
function curl {
  if [[ "$CASE" == metadata-error ]]; then return 30; fi
  if [[ "$CASE" == metadata-malformed ]]; then print -- '{broken'; return 0; fi
  print -- '{"compute":{"name":"example-ces01","subscriptionId":"00000000-0000-0000-0000-000000000000","resourceGroupName":"EXAMPLE-SCALE"},"network":{"interface":[{"macAddress":"020000000001"}]}}'
}
function ip {
  if [[ "$1" == -j ]]
  then
    "$PYTHON" "$MOCK" ip "$@"
    return $?
  fi
  print -- '3: eth2: <BROADCAST,UP> mtu 1500'
  print -- '    link/ether 02:00:00:00:00:01 brd ff:ff:ff:ff:ff:ff'
}
'''
            code = self.candidate
            if helper_only:
                code = code[code.index("function logAzureCesEvent"):code.index("function reconcileAzureCesIP")]
            code = code.replace("/usr/bin/az", "az_mock")
            code = code.replace("/usr/lpp/mmfs/bin/mmces", "ces_mock")
            code = code.replace("/usr/lpp/mmfs/bin/mmgetstate", "state_mock")
            code = code.replace("/usr/sbin/ip a replace", "guest_mock a replace")
            code = code.replace(
                "/var/mmfs/etc/.azure-ces-ip.lock",
                "'" + (tmp / "lock").as_posix() + "'"
            )
            if helper_only:
                code += (
                    '\ndiscoverAzureCesContext eth2 || exit $?\n'
                    'azure_ces_create_required=$(prepareAzureCesIP "$ADDRESS")\n'
                    'rc=$?; print -- "CREATE_REQUIRED=$azure_ces_create_required"; exit $rc\n'
                )
                args = []
            else:
                args = ["enableIP", f"guest_mock a replace {address}/24 dev eth2"]
            script = tmp / "candidate.ksh"
            if COMPAT:
                prefix = r'''
function print {
  local fd=1
  while [[ "$1" == -* ]]; do
    case "$1" in -u2) fd=2 ;; --) shift; break ;; esac
    shift
  done
  printf '%s\n' "$*" >&$fd
}
function jq { "$JQ" -b "$@"; }
# Compatibility-only lock: tests mutual exclusion, NOT native mmksh/flock semantics.
function flock_mock {
  local deadline=$((SECONDS + 20))
  while ! mkdir "$LOCKDIR" 2>/dev/null; do
    [[ $SECONDS -lt $deadline ]] || return 1
    sleep 0.1
  done
  trap 'rmdir "$LOCKDIR"' EXIT
}
''' + prefix
                code = code.replace("/usr/bin/flock", "flock_mock")
            script.write_bytes((prefix + "\n" + code).encode())
            env = dict(os.environ, CASE=case, STATE=str(state), MOCK=str(mock),
                       ADDRESS=address, PYTHON=sys.executable, LOCKDIR=str(tmp / "held"),
                       MSYS2_ARG_CONV_EXCL="*",
                       AZURE_CLIENT_ID=azure_client_id,
                       JQ=str(JQ))
            if mixed_case:
                env["MIXED_CASE"] = "1"
            else:
                env.pop("MIXED_CASE", None)
            shell = [SHELL, "-c", '. "$@"', "candidate"] if COMPAT else [SHELL]
            commands = [shell + [str(script)] + a for a in (actions or [args])]
            if concurrent:
                processes = [subprocess.Popen(command, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env) for command in commands]
                runs = []
                for p in processes:
                    out, err = p.communicate(timeout=TIMEOUT)
                    runs.append(subprocess.CompletedProcess(p.args, p.returncode, out, err))
            else:
                runs = [subprocess.run(command, capture_output=True, text=True,
                        timeout=TIMEOUT, env=env) for command in commands]
            run = runs if actions else runs[0]
            calls = [json.loads(l) for l in state.read_text().splitlines()] if state.exists() else []
            return run, calls

    def test_stopped_peer_release(self):
        for case in ("stopped", "deallocated"):
            with self.subTest(case=case):
                run, calls = self.run_case(case)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(sum("delete" in c for c in calls), 1)
                self.assertIn("CREATE_REQUIRED=yes", run.stdout)

    def test_free_and_local(self):
        for case, expected in (("free", "yes"), ("local", "no"), ("single-node", "yes")):
            with self.subTest(case=case):
                run, calls = self.run_case(case)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("CREATE_REQUIRED="+expected, run.stdout)
                self.assertFalse(any("delete" in c for c in calls))

    def test_case_insensitive_configuration_names(self):
        for case in ("local", "stopped"):
            with self.subTest(case=case):
                run, _ = self.run_case(case, helper_only=False, mixed_case=True)
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        delete = ["deleteIP", "guest_mock a del 10.0.2.20/24 dev eth2"]
        runs, _ = self.run_case("delete-local", helper_only=False,
                                actions=[delete], mixed_case=True)
        self.assertEqual(runs[0].returncode, 0, runs[0].stderr)
        run, calls = self.run_case("name-conflict", mixed_case=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(any("create" in c or "delete" in c for c in calls))

    def test_managed_identity_selection(self):
        for client_id, expected in (("", ["--identity"]),
                                    ("shared-identity-client-id",
                                     ["--identity", "--client-id", "shared-identity-client-id"])):
            with self.subTest(client_id=client_id):
                run, calls = self.run_case("free", azure_client_id=client_id)
                self.assertEqual(run.returncode, 0, run.stderr)
                login = next(c for c in calls if c[:2] == ["az", "login"])
                self.assertEqual(login[2:2 + len(expected)], expected)

    def test_unsafe_states_do_not_delete(self):
        for case in ("running", "unknown", "stopping", "primary", "unknown-primary",
                     "unexpected-nic", "unexpected-config", "ambiguous",
                     "wrong-owner", "no-assignment", "ownership-change", "name-conflict"):
            with self.subTest(case=case):
                run, calls = self.run_case(case)
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse(any("delete" in c for c in calls), calls)

    def test_dynamic_peer_discovery(self):
        for case in ("third-node", "cross-group", "unattached-nic"):
            with self.subTest(case=case):
                run, calls = self.run_case(case, helper_only=False)
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                deleted = [c for c in calls if "delete" in c]
                self.assertEqual(len(deleted), 1)
                if case == "third-node":
                    self.assertIn("example-ces03-protocol", " ".join(deleted[0]))
                if case == "cross-group":
                    self.assertEqual(deleted[0][deleted[0].index("--resource-group")+1],
                                     "OTHER-GROUP")

    def test_addresses_are_discovered_not_baked_in(self):
        run, calls = self.run_case("dynamic-address", helper_only=False, address="10.0.2.77")
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        create = next(c for c in calls if "create" in c)
        self.assertEqual(create[create.index("--private-ip-address") + 1], "10.0.2.77")

    def test_unverified_discovery_never_mutates(self):
        for case in ("metadata-error", "metadata-malformed", "local-state-error",
                     "node-list-error", "vm-show-error", "not-ces-peer",
                     "ambiguous-peer", "detached-peer", "duplicate-mac"):
            with self.subTest(case=case):
                run, calls = self.run_case(case, helper_only=False)
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse(any("create" in c or "delete" in c or c[0] == "guest"
                                     for c in calls), calls)

    def test_unsupported_address(self):
        run, calls = self.run_case("stopped", address="10.0.2.99")
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(any("create" in c or "delete" in c for c in calls))

    def test_errors_propagate(self):
        for case, expected in (("ces-error", 21), ("power-error", 23),
                               ("delete-error", 24), ("list-error", 26)):
            with self.subTest(case=case):
                run, _ = self.run_case(case)
                self.assertEqual(run.returncode, expected, run.stderr)

    def test_release_must_be_confirmed(self):
        run, _ = self.run_case("release-unconfirmed")
        self.assertNotEqual(run.returncode, 0)

    def test_whole_enable_success(self):
        for case in ("free", "local", "stopped"):
            with self.subTest(case=case):
                run, calls = self.run_case(case, helper_only=False)
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                self.assertEqual(sum("create" in c for c in calls), 0 if case == "local" else 1)
                self.assertEqual(sum(c[0] == "guest" for c in calls), 1)
                if case == "stopped":
                    self.assertLess(next(i for i,c in enumerate(calls) if "delete" in c),
                                    next(i for i,c in enumerate(calls) if "create" in c))
                for call in calls:
                    if "create" in call:
                        self.assertEqual(call[call.index("--name")+1],
                                         "example-ces01-ces-10-0-2-20")

    def test_whole_enable_failures_do_not_activate_guest(self):
        for case, expected in (("login-error", 22), ("running", 1),
                               ("create-error", 25), ("unconfirmed", 1)):
            with self.subTest(case=case):
                run, calls = self.run_case(case, helper_only=False)
                self.assertEqual(run.returncode, expected, run.stderr + run.stdout)
                self.assertFalse(any(c[0] == "guest" for c in calls))

    def test_guest_error_propagates(self):
        run, _ = self.run_case("guest-error", helper_only=False)
        self.assertEqual(run.returncode, 29, run.stderr)

    def test_healthy_enable_and_cleanup_are_noops(self):
        for action in (["enableIP", "guest_mock a replace 10.0.2.20/24 dev eth2"],
                       ["cleanupIP", ": ip a del 10.0.2.20 dev eth2"]):
            runs, calls = self.run_case("healthy", helper_only=False, actions=[action])
            self.assertEqual(runs[0].returncode, 0, runs[0].stderr)
            self.assertFalse(any("create" in c or "delete" in c or c[0] == "guest" for c in calls))

    def test_cleanup_repairs_azure_without_removing_guest(self):
        cleanup = ["cleanupIP", ": ip a del 10.0.2.20 dev eth2"]
        runs, calls = self.run_case("guest-only", helper_only=False, actions=[cleanup, cleanup])
        self.assertTrue(all(r.returncode == 0 for r in runs),
                        [(r.returncode, r.stderr) for r in runs])
        self.assertEqual(sum("create" in c for c in calls), 1)
        self.assertFalse(any(c[0] == "guest" for c in calls))

    def test_opposing_local_operations_do_not_remove_current_owner(self):
        enable = ["enableIP", "guest_mock a replace 10.0.2.20/24 dev eth2"]
        delete = ["deleteIP", "guest_mock a del 10.0.2.20/24 dev eth2"]
        runs, calls = self.run_case("mixed", helper_only=False,
                                   actions=[enable, delete], concurrent=True)
        self.assertEqual(runs[0].returncode, 0, runs[0].stderr)
        self.assertNotEqual(runs[1].returncode, 0)
        self.assertFalse(any("delete" in c or c[0] == "guest" for c in calls))

    def test_concurrent_enable_and_cleanup(self):
        enable = ["enableIP", "guest_mock a replace 10.0.2.20/24 dev eth2"]
        cleanup = ["cleanupIP", ": ip a del 10.0.2.20 dev eth2"]
        for case, actions in (("slow", [enable, enable]), ("healthy", [enable, cleanup]),
                              ("healthy", [cleanup, cleanup])):
            with self.subTest(case=case, actions=actions):
                runs, calls = self.run_case(case, helper_only=False,
                                           actions=actions, concurrent=True)
                self.assertTrue(all(r.returncode == 0 for r in runs),
                                [(r.returncode, r.stderr) for r in runs])
                self.assertEqual(sum("create" in c for c in calls), 1 if case == "slow" else 0)
                self.assertEqual(sum(c[0] == "guest" for c in calls), 1 if case == "slow" else 0)

    def test_repeated_and_concurrent_delete(self):
        delete = ["deleteIP", "guest_mock a del 10.0.2.20/24 dev eth2"]
        for concurrent in (False, True):
            for case in ("delete-local", "delete-absent", "delete-retry"):
                with self.subTest(case=case, concurrent=concurrent):
                    runs, calls = self.run_case(case, helper_only=False,
                                               actions=[delete, delete], concurrent=concurrent)
                    self.assertTrue(all(r.returncode == 0 for r in runs),
                                    [(r.returncode, r.stderr) for r in runs])
                    self.assertEqual(sum("delete" in c for c in calls), 0 if case == "delete-absent" else 1)
                    self.assertEqual(sum(c[0] == "guest" for c in calls), 1 if case == "delete-local" else 0)

    def test_stale_delete_preserves_local_state(self):
        delete = ["deleteIP", "guest_mock a del 10.0.2.20/24 dev eth2"]
        runs, calls = self.run_case("stale-delete", helper_only=False, actions=[delete])
        self.assertNotEqual(runs[0].returncode, 0)
        self.assertFalse(any("delete" in c or c[0] == "guest" for c in calls))

    def test_delete_errors_are_not_success(self):
        delete = ["deleteIP", "guest_mock a del 10.0.2.20/24 dev eth2"]
        for case in ("guest-delete-error", "delete-unconfirmed"):
            with self.subTest(case=case):
                runs, _ = self.run_case(case, helper_only=False, actions=[delete])
                self.assertNotEqual(runs[0].returncode, 0)

    def test_partial_enable_retry_does_not_recreate_allocation(self):
        enable = ["enableIP", "guest_mock a replace 10.0.2.20/24 dev eth2"]
        runs, calls = self.run_case("guest-once-error", helper_only=False,
                                   actions=[enable, enable])
        self.assertEqual([r.returncode for r in runs], [29, 0],
                         [(r.returncode, r.stderr) for r in runs])
        self.assertEqual(sum("create" in c for c in calls), 1)

    def test_partial_delete_retry_finishes_azure_removal(self):
        delete = ["deleteIP", "guest_mock a del 10.0.2.20/24 dev eth2"]
        runs, calls = self.run_case("delete-once-error", helper_only=False,
                                   actions=[delete, delete])
        self.assertEqual([r.returncode for r in runs], [24, 0],
                         [(r.returncode, r.stderr) for r in runs])
        self.assertEqual(sum("delete" in c for c in calls), 2)

    def test_delete_mock_rejects_unsupported_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CASE="stopped", STATE=str(Path(tmp) / "calls"))
            for args in (
                    ["--ids", "/subscriptions/s/resourceGroups/g/providers/Microsoft.Network/"
                     "networkInterfaces/n/ipConfigurations/c", "-o", "none"],
                    ["--name", "c", "-o", "none"]):
                with self.subTest(args=args):
                    run = subprocess.run(
                        [sys.executable, "-c", MOCK, "az", "network", "nic", "ip-config",
                         "delete"] + args, capture_output=True, text=True, env=env)
                    self.assertNotEqual(run.returncode, 0)
                    self.assertIn("Unsupported IP configuration delete arguments", run.stderr)

    def test_guest_success_requires_readback(self):
        run, _ = self.run_case("guest-unconfirmed", helper_only=False)
        self.assertNotEqual(run.returncode, 0)

    def test_guest_inspection_errors_do_not_activate_guest(self):
        for case in ("guest-inspect-error", "guest-inspect-malformed"):
            with self.subTest(case=case):
                run, calls = self.run_case(case, helper_only=False)
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse(any(c[0] == "guest" for c in calls))

    def test_assignment_change_before_create_does_not_create(self):
        run, calls = self.run_case("free-ownership-change", helper_only=False)
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(any("create" in c or c[0] == "guest" for c in calls))


if __name__ == "__main__":
    print("Runtime:", "Bash/Windows compatibility; native mmksh/flock NOT verified" if COMPAT
          else "native IBM mmksh/flock", flush=True)
    unittest.main(verbosity=2)
