function logAzureCesEvent
{
  print -u2 -- "[Azure] epoch=$(/usr/bin/date +%s.%N) pid=$$ event=$1 ip=$2"
}

function lockAzureCesIP
{
  exec 9>/var/mmfs/etc/.azure-ces-ip.lock || return $?
  /usr/bin/flock -x -w 20 9 9>&9 || {
    print -u2 -- "[Azure] Timed out waiting for local hook lock"
    return 1
  }
}

function checkAzureCesAssignment
{
  typeset pool mode=${3:-local}
  pool=$(/usr/lpp/mmfs/bin/mmces address list -Y) || return $?
  print -r -- "$pool" | awk -F: -v ip="$1" -v node="$2" -v mode="$mode" '
    $1 == "mmces" && $2 == "addressList" && $3 != "HEADER" && $7 == ip {
      count++
      if ($8 == node) assigned++
      if ($8 != "") owners++
    }
    END {
      exit !(count == 1 && owners == 1 &&
        ((mode == "local" && assigned == 1) ||
         (mode == "remote" && assigned == 0)))
    }
  ' || {
    print -u2 -- "[Azure] CES assignment does not confirm $mode ownership of $1 for $2"
    return 1
  }
}

function listAzureCesNics
{
  /usr/bin/az network nic list --subscription "$subscription" -o json
}

