# Proxmox Deployment Guide (outline — finalized in M8)

FrameFound runs in a **VM on Proxmox**, never on the host itself.

## VM sizing

| Profile | vCPU | RAM | App disk (SSD) | GPU |
|---|---|---|---|---|
| Testing | 4 | 16 GB | 100 GB | none (CPU processing) |
| Small production | 8-12 | 32 GB | 250-500 GB | NVIDIA ≥12 GB VRAM (optional) |

App disk holds Postgres, thumbnails, proxies, caches — size it at roughly
10-20% of the original library volume when 1080p proxies are enabled.

## Steps (to be expanded with screenshots)

1. **Create VM**: Ubuntu Server 24.04 LTS, virtio disk/net, CPU type `host`,
   qemu-guest-agent enabled. Static DHCP reservation on your router.
2. **NAS mounts (read-only)** via `/etc/fstab`, e.g.:
   ```
   //nas/media  /mnt/media  cifs  credentials=/root/.smbcreds,ro,uid=1000,iocharset=utf8,vers=3.0,_netdev,nofail  0 0
   nas:/export/media  /mnt/media  nfs  ro,_netdev,nofail,soft,timeo=100  0 0
   ```
   `nofail` + `_netdev` keep the VM booting when the NAS is down; FrameFound
   flags libraries `unmounted` rather than treating files as deleted.
3. **Docker**: official `get.docker.com` script or distro packages; add user to
   `docker` group.
4. **GPU passthrough (optional)**: enable IOMMU on the host, pass the NVIDIA
   device through, install driver + NVIDIA Container Toolkit in the VM, verify
   with `nvidia-smi` and a CUDA container. Use `docker-compose.gpu.yml`.
5. **Install FrameFound**: clone, `cp .env.example .env`, run
   `infrastructure/scripts/install.sh`, open the printed URL, complete the
   first-run wizard with the printed setup token.
6. **Backups**: Proxmox vzdump snapshots of the VM **plus** application-level
   `./manage.sh backup` (database + config) shipped off-VM. Proxies/thumbnails
   are rebuildable and may be excluded. Restore procedure: new VM → install →
   `./manage.sh restore <file>` → remount NAS → reconciliation scan relinks
   assets by content hash.
