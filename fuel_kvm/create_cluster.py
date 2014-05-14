import os
import sys
import yaml
import time
import socket
import argparse
import contextlib
import subprocess

import libvirt 
import paramiko

class XMLTemplates(object):
    network = """<?xml version="1.0" encoding="UTF-8"?>
    <network>
      <name>{name}</name>
      <bridge name="{br_name}" />
      <ip address="{ip}" netmask="{netmask}" />
      {forward}
    </network>"""

    vm_net = """    
    <interface type='network'>
      <mac address='{mac}'/>
      <source network='{name}'/>
      <model type='virtio'/>
    </interface>
    """

    vm_cdrom = """    
    <disk type='file' device='cdrom'>
        <driver name='qemu' type='raw'/>
        <source file='{path}' />
        <target dev='{dev}' bus='ide'/>
        <readonly />
    </disk>    
    """

    vm_hd = """    
    <disk type='file' device='disk'>
        <driver name='qemu' type='qcow2'/>
        <source file='{path}' />
        <target dev='{dev}' bus='virtio'/>
    </disk>
    """

    vm = """<?xml version="1.0" encoding="UTF-8"?>
    <domain type="kvm" id="2">
        <name>{name}</name>
        <memory unit="{mem_unit}">{mem}</memory>
        <vcpu>{cpus}</vcpu>
        <os>
            <type arch="x86_64" machine="pc-1.2">hvm</type>
            <boot dev="hd" />
            {boot_cdrom}
            {boot_network}
        </os>
        <features>
            <acpi />
            <apic />
            <pae />
        </features>
        <clock offset="utc" />
        <on_poweroff>destroy</on_poweroff>
        <on_reboot>restart</on_reboot>
        <on_crash>restart</on_crash>
        <devices>
            <emulator>/usr/bin/kvm</emulator>
            {disks}
            {cdrom}
            {networks}
            <serial type="pty">
                <source path="/dev/pts/19" />
                <target port="0" />
                <alias name="serial0" />
            </serial>
            <input type="mouse" bus="ps2" />
            <graphics type="vnc" port="5900" autoport="yes" listen="0.0.0.0">
                <listen type="address" address="0.0.0.0" />
            </graphics>
            <video>
                <model type="cirrus" vram="9216" heads="1" />
            </video>
            <memballoon model="virtio" />
      </devices>
    </domain>
    """

class Node(object):
    def __init__(self, name, memory, cpu, networks, disks, **params):
        self.name = name
        self.memory = memory
        self.cpus = cpu
        self.networks = networks.split()
        self.disks = disks.split()
        self.params = params
        self.boot_network = True


class Network(object):
    def __init__(self, name, br_name, ip_and_mask, *attrs):
        self.name = name
        self.br_name = br_name
        self.ip_and_mask = ip_and_mask
        self.attrs = attrs


class Cluster(object):
    def __init__(self, nets, fuel_vm, vms, **attrs):
        self.nets = nets
        self.fuel_vm = fuel_vm
        self.vms = vms
        self.attrs = attrs

@contextlib.contextmanager
def action(message):
    print message + " ...",
    sys.stdout.flush()

    try:
        yield
    except Exception as x:
        print "failed"
        raise
    else:
        print "ok"

def load_cluster_description(yaml_descr):
    data = yaml.load(yaml_descr)
    nets = {name:Network(name, *(params.split()))
                for name, params in data['networks'].items()}

    fuel_vm = Node('fuel', **data['fuel_vm'])

    cluster = {}
    for name, params in data['cluster'].items():
        if isinstance(params, basestring):
            assert params[0] == '='
            cluster[name] = Node(name, **data['cluster'][params[1:]])
        else:
            cluster[name] = Node(name, **params)

    del data['cluster']
    del data['fuel_vm']
    del data['networks']

    return Cluster(nets, fuel_vm, cluster, **data)


def create_disk_image(path, size):
    if not os.path.exists(path):
        with action("Create disk image {} size {}".format(path, size)):
            cmd = ('qemu-img', 'create', '-f', 'qcow2', path, size)
            subprocess.check_output(cmd)


def net_sz_to_mask(sz):
    imask = 0
    for i in range(sz):
        imask = (imask << 1) + 1
    imask <<= (32 - sz)
    return "{}.{}.{}.{}".format(imask >> 24, 
                                (imask >> 16) & 0xFF,
                                (imask >> 8) & 0xFF,
                                imask & 0xFF)


