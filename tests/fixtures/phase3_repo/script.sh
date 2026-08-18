#!/usr/bin/env bash
set -e
source ./lib.sh

function greet() {
  local name="$NAME"
  echo "hello $name"
}

greet
helper | jq .
