# Proxmox VM Setup for Comadre

Step-by-step guide for creating a Debian 13 VM on Proxmox VE to run Comadre.

**Research date:** 2026-03-25
**Proxmox VE version:** 9.1 (released 2025-11-19, based on Debian 13 "Trixie")
**Guest OS:** Debian 13.4 "Trixie" (released 2026-03-14)

## 1. Download the Debian ISO

In the Proxmox web UI (`https://<proxmox-ip>:8006`):

1. Expand your node in the left sidebar, click **local** storage
2. Go to the **ISO Images** tab
3. Click **Download from URL**
4. Paste:

    ```text
    https://cdimage.debian.org/debian-cd/13.4.0/amd64/iso-cd/debian-13.4.0-amd64-netinst.iso
    ```

5. Click **Query URL**, then **Download**

Alternative -- download directly on the Proxmox host via SSH:

```bash
cd /var/lib/vz/template/iso/
wget https://cdimage.debian.org/debian-cd/13.4.0/amd64/iso-cd/debian-13.4.0-amd64-netinst.iso
```

## 2. Create the VM

Click **Create VM** in the top-right. Check **Advanced** at the bottom of the wizard.

### General

| Field | Value |
| --- | --- |
| Name | `comadre-tts` |
| Add to HA | No (only useful for multi-node clusters with automatic failover) |

### OS

| Field | Value |
| --- | --- |
| ISO image | `debian-13.4.0-amd64-netinst.iso` |
| Type | Linux |
| Version | 6.x - 2.6 Kernel |

### System

| Field | Value | Why |
| --- | --- | --- |
| Machine | q35 | Modern chipset, better PCIe support |
| BIOS | OVMF (UEFI) | Recommended for Debian 13 |
| EFI Storage | local-lvm | Required with OVMF |
| Add TPM | No | Unnecessary for a TTS server VM |
| Pre-Enroll keys | No | Secure Boot not needed for internal workloads |
| SCSI Controller | VirtIO SCSI single | Best disk performance with IO threads |
| QEMU Agent | Enabled | Shows IP in Proxmox, graceful shutdown, backup freeze |
| Graphic card | Default (VGA) | No GPU needed, Comadre runs 100% on CPU |

### Disks

| Field | Value | Why |
| --- | --- | --- |
| Bus/Device | SCSI | Uses VirtIO SCSI controller above |
| Storage | local-lvm | Or your preferred storage |
| Size | 32 GB | 20 GB minimum, 32 GB gives headroom for Docker images |
| SSD Emulation | Enabled | Informs guest the disk is SSD, enables TRIM/discard |
| Cache | Write back | Best performance |
| Discard | Enabled | Reclaims space via TRIM on thin-provisioned storage |
| IO Thread | Enabled | Dedicated IO thread per disk, 30-50% improvement |
| Skip Replication | Enabled | Replication is for multi-node clusters with shared storage |

### CPU

| Field | Value | Why |
| --- | --- | --- |
| Sockets | 1 | |
| Cores | 4 | Qwen3 sweet spot (diminishing returns beyond 4) |
| Type | host | Exposes AVX/AVX2/FMA to guest. 3-10% faster than kvm64 for CPU-bound inference. Only use kvm64 if you need live migration across different CPUs. |

### Memory

| Field | Value | Why |
| --- | --- | --- |
| Memory (MiB) | 8192 | |
| Ballooning | Disabled | Predictable latency for TTS inference |
| KSM (Kernel Same-page Merging) | Disabled | Adds CPU overhead and unpredictable latency, harmful for inference |

### Network

| Field | Value | Why |
| --- | --- | --- |
| Bridge | vmbr0 | VM gets LAN IP via DHCP |
| Model | VirtIO (virtio) | Paravirtualized, vastly faster than e1000 |
| Firewall | Disabled | Unnecessary overhead for internal LAN; use ufw inside the VM if needed |

Click **Finish**.

## 3. Install Debian

1. Select the VM in Proxmox > **Console** > **Start**
2. Walk through the Debian installer:
   - Language, keyboard, hostname, domain (leave empty)
   - Root password (set one or leave empty for sudo-only)
   - Create a user account and password
   - Partitioning: **Guided - use entire disk** (default is fine)
   - Software selection: **uncheck** Desktop Environment, **check** SSH server and standard system utilities
3. Reboot when prompted. Proxmox auto-ejects the ISO.

## 4. Post-install configuration

