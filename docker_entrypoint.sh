#!/bin/sh
set -eu

# -------------------------------------------------------------------
# ADB connect + port forwarding for Unity API (default: 37772)
#
# - QA_DEVICE: ADB endpoint or serial (e.g. host.docker.internal:5555, 192.168.0.10:5555, emulator-5554)
# - QA_UNITY_API_PORT: Local/device port to forward (default: 37772)
# - QA_ADB_CONNECT: 1/0 (default: 1) - run `adb connect` when QA_DEVICE looks like host:port
# - QA_ADB_SERIAL: Explicit device serial to target (optional)
# -------------------------------------------------------------------

QA_UNITY_API_PORT="${QA_UNITY_API_PORT:-37772}"
QA_ADB_CONNECT="${QA_ADB_CONNECT:-1}"
QA_ADB_CONNECT_RETRIES="${QA_ADB_CONNECT_RETRIES:-5}"
QA_ADB_CONNECT_SLEEP="${QA_ADB_CONNECT_SLEEP:-1}"
QA_ADB_FORWARD_RETRIES="${QA_ADB_FORWARD_RETRIES:-5}"
QA_ADB_FORWARD_SLEEP="${QA_ADB_FORWARD_SLEEP:-1}"
QA_ADB_VERBOSE="${QA_ADB_VERBOSE:-0}"

adb start-server >/dev/null 2>&1 || true

_log() {
  if [ "$QA_ADB_VERBOSE" = "1" ]; then
    # stderr so it shows up in docker logs but doesn't pollute stdout if caller expects it
    echo "$@" >&2
  fi
}

_first_adb_device() {
  adb devices 2>/dev/null | awk 'NR>1 && $2=="device" {print $1; exit}'
}

_is_device_connected() {
  serial="$1"
  [ -n "$serial" ] || return 1
  adb devices 2>/dev/null | awk -v s="$serial" 'NR>1 && $1==s && $2=="device" {found=1} END{exit found?0:1}'
}

if [ -n "${QA_DEVICE:-}" ] && [ "$QA_ADB_CONNECT" = "1" ]; then
  case "$QA_DEVICE" in
    *:*)
      i=1
      while [ "$i" -le "$QA_ADB_CONNECT_RETRIES" ]; do
        _log "adb connect $QA_DEVICE (attempt $i/$QA_ADB_CONNECT_RETRIES)"
        adb connect "$QA_DEVICE" >/dev/null 2>&1 || true
        if _is_device_connected "$QA_DEVICE"; then
          break
        fi
        i=$((i + 1))
        sleep "$QA_ADB_CONNECT_SLEEP"
      done
      ;;
  esac
fi

ADB_SERIAL="${QA_ADB_SERIAL:-}"
if [ -z "$ADB_SERIAL" ]; then
  if _is_device_connected "${QA_DEVICE:-}"; then
    ADB_SERIAL="$QA_DEVICE"
  else
    ADB_SERIAL="$(_first_adb_device)"
  fi
fi

if [ -n "$ADB_SERIAL" ]; then
  i=1
  while [ "$i" -le "$QA_ADB_FORWARD_RETRIES" ]; do
    _log "adb -s $ADB_SERIAL forward tcp:$QA_UNITY_API_PORT tcp:$QA_UNITY_API_PORT (attempt $i/$QA_ADB_FORWARD_RETRIES)"
    adb -s "$ADB_SERIAL" forward "tcp:$QA_UNITY_API_PORT" "tcp:$QA_UNITY_API_PORT" >/dev/null 2>&1 && break
    i=$((i + 1))
    sleep "$QA_ADB_FORWARD_SLEEP"
  done
fi

exec "$@"