function discoverAzureCesContext
{
  typeset interface=$1 metadata link mac localState
  metadata=$(curl -fsS --connect-timeout 5 --max-time 15 --noproxy "*" \
    -H "Metadata:true" \
    "http://169.254.169.254/metadata/instance?api-version=2021-02-01") || return $?
  subscription=$(print -r -- "$metadata" | jq -er '.compute.subscriptionId | select(type == "string" and length > 0)') || return $?
  vm_name=$(print -r -- "$metadata" | jq -er '.compute.name | select(type == "string" and length > 0)') || return $?
  resource_group=$(print -r -- "$metadata" | jq -er '.compute.resourceGroupName | select(type == "string" and length > 0)') || return $?
  vm_id="/subscriptions/$subscription/resourceGroups/$resource_group/providers/Microsoft.Compute/virtualMachines/$vm_name"
  link=$(ip -j link show dev "$interface") || return $?
  mac=$(print -r -- "$link" | jq -er '
    if length == 1 then .[0].address | ascii_downcase | gsub("[:-]"; "")
    else error("Ambiguous guest interface") end
    | select(test("^[0-9a-f]{12}$"))
  ') || return $?
  print -r -- "$metadata" | jq -e --arg mac "$mac" '
    [.network.interface[] | select((.macAddress | ascii_downcase | gsub("[:-]"; "")) == $mac)]
    | length == 1
  ' >/dev/null || {
    print -u2 -- "[Azure] Guest interface does not match instance metadata"
    return 1
  }
  if [[ -n "${AZURE_CLIENT_ID:-}" ]]
  then
    /usr/bin/az login --identity --client-id "$AZURE_CLIENT_ID" -o none || return $?
  else
    /usr/bin/az login --identity -o none || return $?
  fi
  nics=$(listAzureCesNics) || return $?
  local_nic=$(print -r -- "$nics" | jq -ce --arg mac "$mac" --arg vm "$vm_id" '
    [.[] | select(
      ((.macAddress // "" | ascii_downcase | gsub("[:-]"; "")) == $mac) and
      ((.virtualMachine.id // "" | ascii_downcase) == ($vm | ascii_downcase)))]
    | if length == 1 then .[0] else error("Local Azure NIC missing or ambiguous") end
  ') || return $?
  azure_nic_id=$(print -r -- "$local_nic" | jq -er '.id | select(type == "string" and length > 0)') || return $?
  azure_nic_name=$(print -r -- "$local_nic" | jq -er '.name | select(type == "string" and length > 0)') || return $?
  resource_group=$(print -r -- "$local_nic" | jq -er '.resourceGroup | select(type == "string" and length > 0)') || return $?
  subnet=$(print -r -- "$local_nic" | jq -er '
    [.ipConfigurations[] | select(.primary == true) | .subnet.id]
    | if length == 1 then .[0] else error("Protocol subnet missing or ambiguous") end
    | select(type == "string" and length > 0)
  ') || return $?
  localState=$(/usr/lpp/mmfs/bin/mmgetstate -Y) || return $?
  cesNode=$(print -r -- "$localState" | awk -F: '
    $1 == "mmgetstate" && $3 != "HEADER" && $7 != "" { node=$7; count++ }
    END { if (count == 1) print node; else exit 1 }
  ') || {
    print -u2 -- "[Azure] Local Storage Scale node identity missing or ambiguous"
    return 1
  }
  cesNodes=$(/usr/lpp/mmfs/bin/mmces node list -Y) || return $?
  print -r -- "$cesNodes" | awk -F: -v node="$cesNode" '
    $1 == "mmces" && $2 == "nodeList" && $3 != "HEADER" && $8 == node { count++ }
    END { exit !(count == 1) }
  ' || {
    print -u2 -- "[Azure] Local node is not an unambiguous CES member"
    return 1
  }
}

function resolveAzureCesPeer
{
  typeset nic=$1 peerNics daemonAddresses matches
  peer_vm_id=$(print -r -- "$nic" | jq -er '.virtualMachine.id | select(type == "string" and length > 0)') || return $?
  typeset -l peerIdLower="$peer_vm_id" localIdLower="$vm_id"
  [[ "$peerIdLower" != "$localIdLower" ]] || {
    print -u2 -- "[Azure] Refusing takeover from another NIC on the local VM"
    return 1
  }
  peer_vm=$(/usr/bin/az vm show --ids "$peer_vm_id" -o json) || return $?
  print -r -- "$peer_vm" | jq -e --arg id "$peer_vm_id" '
    (.id | ascii_downcase) == ($id | ascii_downcase)
  ' >/dev/null || {
    print -u2 -- "[Azure] Peer VM identity not confirmed"
    return 1
  }
  peer_name=$(print -r -- "$peer_vm" | jq -er '.name | select(type == "string" and length > 0)') || return $?
  peerNics=$(print -r -- "$nics" | jq -ce --arg vm "$peer_vm_id" --argjson peer "$peer_vm" '
    [.[] | select((.virtualMachine.id // "" | ascii_downcase) == ($vm | ascii_downcase))
      | select(.id as $id | any($peer.networkProfile.networkInterfaces[];
          (.id | ascii_downcase) == ($id | ascii_downcase)))]
    | if length > 0 then . else error("Peer VM NIC membership not confirmed") end
  ') || return $?
  print -r -- "$peerNics" | jq -e --argjson owner "$nic" '
    any(.[]; (.id | ascii_downcase) == ($owner.id | ascii_downcase))
  ' >/dev/null || {
    print -u2 -- "[Azure] Owning NIC is not attached to the peer VM"
    return 1
  }
  daemonAddresses=$(print -r -- "$peerNics" | jq -ce '
    [.[] | .ipConfigurations[] | select(.primary == true) | .privateIPAddress]
  ') || return $?
  matches=$(print -r -- "$cesNodes" | awk -F: '
    $1 == "mmces" && $2 == "nodeList" && $3 != "HEADER" { print $8 " " $10 }
  ' | jq -Rsc --argjson ips "$daemonAddresses" '
    [split("\n")[] | select(length > 0) | split(" ")
      | select(.[1] as $ip | $ips | index($ip)) | .[0]] | unique
  ') || return $?
  print -r -- "$matches" | jq -e --arg local "$cesNode" '
    length == 1 and .[0] != $local
  ' >/dev/null || {
    print -u2 -- "[Azure] Allocation owner is not an unambiguous peer CES VM"
    return 1
  }
}

function prepareAzureCesIP
{
  typeset address=$1 matches count owner config primary peerNic peerNicName peerResourceGroup state
  checkAzureCesAssignment "$address" "$cesNode" || return $?
  nics=$(listAzureCesNics) || return $?
  print -r -- "$nics" | jq -e --arg id "$azure_nic_id" \
    --arg name "${vm_name}-ces-${address//./-}" --arg ip "$address" '
    [.[] | select((.id | ascii_downcase) == ($id | ascii_downcase)) | .ipConfigurations[]
      | select((.name | ascii_downcase) == ($name | ascii_downcase) and .privateIPAddress != $ip)] | length == 0
  ' >/dev/null || {
    print -u2 -- "[Azure] Expected configuration name belongs to another address"
    return 1
  }
  matches=$(print -r -- "$nics" | jq -ce --arg ip "$address" --arg subnet "$subnet" '
    [.[] as $nic | $nic.ipConfigurations[]
      | select(.privateIPAddress == $ip and
          ((.subnet.id | ascii_downcase) == ($subnet | ascii_downcase)))
      | {nic: $nic, config: .name, primary: .primary}]
  ') || return $?
  count=$(print -r -- "$matches" | jq -r 'length') || return $?
  if [[ "$count" == 0 ]]
  then
    print -- yes
    return 0
  fi
  [[ "$count" == 1 ]] || {
    print -u2 -- "[Azure] Refusing ambiguous allocation for $address"
    return 1
  }
  owner=$(print -r -- "$matches" | jq -er '.[0].nic.id') || return $?
  config=$(print -r -- "$matches" | jq -er '.[0].config') || return $?
  primary=$(print -r -- "$matches" | jq -r '.[0].primary') || return $?
  [[ "$primary" == false ]] || {
    print -u2 -- "[Azure] Refusing primary or unknown allocation for $address"
    return 1
  }
  typeset -l ownerLower="$owner" localNicLower="$azure_nic_id"
  if [[ "$ownerLower" == "$localNicLower" ]]
  then
    typeset -l configLower="$config" expectedLower="${vm_name}-ces-${address//./-}"
    [[ "$configLower" == "$expectedLower" ]] || {
      print -u2 -- "[Azure] Refusing unexpected local allocation"
      return 1
    }
    print -- no
    return 0
  fi
  peerNic=$(print -r -- "$matches" | jq -c '.[0].nic') || return $?
  peerNicName=$(print -r -- "$peerNic" | jq -er '.name | select(type == "string" and length > 0)') || return $?
  peerResourceGroup=$(print -r -- "$peerNic" | jq -er '.resourceGroup | select(type == "string" and length > 0)') || return $?
  resolveAzureCesPeer "$peerNic" || return $?
  typeset -l configLower="$config" expectedLower="${peer_name}-ces-${address//./-}"
  [[ "$configLower" == "$expectedLower" ]] || {
    print -u2 -- "[Azure] Refusing unexpected peer allocation name"
    return 1
  }
  state=$(/usr/bin/az vm get-instance-view --ids "$peer_vm_id" \
    --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code | [0]" -o tsv) || return $?
  [[ "$state" == PowerState/stopped || "$state" == PowerState/deallocated ]] || {
    print -u2 -- "[Azure] Will not release $address: peer state is $state"
    return 1
  }
  checkAzureCesAssignment "$address" "$cesNode" || return $?
  /usr/bin/az network nic ip-config delete --subscription "$subscription" \
    --resource-group "$peerResourceGroup" --nic-name "$peerNicName" \
    --name "$config" -o none || return $?
  nics=$(listAzureCesNics) || return $?
  print -r -- "$nics" | jq -e --arg ip "$address" --arg subnet "$subnet" '
    [.[] | .ipConfigurations[] | select(.privateIPAddress == $ip and
      ((.subnet.id | ascii_downcase) == ($subnet | ascii_downcase)))] | length == 0
  ' >/dev/null || {
    print -u2 -- "[Azure] Peer release not confirmed"
    return 1
  }
  checkAzureCesAssignment "$address" "$cesNode" || return $?
  print -- yes
}

function reconcileAzureCesIP
{
  typeset ipWithMask=$4 interface=$6 address=${4%/*} create verified addresses count
  typeset subscription vm_name resource_group vm_id nics local_nic azure_nic_id azure_nic_name subnet cesNode cesNodes
  discoverAzureCesContext "$interface" || return $?
  create=$(prepareAzureCesIP "$address") || return $?
  if [[ "$create" == yes ]]
  then
    checkAzureCesAssignment "$address" "$cesNode" || return $?
    /usr/bin/az network nic ip-config create --subscription "$subscription" \
      --resource-group "$resource_group" --nic-name "$azure_nic_name" \
      --name "${vm_name}-ces-${address//./-}" --private-ip-address "$address" \
      --subnet "$subnet" -o none || return $?
  fi
  verified=$(/usr/bin/az network nic show --ids "$azure_nic_id" -o json) || return $?
  print -r -- "$verified" | jq -e --arg ip "$address" --arg subnet "$subnet" '
    [.ipConfigurations[] | select(.privateIPAddress == $ip and .primary == false and
      ((.subnet.id | ascii_downcase) == ($subnet | ascii_downcase)))] | length == 1
  ' >/dev/null || {
    print -u2 -- "[Azure] Local secondary allocation not confirmed"
    return 1
  }
  checkAzureCesAssignment "$address" "$cesNode" || return $?
  addresses=$(ip -j -4 address show dev "$interface") || return $?
  count=$(print -r -- "$addresses" | jq -er --arg ip "$address" --argjson prefix "${ipWithMask#*/}" '
    [.[].addr_info[] | select(.local == $ip and .prefixlen == $prefix)] | length
  ') || return $?
  [[ "$count" == 1 ]] && return 0
  [[ "$count" == 0 ]] || {
    print -u2 -- "[Azure] Ambiguous guest address"
    return 1
  }
  "$@" || return $?
  addresses=$(ip -j -4 address show dev "$interface") || return $?
  print -r -- "$addresses" | jq -e --arg ip "$address" --argjson prefix "${ipWithMask#*/}" '
    [.[].addr_info[] | select(.local == $ip and .prefixlen == $prefix)] | length == 1
  ' >/dev/null || {
    print -u2 -- "[Azure] Guest activation not confirmed"
    return 1
  }
}

function enableIP
{
  lockAzureCesIP || return $?
  reconcileAzureCesIP "$@"
  typeset result=$?
  logAzureCesEvent "enable-rc-$result" "$4"
  return $result
}

function cleanupIP
{
  typeset address=$5 interface=$7 addresses prefix
  lockAzureCesIP || return $?
  addresses=$(ip -j -4 address show dev "$interface") || return $?
  prefix=$(print -r -- "$addresses" | jq -er --arg ip "$address" '
    [.[].addr_info[] | select(.local == $ip) | .prefixlen]
    | if length == 1 then .[0] else error("CES guest address missing or ambiguous") end
  ') || return $?
  reconcileAzureCesIP /usr/sbin/ip a replace "$address/$prefix" dev "$interface"
}

function deleteIP
{
  typeset address=${4%/*} interface=$6 verified config count addresses
  typeset subscription vm_name resource_group vm_id nics local_nic azure_nic_id azure_nic_name subnet cesNode cesNodes
  lockAzureCesIP || return $?
  discoverAzureCesContext "$interface" || return $?
  checkAzureCesAssignment "$address" "$cesNode" remote || return $?
  config="${vm_name}-ces-${address//./-}"
  verified=$(/usr/bin/az network nic show --ids "$azure_nic_id" -o json) || return $?
  print -r -- "$verified" | jq -e --arg name "$config" --arg ip "$address" '
    [.ipConfigurations[] | select((.name | ascii_downcase) == ($name | ascii_downcase))] as $named
    | [.ipConfigurations[] | select(.privateIPAddress == $ip)] as $address
    | (($named | length) == 0 and ($address | length) == 0) or
      (($named | length) == 1 and ($address | length) == 1 and
        $named[0].privateIPAddress == $ip and $named[0].primary == false)
  ' >/dev/null || {
    print -u2 -- "[Azure] Refusing unexpected local configuration"
    return 1
  }
  config=$(print -r -- "$verified" | jq -er --arg name "$config" '
    [.ipConfigurations[] | select((.name | ascii_downcase) == ($name | ascii_downcase))]
    | if length == 0 then $name else .[0].name end
  ') || return $?
  addresses=$(ip -j -4 address show dev "$interface") || return $?
  count=$(print -r -- "$addresses" | jq -er --arg ip "$address" '
    [.[].addr_info[] | select(.local == $ip)] | length
  ') || return $?
  if [[ "$count" != 0 ]]
  then
    checkAzureCesAssignment "$address" "$cesNode" remote || return $?
    "$@" || return $?
  fi
  addresses=$(ip -j -4 address show dev "$interface") || return $?
  print -r -- "$addresses" | jq -e --arg ip "$address" '
    [.[].addr_info[] | select(.local == $ip)] | length == 0
  ' >/dev/null || {
    print -u2 -- "[Azure] Guest removal not confirmed"
    return 1
  }
  checkAzureCesAssignment "$address" "$cesNode" remote || return $?
  if print -r -- "$verified" | jq -e --arg name "$config" '
    [.ipConfigurations[] | select((.name | ascii_downcase) == ($name | ascii_downcase))] | length == 1
  ' >/dev/null
  then
    /usr/bin/az network nic ip-config delete --subscription "$subscription" \
      --resource-group "$resource_group" --nic-name "$azure_nic_name" \
      --name "$config" -o none || return $?
  fi
  verified=$(/usr/bin/az network nic show --ids "$azure_nic_id" -o json) || return $?
  print -r -- "$verified" | jq -e --arg name "$config" --arg ip "$address" '
    [.ipConfigurations[] | select((.name | ascii_downcase) == ($name | ascii_downcase) or .privateIPAddress == $ip)] | length == 0
  ' >/dev/null || {
    print -u2 -- "[Azure] Azure removal not confirmed"
    return 1
  }
}