def create_network(conn, net):
    try:
        conn.networkLookupByName(net.name)
    except libvirt.libvirtError as x:
        if x.get_error_code() != libvirt.VIR_ERR_NO_NETWORK:
            raise
    else:
        print "Network {} already exists".format(net.name)
        return

    params = {'name': net.name}
    params['br_name'] = net.br_name
    params['ip'], sz = net.ip_and_mask.split('/')
    params['netmask'] = net_sz_to_mask(int(sz))

    if "NAT" in net.attrs:
        params['forward'] = '<forward mode="nat" />'
    else:
        params['forward'] = ""

    net_xml = XMLTemplates.network.format(**params)
    with action("Creating network {}".format(net.name)):
        conn.networkCreateXML(net_xml)


def get_mac(curr_mac=[0x525400da7227]):
    curr_mac[0] += 1
    hmac = hex(curr_mac[0])
    digit_pairs = [hmac[i * 2: i * 2 + 2] for i in range(1, 7)]
    return "{}:{}:{}:{}:{}:{}".format(*digit_pairs)


def launch_vm(conn, vm, nets, images_path):
    print "Start vm", vm.name
    try:
        conn.lookupByName(vm.name)
    except libvirt.libvirtError as x:
        if x.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
            raise
    else:
        print "Vm {!r} already exists".format(vm.name)
        return
    
    nets_xml = ""
    for net_name in vm.networks:
        create_network(conn, nets[net_name])
        nets_xml += XMLTemplates.vm_net.format(mac=get_mac(), 
                                           name=net_name)
    hds_xml = ""
    for pos, disk_size in enumerate(vm.disks):
        dev_name = 'vd' + chr(ord('a') + pos)
        fname = "{}_{}.qcow2".format(vm.name, dev_name)
        path = os.path.join(images_path, fname)
        create_disk_image(path, disk_size)
        hds_xml += XMLTemplates.vm_hd.format(path=path, 
                                         dev=dev_name)
    cd_dev_pos = pos + 1

    if 'iso' in vm.params:
        dev_name = 'vd' + chr(ord('a') + cd_dev_pos)
        cdrom_xml = XMLTemplates.vm_cdrom.format(path=vm.params['iso'], 
                                             dev=dev_name)
        boot_cdrom_xml = '<boot dev="cdrom" />'
    else:
        cdrom_xml = ""
        boot_cdrom_xml = ""

    if vm.boot_network:
        boot_network_xml = "<boot dev='network' />"
    else:
        boot_network_xml = ""

    munit = {'M':'MiB', 'K':'KiB', 'G':'GiB', 'T':'TiB'}[vm.memory[-1]]
    vm_xml = XMLTemplates.vm.format(name=vm.name,
                                    cpus=vm.cpus,
                                    mem=vm.memory[:-1],
                                    mem_unit=munit,
                                    boot_cdrom=boot_cdrom_xml,
                                    boot_network=boot_network_xml,
                                    disks=hds_xml,
                                    cdrom=cdrom_xml,
                                    networks=nets_xml)
    conn.createXML(vm_xml)
    print "VM launched"


def wait_fuel_installed(fuel_vm):
    log_file = '/var/log/puppet/bootstrap_admin_node.log'
    user_and_passwd, host = fuel_vm.params['ssh_creds'].split('@')
    user, passwd = user_and_passwd.split(':')

    with action("Wait untill vm appears online"):
        while True:
            s = socket.socket()
            s.settimeout(1)
            try:
                s.connect((host, 22))
                break
            except socket.error:
                pass

    with action("Wait ssh connection"):
        while True:
            try:
                t = paramiko.Transport((host, 22))
                break
            except paramiko.SSHException as x:
                if 'No route to host' not in str(x):
                    raise


    t.connect(username=user, password=passwd)
    sftp = paramiko.SFTPClient.from_transport(t)

    with action("Wait installation finished"):
        while True:
            try:
                remote_file_data = sftp.open(log_file).read()
                if 'Finished catalog run' in remote_file_data:
                    break
            except IOError:
                pass
            time.sleep(10)

    t.close()


def main(args):
    parser = argparse.ArgumentParser()
    parser.add_argument('--libvirt-url', dest='libvirturl',
                        default="qemu:///system",
                        help='Set libvirt connection url - default is "qemu:///system"')
    parser.add_argument('cluster_description_file', type=argparse.FileType('r'),
                        help='File with cluster description')
    args = parser.parse_args()

    with action("Connecting to libvirt at {}".format(args.libvirturl)):
        conn = libvirt.open(args.libvirturl)

    with action("Load cluster description from {}".format(args.cluster_description_file.name)):
        cluster = load_cluster_description(args.cluster_description_file.read())
    
    images_path = cluster.attrs['images_path']
    print "Will store images to", images_path

    cluster.fuel_vm.boot_network = False
    launch_vm(conn, cluster.fuel_vm, cluster.nets, images_path)
    wait_fuel_installed(cluster.fuel_vm)

    for vm_name, vm in cluster.vms.items():
        launch_vm(conn, vm, cluster.nets, images_path)
    return 0

if __name__ == "__main__":
    exit(main(sys.argv))