All steps below are done via the Proxmox console, logged in as the user
you created during installation.

### Install sudo

The Debian netinst with a root password does not install sudo by default:

```bash
su -
apt install -y sudo
usermod -aG sudo YOUR_USER
exit
```

Log out and back in for the group to take effect.

### Configure static IP

Find the network interface name:

```bash
ip link show
```

Typically `ens18` on Proxmox VirtIO. Edit the network config:

```bash
sudo nano /etc/network/interfaces
```

Replace the DHCP block for your interface with:

```text
auto ens18
iface ens18 inet static
    address YOUR_STATIC_IP/24
    gateway YOUR_GATEWAY
    dns-nameservers 1.1.1.1 8.8.8.8
```

Apply:

```bash
sudo systemctl restart networking
```

The `dns-nameservers` directive in `/etc/network/interfaces` requires the
`resolvconf` package, which is not installed by default. Instead, set the
nameservers directly:

```bash
sudo nano /etc/resolv.conf
```

```text
nameserver 1.1.1.1
nameserver 8.8.8.8
```

Verify connectivity:

```bash
ping -c 1 google.com
```

### Install avahi (mDNS)

Enables `HOSTNAME.local` resolution on the LAN:

```bash
sudo apt update
sudo apt install -y avahi-daemon
sudo systemctl enable --now avahi-daemon
```

### Install essentials and guest agent

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y qemu-guest-agent htop curl git
sudo systemctl enable --now qemu-guest-agent
```

The VM IP now appears in Proxmox UI under VM > Summary > IPs.

### SSH key setup (from your workstation)

```bash
ssh-copy-id YOUR_USER@HOSTNAME.local
ssh YOUR_USER@HOSTNAME.local
```

> **Note:** Docker installation, Docker log limits, and performance tuning are
> handled by the Ansible bootstrap playbook. Do not install Docker manually.

## 5. Deploy Comadre

The VM is ready. See [local-testing-guide.md](local-testing-guide.md) for steps to build and run the service, or use Ansible from your workstation:

```bash
cd deploy/ansible

# Edit inventory with your VM's IP and SSH user
vim inventory/production.yml

# Deploy
export KEY_REGISTRY_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
ansible-playbook playbooks/bootstrap.yml -i inventory/production.yml
```

## Notes

- **Nested virtualization is NOT needed.** Docker uses namespaces/cgroups, not hardware virtualization.
- **VM is the right choice** (not LXC). Docker in LXC requires privileged containers and kernel-sharing hacks. Proxmox documentation recommends VMs for Docker workloads.
- **CPU type `host`** passes through all CPU instruction sets including AVX/AVX2 and FMA, which benefits GGML-based Qwen3 inference.
- **ARM is not supported.** The `qwen3-tts-cli` binary is compiled for x86-64. ARM hosts (Oracle free tier, Raspberry Pi) require recompilation.

## VM config summary

```text
Name:    comadre-tts
HA:      No
OS:      Debian 13.4 "Trixie" (netinst)
Machine: q35, OVMF (UEFI), no TPM, no Secure Boot
GPU:     Default (VGA), no passthrough
CPU:     4 cores, type=host, 1 socket
RAM:     8192 MiB, ballooning off, KSM off
Disk:    32 GB, VirtIO SCSI single, SSD emulation, IO thread, discard, write-back cache, skip replication
Network: vmbr0, VirtIO (virtio), firewall off
Agent:   QEMU Guest Agent enabled
```

## Sources

- [Proxmox VE 9.1 release announcement](https://proxmox.com/en/about/company-details/press-releases/proxmox-virtual-environment-9-1)
- [Proxmox VE downloads](https://www.proxmox.com/en/downloads/proxmox-virtual-environment)
- [Debian 13 "Trixie" release notes](https://www.debian.org/releases/trixie/)
- [Debian 13.4 ISO download](https://cdimage.debian.org/debian-cd/13.4.0/amd64/iso-cd/)
- [Docker Engine install on Debian (official)](https://docs.docker.com/engine/install/debian/)
- [Proxmox VE 9 VM template for Debian Trixie](https://forum.proxmox.com/threads/pve9-create-a-vm-template-for-a-debian-trixie-server-with-cloud-init.170206/)
- [Proxmox VM best practices](https://forum.proxmox.com/threads/best-practice-and-recommendations-for-building-vm-templates.173743/)
- [QEMU Guest Agent wiki](https://pve.proxmox.com/wiki/Qemu-guest-agent)
