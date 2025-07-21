#!/bin/bash

# set -x
# set -e
# set -o pipefail

check_sudo() {
	if [ "$EUID" -ne 0 ]; then
		echo "Please run as root."
		exit
	fi
}

host() {
	local ns=$1
	ip netns exec ns$ns bash
}

kitty () {
	local ns=$1
	if [[ $ns == "hosts" ]]; then
		ip netns exec ns1 kitty &
		ip netns exec ns4 kitty &
	else
		ip netns exec ns$ns kitty &
	fi
}

main() {
	check_sudo
	echo "If connectivity doesn't work, remember to run: "
	echo "modprobe br_netfilter"
	echo "modprobe sch_netem"
	echo "sysctl -w net.bridge.bridge-nf-call-arptables=0"
	echo "sysctl -w net.bridge.bridge-nf-call-ip6tables=0"
	echo "sysctl -w net.bridge.bridge-nf-call-iptables=0"
	echo "sysctl -w net.ipv4.ip_forward=1"

	if [ "$#" -lt 1 ]; then
		echo "usage: ventns.sh { kitty [hosts | <host-N> ] | host1 | host2 }"
		exit 1
	fi

	local cmd=$1
	if [[ $cmd == "kitty" ]]; then
		kitty $2
	else
		host $1
	fi
}

main "$@"

