#!/bin/zsh

set -u

label="me.gorani.image-upload"
domain="gui/$(id -u)"
mount_point="${GORANI_SMB_MOUNT_POINT:-/Volumes/gorani-images}"

if /sbin/mount | /usr/bin/grep -Fq -- " on ${mount_point} (smbfs,"; then
  print "SMB: mounted (${mount_point})"
else
  print "SMB: disconnected (${mount_point})"
fi

if /bin/launchctl print "${domain}/${label}" >/dev/null 2>&1; then
  print "LaunchAgent: loaded (${label})"
else
  print "LaunchAgent: not loaded (${label})"
fi

if /usr/bin/curl --fail --silent --max-time 2 http://127.0.0.1:8080/healthz >/dev/null; then
  print "HTTP: healthy (http://127.0.0.1:8080/healthz)"
  exit 0
fi

print "HTTP: unavailable (http://127.0.0.1:8080/healthz)"
exit 1
