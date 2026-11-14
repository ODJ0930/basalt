#!/bin/bash
# One ext4 volume at $MNT made of a sparse file on the VM disk plus a file in a RAM disk, joined with device-mapper.
# The DeepSeek pack is a single ~242 GiB file and Colab's G4 disk is ~236 GiB, so part of it has to live in RAM.
# Colab runs as root with device-mapper available but no loop/dm device nodes: they are created here by hand.
set -euo pipefail
DISK_GB=${DISK_GB:-220}       # sparse file on the VM disk (leave ~15 GiB free for the build and pip)
RAM_GB=${RAM_GB:-30}          # file in a tmpfs; RAM left for basalt's expert tier = total RAM - this
MNT=${MNT:-/mnt/models}

if mountpoint -q "$MNT"; then echo "$MNT is already mounted"; df -h "$MNT"; exit 0; fi
command -v dmsetup >/dev/null || { apt-get -qq update >/dev/null; apt-get -qq install -y dmsetup >/dev/null; }
mkdir -p /dev/mapper /mnt/ramdisk "$MNT"
[ -e /dev/mapper/control ] || mknod /dev/mapper/control c 10 236
mountpoint -q /mnt/ramdisk || mount -t tmpfs -o size=$((RAM_GB + 1))G tmpfs /mnt/ramdisk

truncate -s "${DISK_GB}G" /content/models-disk.img
truncate -s "${RAM_GB}G" /mnt/ramdisk/models-ram.img

attach() {   # a free loop device for file $1 (the node is made if missing); prints its path
  local dev n
  dev=$(losetup -f); n=${dev##*loop}
  [ -e "$dev" ] || mknod "$dev" b 7 "$n"
  losetup "$dev" "$1"
  echo "$dev"
}
A=$(attach /content/models-disk.img)
B=$(attach /mnt/ramdisk/models-ram.img)
SA=$(blockdev --getsz "$A"); SB=$(blockdev --getsz "$B")
printf '0 %s linear %s 0\n%s %s linear %s 0\n' "$SA" "$A" "$SA" "$SB" "$B" | dmsetup create models
dmsetup mknodes models
mkfs.ext4 -q -F -m 0 -T largefile4 /dev/mapper/models
mount /dev/mapper/models "$MNT"
echo "disk part: $A ($DISK_GB GiB), RAM part: $B ($RAM_GB GiB)"
df -h "$MNT"
