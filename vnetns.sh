#!/bin/bash

usage() {
	echo "usage: vnetns.sh { <N> | kitty { hosts | <N> } }" >&2
	echo "  <N>          open a shell in namespace nsN (1-4)" >&2
	echo "  kitty <N>    open a kitty terminal in namespace nsN" >&2
	echo "  kitty hosts  open kitty terminals in ns1 and ns4" >&2
	exit 1
}

check_sudo() {
	if [ "$EUID" -ne 0 ]; then
		echo "Please run as root." >&2
		exit 1
	fi
}

check_ns() {
	local ns=$1
	if ! ip netns list | grep -qw "^ns$ns"; then
		echo "Namespace ns$ns does not exist, run 'main.py setup' first." >&2
		exit 1
	fi
}

host() {
	local ns=$1
	check_ns "$ns"
	ip netns exec "ns$ns" bash
}

kitty() {
	local ns=$1
	if [[ $ns == "hosts" ]]; then
		check_ns 1
		check_ns 4
		ip netns exec ns1 kitty &
		ip netns exec ns4 kitty &
	else
		check_ns "$ns"
		ip netns exec "ns$ns" kitty &
	fi
}

main() {
	if [[ $1 == "kitty" ]]; then
		[ "$#" -eq 2 ] || usage
		[[ $2 == "hosts" || $2 =~ ^[1-4]$ ]] || usage
		check_sudo
		kitty "$2"
	else
		[ "$#" -eq 1 ] || usage
		[[ $1 =~ ^[1-4]$ ]] || usage
		check_sudo
		host "$1"
	fi
}

main "$@"
